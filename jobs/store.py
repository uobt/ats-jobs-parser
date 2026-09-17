"""Хранилище парсера: SQLite в файловом режиме, как и весь остальной outbound-os.

Postgres не нужен: 2-5 млн строк метаданных вакансий помещаются на диск, а вся
работа — точечные upsert'ы по натуральному ключу и один диапазонный запрос по
дате. Схема переносится в Postgres один-в-один, если объём заставит.

Главное здесь — **дельта**, потому что сигнал найма это не «у компании есть
вакансии», а «у компании ПОЯВИЛАСЬ вакансия». Портальный парсер этого не умел:
`refresh_cache: true` перетирал кэш целиком, истории не оставалось.

Правило, из-за нарушения которого дельта тихо врёт: закрытие вакансий
считается ТОЛЬКО по компаниям, которые в этом прогоне опрошены успешно.
Если Greenhouse вернул 503, а мы бы посчитали его вакансии исчезнувшими,
то на следующем прогоне они бы «открылись заново» и дали пачку ложных сигналов.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .normalize import Vacancy

SCHEMA = """
create table if not exists company (
    source            text not null,
    slug              text not null,
    company_name      text,
    careers_url       text,
    site_url          text,
    domain            text,
    domain_source     text,
    country_code      text,
    origin            text,
    first_seen_at     text,
    last_ok_at        text,
    last_error        text,
    fail_streak       integer not null default 0,
    active            integer not null default 1,
    primary key (source, slug)
);
create index if not exists company_domain_idx on company(domain);
create index if not exists company_active_idx on company(active, source);

create table if not exists vacancy (
    source              text not null,
    slug                text not null,
    source_job_id       text not null,
    title               text,
    url                 text,
    apply_url           text,
    company_name        text,
    location_raw        text,
    city                text,
    region              text,
    country_code        text,
    workplace           text,
    department          text,
    seniority           text,
    employment_type     text,
    salary_min          integer,
    salary_max          integer,
    salary_currency     text,
    published_at        text,
    published_precision text,
    first_seen_at       text not null,
    last_seen_at        text not null,
    closed_at           text,
    first_seen_run      integer,
    baseline            integer not null default 0,
    -- Свежесть сигнала = дата публикации, а где её нет — момент, когда вакансию
    -- впервые увидел наш парсер. Выражение под индекс не ложится, поэтому
    -- генерируемая колонка: она и делает выборку сигналов индексной.
    signal_at           text generated always as
                        (coalesce(published_at, first_seen_at)) virtual,
    primary key (source, slug, source_job_id)
);
create index if not exists vacancy_company_idx on vacancy(source, slug);
create index if not exists vacancy_first_seen_idx on vacancy(first_seen_at);
create index if not exists vacancy_published_idx on vacancy(published_at);
create index if not exists vacancy_open_idx on vacancy(closed_at);
-- Обзор считает открытые вакансии по каждой площадке. Без составного индекса
-- это полный проход по таблице на каждый источник: на 600 тыс. строк вкладка
-- открывалась 1.2 с, с индексом — 0.09 с.
create index if not exists vacancy_source_open_idx on vacancy(source, closed_at);
-- Индекс по signal_at создаётся в _ensure_columns, а не здесь: на уже существующей
-- базе `create table if not exists` ничего не делает, колонки ещё нет, и создание
-- индекса упало бы с «no such column» до того, как отработает догоняющий ALTER.

create table if not exists run (
    id                integer primary key autoincrement,
    started_at        text not null,
    finished_at       text,
    status            text not null default 'running',
    sources           text,
    geo               text,
    companies_planned integer default 0,
    companies_ok      integer default 0,
    companies_failed  integer default 0,
    vacancies_seen    integer default 0,
    vacancies_new     integer default 0,
    vacancies_closed  integer default 0,
    http_requests     integer default 0,
    http_retries      integer default 0,
    error             text
);

create table if not exists run_source (
    run_id           integer not null,
    source           text not null,
    companies_ok     integer default 0,
    companies_failed integer default 0,
    vacancies        integer default 0,
    vacancies_new    integer default 0,
    sample_error     text,
    primary key (run_id, source)
);
"""

VACANCY_FIELDS = [
    "title", "url", "apply_url", "company_name", "location_raw", "city", "region",
    "country_code", "workplace", "department", "seniority", "employment_type",
    "salary_min", "salary_max", "salary_currency", "published_at", "published_precision",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CompanyDelta:
    new: int = 0
    closed: int = 0
    seen: int = 0
    reopened: int = 0
    duplicates: int = 0        # сколько дублей отбросили внутри одной выдачи


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=WAL")
        self.conn.execute("pragma synchronous=NORMAL")
        # Без busy_timeout SQLite падает с «database is locked» сразу, а не ждёт.
        # Сценарий обычный: идёт длинный обход и пишет, а в это время открывают
        # вкладку «Вакансии» или запускают ленты. WAL разводит читателей с
        # писателем, но двух писателей — нет, и ждать очереди тут правильнее,
        # чем терять прогон.
        self.conn.execute("pragma busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """Догоняющие ALTER'ы: `create table if not exists` новые колонки не добавляет,
        а база у пользователя уже создана и терять её из-за схемы нельзя."""
        wanted = {
            "run_source": [("truncated", "integer not null default 0"),
                           ("note", "text")],
            "vacancy": [
                ("baseline", "integer not null default 0"),
                # Свежесть везде считается как coalesce(published_at, first_seen_at),
                # а выражение под индекс не ложится — отсюда генерируемая колонка.
                # Именно она превращает полный проход по 700 тыс. строк в поиск
                # по индексу: выборка сигналов падает с десятков секунд до долей.
                ("signal_at",
                 "text generated always as (coalesce(published_at, first_seen_at)) virtual"),
            ],
        }
        for table, columns in wanted.items():
            # table_xinfo, а не table_info: обычный table_info НЕ показывает
            # виртуальные генерируемые колонки, и проверка «есть ли signal_at»
            # всегда отвечала бы «нет» — ALTER падал бы с duplicate column name.
            with closing(self.conn.execute(f"pragma table_xinfo({table})")) as cur:
                existing = {row["name"] for row in cur.fetchall()}
            for name, ddl in columns:
                if name not in existing:
                    self.conn.execute(f"alter table {table} add column {name} {ddl}")
        self.conn.execute(
            "create index if not exists vacancy_signal_at_idx on vacancy(signal_at)")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- реестр компаний ------------------------------------------------------

    def upsert_companies(self, rows: Iterable[dict]) -> int:
        now = utcnow()
        payload = []
        for row in rows:
            source, slug = row.get("source"), row.get("slug")
            if not source or not slug:
                continue
            payload.append((source, slug, row.get("company_name"), row.get("careers_url"),
                            row.get("site_url"), row.get("domain"), row.get("domain_source"),
                            row.get("country_code"), row.get("origin") or "manual", now))
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany("""
                insert into company (source, slug, company_name, careers_url, site_url,
                                     domain, domain_source, country_code, origin, first_seen_at)
                values (?,?,?,?,?,?,?,?,?,?)
                on conflict(source, slug) do update set
                    company_name = coalesce(excluded.company_name, company.company_name),
                    careers_url  = coalesce(excluded.careers_url,  company.careers_url),
                    site_url     = coalesce(excluded.site_url,     company.site_url),
                    domain       = coalesce(company.domain,        excluded.domain),
                    domain_source= coalesce(company.domain_source, excluded.domain_source),
                    country_code = coalesce(excluded.country_code, company.country_code)
            """, payload)
        return len(payload)

    def companies(self, sources: Sequence[str] | None = None, limit: int | None = None,
                  only_active: bool = True, order: str = "rotate") -> list[sqlite3.Row]:
        """Компании к обходу.

        `rotate` (по умолчанию) — карусель по площадкам: из каждой берётся своя
        очередь, и частичный прогон задевает все адаптеры пропорционально.
        Наблюдалось на живом прогоне: при глобальной сортировке по slug'у 1 300
        компаний оказались сплошь на «a», и Rippling с его 273 компаниями
        не опрашивался ни разу — статус-борд показывал прочерк, хотя адаптер рабочий.
        Побочный эффект полезен для вежливости: соседние запросы уходят на разные
        хосты, и троттлинг по хостам почти не заставляет ждать.

        `stale` — строго «кого дольше всех не опрашивали», без учёта площадки.
        """
        where, params = ["1=1"], []
        if only_active:
            where.append("active = 1")
        if sources:
            where.append(f"source in ({','.join('?' * len(sources))})")
            params.extend(sources)
        clause = " and ".join(where)

        if order == "rotate":
            sql = f"""
                select * from (
                    select *, row_number() over (
                        partition by source
                        order by last_ok_at is not null, last_ok_at asc, slug asc) as rn
                    from company where {clause}
                ) order by rn asc, source asc"""
        elif order == "stale":
            sql = (f"select * from company where {clause} "
                   f"order by last_ok_at is not null, last_ok_at asc, slug asc")
        elif order == "slug":
            sql = f"select * from company where {clause} order by source, slug"
        else:
            raise ValueError(f"неизвестный порядок обхода: {order!r}")

        if limit:
            sql += f" limit {int(limit)}"
        with closing(self.conn.execute(sql, params)) as cur:
            return cur.fetchall()

    def mark_company(self, source: str, slug: str, ok: bool, error: str | None = None) -> None:
        with self.conn:
            if ok:
                self.conn.execute(
                    "update company set last_ok_at=?, last_error=null, fail_streak=0 "
                    "where source=? and slug=?", (utcnow(), source, slug))
            else:
                # Три подряд неудачи — борд снимается с обхода. Компания не удаляется:
                # борд может открыться обратно, и мы это увидим при ручном пере-включении.
                self.conn.execute(
                    "update company set last_error=?, fail_streak=fail_streak+1, "
                    "active = case when fail_streak + 1 >= 3 then 0 else active end "
                    "where source=? and slug=?", (error, source, slug))

    # --- вакансии и дельта ----------------------------------------------------

    def apply_company_result(self, source: str, slug: str, vacancies: Sequence[Vacancy],
                             run_id: int | None = None, now: str | None = None,
                             baseline: bool = False) -> CompanyDelta:
        """Записать результат успешного опроса ОДНОЙ компании и посчитать дельту.

        Вызывать только после успешного забора: закрытие вакансий здесь считается
        по разнице множеств, и вызов на неудачном заборе закроет всё подряд.

        `baseline=True` — это ПЕРВЫЙ наш успешный обход компании. Тогда вакансии
        помечаются как базовый срез и не считаются сигналом найма. Иначе получится
        так: у BambooHR, Rippling и части Workday даты публикации нет, свежесть
        выводится из `first_seen_at`, и в день первого запуска ВСЕ вакансии этих
        площадок выглядели бы «только что появившимися». Это был бы не сигнал,
        а шум ровно в тот момент, когда систему смотрят впервые.
        """
        now = now or utcnow()

        # Дедуп внутри одной выдачи. Площадки этим грешат по-разному: Rippling
        # штатно повторяет вакансию по числу локаций, у других это редкий баг.
        # Ловить здесь, а не в адаптерах: натуральный ключ обязан быть уникальным
        # независимо от того, насколько аккуратен конкретный адаптер.
        unique: dict[str, Vacancy] = {}
        for vacancy in vacancies:
            unique.setdefault(vacancy.source_job_id, vacancy)
        duplicates = len(vacancies) - len(unique)
        vacancies = list(unique.values())

        delta = CompanyDelta(seen=len(vacancies), duplicates=duplicates)
        seen_ids = set(unique)

        with closing(self.conn.execute(
                "select source_job_id, closed_at from vacancy where source=? and slug=?",
                (source, slug))) as cur:
            known = {row["source_job_id"]: row["closed_at"] for row in cur.fetchall()}

        with self.conn:
            for vacancy in vacancies:
                values = [getattr(vacancy, f) for f in VACANCY_FIELDS]
                values[VACANCY_FIELDS.index("published_at")] = (
                    vacancy.published_at.isoformat() if vacancy.published_at else None)
                job_id = vacancy.source_job_id
                if job_id not in known:
                    delta.new += 1
                    self.conn.execute(
                        f"insert into vacancy (source, slug, source_job_id, "
                        f"{','.join(VACANCY_FIELDS)}, first_seen_at, last_seen_at, "
                        f"first_seen_run, baseline) "
                        f"values (?,?,?,{','.join('?' * len(VACANCY_FIELDS))},?,?,?,?)",
                        [source, slug, job_id, *values, now, now, run_id,
                         1 if baseline else 0])
                else:
                    if known[job_id] is not None:
                        # Вакансия была закрыта и открылась снова — это тоже наём.
                        delta.reopened += 1
                    self.conn.execute(
                        f"update vacancy set {','.join(f + '=?' for f in VACANCY_FIELDS)}, "
                        f"last_seen_at=?, closed_at=null "
                        f"where source=? and slug=? and source_job_id=?",
                        [*values, now, source, slug, job_id])

            gone = [job_id for job_id, closed in known.items()
                    if job_id not in seen_ids and closed is None]
            if gone:
                delta.closed = len(gone)
                self.conn.executemany(
                    "update vacancy set closed_at=? where source=? and slug=? and source_job_id=?",
                    [(now, source, slug, job_id) for job_id in gone])
        return delta

    def apply_feed_result(self, vacancies: Sequence[Vacancy], run_id: int | None = None,
                          now: str | None = None) -> dict[str, int]:
        """Результат ленты-агрегатора: только вставка и обновление, БЕЗ закрытия.

        Лента — скользящее окно «что появилось за последние дни», а не полный
        каталог компании. Считать разницу множеств здесь нельзя: вакансия исчезает
        из выдачи просто потому, что уехала за край окна, и «закрытие» было бы
        выдумкой. Свежесть по лентам держится только на дате публикации.
        """
        now = now or utcnow()
        stats = {"seen": 0, "new": 0, "companies": 0}
        by_company: dict[tuple[str, str], list[Vacancy]] = {}
        for vacancy in vacancies:
            by_company.setdefault((vacancy.source, vacancy.slug), []).append(vacancy)

        for (source, slug), items in by_company.items():
            unique: dict[str, Vacancy] = {}
            for vacancy in items:
                unique.setdefault(vacancy.source_job_id, vacancy)
            with closing(self.conn.execute(
                    "select source_job_id from vacancy where source=? and slug=?",
                    (source, slug))) as cur:
                known = {row["source_job_id"] for row in cur.fetchall()}
            stats["companies"] += 1
            with self.conn:
                for job_id, vacancy in unique.items():
                    values = [getattr(vacancy, f) for f in VACANCY_FIELDS]
                    values[VACANCY_FIELDS.index("published_at")] = (
                        vacancy.published_at.isoformat() if vacancy.published_at else None)
                    stats["seen"] += 1
                    if job_id in known:
                        self.conn.execute(
                            f"update vacancy set {','.join(f + '=?' for f in VACANCY_FIELDS)}, "
                            f"last_seen_at=? where source=? and slug=? and source_job_id=?",
                            [*values, now, source, slug, job_id])
                        continue
                    stats["new"] += 1
                    self.conn.execute(
                        f"insert into vacancy (source, slug, source_job_id, "
                        f"{','.join(VACANCY_FIELDS)}, first_seen_at, last_seen_at, "
                        f"first_seen_run, baseline) "
                        f"values (?,?,?,{','.join('?' * len(VACANCY_FIELDS))},?,?,?,?)",
                        [source, slug, job_id, *values, now, now, run_id,
                         # без даты публикации из ленты свежесть не подтверждена —
                         # такая строка не должна выглядеть сигналом
                         0 if vacancy.published_at else 1])
        return stats

    def vacancies(self, days: int | None = None, sources: Sequence[str] | None = None,
                  countries: Sequence[str] | None = None, open_only: bool = True,
                  require_domain: bool = False, limit: int = 200_000,
                  company_keys: Sequence[tuple[str, str]] | None = None) -> Iterator[dict]:
        """Поток вакансий для выгрузки. Курсор, а не список: строк сотни тысяч.

        `company_keys` ограничивает выгрузку конкретными компаниями — теми, что
        прошли фильтр сигналов. Без него выгрузка берёт ВСЕ вакансии окна, и на
        экране 657 компаний превращаются в 33 487 строк файла: расхождение
        не в данных, а в том, что на экране компании, а в файле вакансии.
        """
        where, params = ["1=1"], []
        if company_keys is not None:
            # Временная таблица, а не IN-список: компаний бывают тысячи, и в
            # параметры запроса они не помещаются.
            self.conn.execute("drop table if exists temp._export_companies")
            self.conn.execute(
                "create temp table _export_companies (source text, slug text, "
                "primary key (source, slug))")
            self.conn.executemany(
                "insert or ignore into temp._export_companies values (?,?)",
                [(s, g) for s, g in company_keys])
            where.append("exists (select 1 from temp._export_companies e "
                         "where e.source = v.source and e.slug = v.slug)")
        if open_only:
            where.append("v.closed_at is null")
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            where.append("coalesce(v.published_at, v.first_seen_at) >= ?")
            params.append(since)
        if sources:
            where.append(f"v.source in ({','.join('?' * len(sources))})")
            params.extend(sources)
        if countries:
            where.append(f"v.country_code in ({','.join('?' * len(countries))})")
            params.extend(countries)
        if require_domain:
            where.append("c.domain is not null")
        params.append(int(limit))

        sql = f"""
            select v.source, v.slug, v.source_job_id, v.title, v.url,
                   coalesce(v.company_name, c.company_name) as company_name,
                   c.domain, c.careers_url,
                   v.location_raw, v.city, v.country_code, v.workplace,
                   v.department, v.seniority, v.employment_type,
                   v.salary_min, v.salary_max, v.salary_currency,
                   v.published_at, v.published_precision,
                   v.first_seen_at, v.last_seen_at, v.closed_at, v.baseline
            from vacancy v
            left join company c on c.source = v.source and c.slug = v.slug
            where {' and '.join(where)}
            order by coalesce(v.published_at, v.first_seen_at) desc
            limit ?"""
        cur = self.conn.execute(sql, params)
        try:
            while True:
                rows = cur.fetchmany(2_000)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
        finally:
            cur.close()

    def seed_vacancies(self, rows: Iterable[dict], run_id: int | None = None) -> int:
        """Залить историю из снимка портала, НЕ считая её дельтой.

        Смысл: без этого первый собственный прогон объявит новыми все 500 тыс.
        вакансий и утопит настоящий сигнал. `first_seen_at` берётся из даты
        публикации портала, а не из «сейчас».
        """
        inserted = 0
        buffer: list[list[Any]] = []
        columns = (["source", "slug", "source_job_id"] + VACANCY_FIELDS
                   + ["first_seen_at", "last_seen_at", "first_seen_run", "baseline"])
        placeholders = ",".join("?" * len(columns))

        def flush() -> None:
            nonlocal inserted
            if not buffer:
                return
            with self.conn:
                self.conn.executemany(
                    f"insert or ignore into vacancy ({','.join(columns)}) "
                    f"values ({placeholders})", buffer)
            inserted += len(buffer)
            buffer.clear()

        for row in rows:
            source, slug = row.get("source"), row.get("source_company_slug") or row.get("slug")
            job_id = row.get("source_job_id") or row.get("id")
            if not (source and slug and job_id):
                continue
            published = row.get("published_at")
            seen_at = published or row.get("cache_fetched_at") or row.get("created_at") or utcnow()
            buffer.append([
                source, slug, str(job_id),
                row.get("vacancy_title") or row.get("title"),
                row.get("vacancy_url") or row.get("url"), None,
                row.get("company_name"),
                row.get("location") or row.get("location_raw"),
                row.get("city"), None,
                row.get("country_code"), None, None, None, None,
                row.get("salary_from"), row.get("salary_to"), row.get("salary_currency"),
                published, "exact" if published else "none",
                seen_at, row.get("last_seen_at") or seen_at, run_id,
                # История портала — это база отсчёта, а не наше наблюдение.
                # Строки без даты публикации не должны стать «свежим наймом».
                0 if published else 1,
            ])
            if len(buffer) >= 5_000:
                flush()
        flush()
        return inserted

    # --- прогоны --------------------------------------------------------------

    def start_run(self, sources: Sequence[str], geo: str, planned: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "insert into run (started_at, sources, geo, companies_planned) values (?,?,?,?)",
                (utcnow(), ",".join(sources), geo, planned))
        return int(cur.lastrowid)

    def progress_run(self, run_id: int, totals: dict[str, int]) -> None:
        """Промежуточная запись счётчиков прогона.

        Без неё UI показывает «0 из 900» до самого конца, и длинный обход выглядит
        зависшим. Пишется раз в несколько компаний — не на каждой, чтобы не гонять
        транзакцию впустую.
        """
        with self.conn:
            self.conn.execute("""
                update run set companies_ok=?, companies_failed=?, vacancies_seen=?,
                       vacancies_new=?, vacancies_closed=? where id=?""",
                (totals.get("companies_ok", 0), totals.get("companies_failed", 0),
                 totals.get("vacancies_seen", 0), totals.get("vacancies_new", 0),
                 totals.get("vacancies_closed", 0), run_id))

    def finish_run(self, run_id: int, status: str, totals: dict[str, int],
                   error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""
                update run set finished_at=?, status=?, companies_ok=?, companies_failed=?,
                       vacancies_seen=?, vacancies_new=?, vacancies_closed=?,
                       http_requests=?, http_retries=?, error=? where id=?""",
                (utcnow(), status, totals.get("companies_ok", 0),
                 totals.get("companies_failed", 0), totals.get("vacancies_seen", 0),
                 totals.get("vacancies_new", 0), totals.get("vacancies_closed", 0),
                 totals.get("http_requests", 0), totals.get("http_retries", 0),
                 error, run_id))

    def record_source(self, run_id: int, source: str, stats: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("""
                insert into run_source (run_id, source, companies_ok, companies_failed,
                                        vacancies, vacancies_new, sample_error,
                                        truncated, note)
                values (?,?,?,?,?,?,?,?,?)
                on conflict(run_id, source) do update set
                    companies_ok=excluded.companies_ok,
                    companies_failed=excluded.companies_failed,
                    vacancies=excluded.vacancies,
                    vacancies_new=excluded.vacancies_new,
                    sample_error=excluded.sample_error,
                    truncated=excluded.truncated,
                    note=excluded.note""",
                (run_id, source, stats.get("companies_ok", 0), stats.get("companies_failed", 0),
                 stats.get("vacancies", 0), stats.get("vacancies_new", 0),
                 stats.get("sample_error"), stats.get("truncated", 0), stats.get("note")))

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with closing(self.conn.execute(
                "select * from run order by id desc limit ?", (limit,))) as cur:
            return cur.fetchall()

    def run_sources(self, run_id: int) -> list[sqlite3.Row]:
        with closing(self.conn.execute(
                "select * from run_source where run_id=? order by source", (run_id,))) as cur:
            return cur.fetchall()

    # --- сигналы --------------------------------------------------------------

    def hiring_signals(self, days: int = 30, min_new: int = 1, max_new: int | None = None,
                       countries: Sequence[str] | None = None,
                       require_domain: bool = False, limit: int = 500) -> list[dict]:
        """Компании, у которых за последние N дней появились вакансии.

        Свежесть считается по `published_at`, а где его нет (BambooHR, Rippling,
        часть Workday) — по `first_seen_at`, то есть по моменту, когда вакансию
        впервые увидел НАШ парсер. Это честная замена: она не выдумывает дату,
        но и не выбрасывает источник целиком.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        params: list[Any] = [since]
        geo = ""
        if countries:
            geo = f"and v.country_code in ({','.join('?' * len(countries))})"
            params.extend(countries)
        domain_filter = "and c.domain is not null" if require_domain else ""

        # Страны берутся из ОТОБРАННЫХ вакансий, а не из карточки компании в реестре.
        # Иначе фильтр выглядит сломанным: у компании в реестре стоит `gb`, вакансия
        # под фильтром — американская, и при выборе «только US» в списке видно `gb`.
        #
        # Заголовки и ссылки здесь НЕ собираются, хотя раньше собирались через
        # group_concat. Это стоило 31 секунды на первом запросе: у джоб-борда
        # с тысячами вакансий склеивалась строка на мегабайты, из которой потом
        # брались пять первых. Браузер столько не ждёт и рвёт запрос с
        # «Failed to fetch». Примеры вакансий добираются отдельным запросом
        # и только для тех компаний, что реально попали в выдачу.
        body = f"""
            from vacancy v
            join company c on c.source = v.source and c.slug = v.slug
            where v.closed_at is null
              -- базовый срез первого обхода сигналом не считается: без даты
              -- публикации он выглядел бы пачкой «только что появившихся»
              and (v.published_at is not null or v.baseline = 0)
              and v.signal_at >= ?
              {geo} {domain_filter}
            group by c.source, c.slug
            having count(*) >= ? and (? is null or count(*) <= ?)
        """
        # Верхний порог отсекает не работодателей, а джоб-борды и кадровые агентства:
        # 2400 новых вакансий за две недели — это не «компания нанимает», это площадка.
        having_params = [min_new, max_new, max_new]

        sql = f"""
            select c.source, c.slug, c.company_name, c.domain, c.domain_source,
                   c.site_url, c.careers_url, c.country_code as company_country,
                   count(*)                              as new_jobs,
                   max(v.signal_at)                      as latest_at,
                   group_concat(distinct v.country_code) as job_countries
            {body}
            order by new_jobs desc, latest_at desc
            limit ?
        """
        with closing(self.conn.execute(sql, [*params, *having_params, limit])) as cur:
            rows = [dict(r) for r in cur.fetchall()]

        # Сколько всего компаний прошло фильтр — до отсечения лимитом. Без этого
        # числа выдача всегда упирается в потолок, и по ней не видно, что фильтр
        # вообще что-то изменил.
        with closing(self.conn.execute(
                f"select count(*), coalesce(sum(n), 0) from "
                f"(select count(*) as n {body})",
                [*params, *having_params])) as cur:
            total, total_jobs = cur.fetchone()

        samples = self._signal_samples(rows, since, countries)
        for row in rows:
            key = (row["source"], row["slug"])
            row["titles"], row["urls"] = samples.get(key, ([], []))
            row["job_countries"] = [c for c in (row.get("job_countries") or "").split(",") if c]
            row["total_matched"] = total
            # Сколько вакансий стоит за этими компаниями — чтобы кнопка выгрузки
            # заранее говорила, сколько строк будет в файле, а не удивляла после.
            row["total_jobs"] = total_jobs
        return rows

    def _signal_samples(self, rows: list[dict], since: str,
                        countries: Sequence[str] | None, per_company: int = 5
                        ) -> dict[tuple[str, str], tuple[list[str], list[str]]]:
        """По несколько свежих вакансий на компанию — только для строк выдачи.

        Отдельным запросом, а не агрегатом в основном: там пришлось бы склеивать
        все заголовки компании ради пяти показанных.
        """
        if not rows:
            return {}
        out: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        geo = ""
        geo_params: list[Any] = []
        if countries:
            geo = f"and country_code in ({','.join('?' * len(countries))})"
            geo_params = list(countries)

        # По одному маленькому запросу на компанию — ровно по индексу (source, slug).
        # Пробовал иначе: один запрос с OR-цепочкой и оконной функцией по всей
        # таблице занимал 26 секунд, из-за чего браузер рвал запрос и показывал
        # «Failed to fetch». Триста индексных выборок по пять строк — доли секунды.
        sql = f"""
            select title, url from vacancy
            where source = ? and slug = ? and closed_at is null
              and (published_at is not null or baseline = 0)
              and signal_at >= ? {geo}
            order by signal_at desc
            limit {int(per_company)}"""
        for row in rows:
            key = (row["source"], row["slug"])
            titles: list[str] = []
            urls: list[str] = []
            with closing(self.conn.execute(
                    sql, [row["source"], row["slug"], since, *geo_params])) as cur:
                for title, url in cur.fetchall():
                    if title:
                        titles.append(title)
                    if url:
                        urls.append(url)
            out[key] = (titles, urls)
        return out

    def overview(self) -> dict[str, Any]:
        def one(sql: str, *params: Any) -> Any:
            with closing(self.conn.execute(sql, params)) as cur:
                row = cur.fetchone()
                return row[0] if row else None

        by_source = []
        with closing(self.conn.execute("""
                select c.source,
                       count(distinct c.slug)                              as companies,
                       sum(case when c.active = 1 then 1 else 0 end)       as active,
                       (select count(*) from vacancy v
                         where v.source = c.source and v.closed_at is null) as open_jobs
                from company c group by c.source order by c.source""")) as cur:
            by_source = [dict(r) for r in cur.fetchall()]

        return {
            "companies": one("select count(*) from company") or 0,
            "companies_active": one("select count(*) from company where active=1") or 0,
            "companies_with_domain": one("select count(*) from company where domain is not null") or 0,
            "vacancies_open": one("select count(*) from vacancy where closed_at is null") or 0,
            "vacancies_total": one("select count(*) from vacancy") or 0,
            "last_run": (lambda r: dict(r) if r else None)(
                (lambda c: c.fetchone())(self.conn.execute(
                    "select * from run order by id desc limit 1"))),
            "by_source": by_source,
        }


def read_jsonl(path: Path) -> Iterable[dict]:
    import gzip
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
