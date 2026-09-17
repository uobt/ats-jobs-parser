#!/usr/bin/env python
"""Этап 0: снимок парсера вакансий с портала. ТОЛЬКО ЧТЕНИЕ.

Портал не меняется ничем: ни строки не удаляется, ни строки не добавляется.
Read-only гарантируется не дисциплиной, а кодом:

  1. `lib/portal.py` открывает сессию с `default_transaction_read_only=on` —
     любой INSERT/UPDATE/DELETE/DDL упадёт на уровне Postgres;
  2. `_guard()` ниже отказывается выполнить запрос, который не начинается
     с `select` или `with`;
  3. до и после снимка снимаются counts исходных таблиц и сравниваются —
     расхождение попадает в манифест как `source_changed`.

Зачем снимок. Портальный парсер — это 756 736 вакансий с 10 площадок и,
главное, **реестр из ~31 тыс. компаний с их ATS и slug'ами**. Реестр и есть
основной актив: он снимает задачу дискавери, которую ни один открытый
репозиторий не решает. Свежие вакансии нужны, чтобы первый прогон своего
парсера уже посчитал дельту, а не ждал второго захода.

Что кладём на диск (data/portal_snapshot/{YYYY-MM-DD}/):

  companies.jsonl          реестр (source, slug) → компания, careers_url, домен, гео
  vacancies_fresh.jsonl.gz вакансии за N дней, метаданные без текста описания
  selected.jsonl           eng_hiring_vacancies — что парсер уже отдавал наружу
  runs.jsonl               eng_hiring_cache_runs — история прогонов
  parser_configs.jsonl     конфиги запусков: контракт парсера
  ats_companies.jsonl      результат дискавери-джобы ats_companies
  schema.sql               DDL исходных таблиц + индексы
  _manifest.json           counts, sha256 каждого файла, сверка «до/после»

Тексты описаний и сырой jsonb по умолчанию НЕ выгружаются: они дают 90% объёма
(3.9 ГБ против ~100 МБ), а сигналу найма нужны компания, тайтл, дата и ссылка.
Нужны тексты — `--with-description`, нужен сырой payload — `--with-raw`.

Запуск:
    python jobs/portal_snapshot.py --dry-run
    python jobs/portal_snapshot.py --fresh-days 90
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import config, console, portal                          # noqa: E402

console.init()

# Таблицы портала, которые относятся к парсеру вакансий. Только чтение.
SOURCE_TABLES = [
    "eng_hiring_cache",
    "eng_hiring_vacancies",
    "eng_hiring_cache_runs",
    "ats_companies",
    "parser_jobs",
]

BATCH = 5_000


class NotReadOnly(RuntimeError):
    pass


def _guard(sql: str) -> str:
    """Предохранитель: в этот файл не должно попасть ничего, кроме чтения."""
    head = sql.strip().lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise NotReadOnly(f"портальный снимок выполняет только SELECT, получено: {sql[:60]!r}")
    return sql


def _rows(conn, sql: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_guard(sql))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def _stream(conn, sql: str, name: str) -> Iterator[dict]:
    """Курсор на стороне сервера: 250 тыс. строк не поднимаются в память целиком."""
    with conn.cursor(name=name) as cur:
        cur.itersize = BATCH
        cur.execute(_guard(sql))
        names = [d[0] for d in cur.description]
        for row in cur:
            yield dict(zip(names, row))


def _assert_read_only(conn) -> None:
    value = _rows(conn, "select current_setting('default_transaction_read_only') as ro")[0]["ro"]
    if value != "on":
        raise NotReadOnly(
            "сессия не read-only — снимок прерван. Ожидалось "
            "default_transaction_read_only=on (его выставляет lib/portal.py)")


def _counts(conn) -> dict[str, int]:
    """Точные counts исходных таблиц: снимаются до и после и сверяются."""
    parts = " union all ".join(
        f"select '{t}' as t, count(*)::bigint as n from public.{t}" for t in SOURCE_TABLES)
    return {r["t"]: int(r["n"]) for r in _rows(conn, parts)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: Iterator[dict], gz: bool = False) -> int:
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8")) if gz else (
        lambda p: open(p, "w", encoding="utf-8"))
    written = 0
    with opener(path) as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            written += 1
    return written


# --- запросы -----------------------------------------------------------------

COMPANIES_SQL = """
select source,
       source_company_slug                                    as slug,
       min(company_name)                                      as company_name,
       min(careers_url)                                       as careers_url,
       min(company_site_url) filter (where company_site_url is not null) as company_site_url,
       mode() within group (order by country_code)            as country_code,
       count(*)                                               as jobs_total,
       count(*) filter (where published_at > now() - interval '90 days') as jobs_90d,
       min(published_at)                                      as first_published_at,
       max(published_at)                                      as last_published_at,
       max(last_seen_at)                                      as last_seen_at
from public.eng_hiring_cache
where source_company_slug is not null
group by source, source_company_slug
order by source, slug
"""

SELECTED_SQL = """
select id, job_id, cache_id, source, source_company_slug, source_job_id,
       company_name, company_site_url, vacancy_title, vacancy_url, careers_url,
       location, city, country, country_code, salary_from, salary_to,
       salary_currency, published_at, created_at
from public.eng_hiring_vacancies
order by created_at desc
"""

RUNS_SQL = "select * from public.eng_hiring_cache_runs order by started_at desc"

PARSER_CONFIGS_SQL = """
select id, parser_type, status, config, total_found, total_parsed,
       created_at, started_at, completed_at, error_message, progress_stage
from public.parser_jobs
where parser_type in ('eng_hiring', 'ats_companies')
order by created_at desc
"""

ATS_COMPANIES_SQL = "select * from public.ats_companies order by created_at desc"


def _vacancies_sql(fresh_days: int, with_description: bool, with_raw: bool) -> str:
    extra = ""
    if with_description:
        extra += ", vacancy_description, company_description"
    if with_raw:
        extra += ", raw"
    # published_at is null — это bamboohr и часть smartrecruiters: даты у них нет
    # в принципе, и отбрасывать их по дате нельзя. Берём по свежести самого кэша.
    return f"""
select id, source, source_company_slug, source_job_id, company_name,
       company_site_url, vacancy_title, vacancy_url, careers_url,
       location, city, country, country_code,
       salary_from, salary_to, salary_currency,
       published_at, last_seen_at, cache_fetched_at, created_at{extra}
from public.eng_hiring_cache
where published_at > now() - interval '{fresh_days} days'
   or (published_at is null and cache_fetched_at > now() - interval '{fresh_days} days')
"""


SCHEMA_COLUMNS_SQL = """
select table_name, ordinal_position, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and table_name in ('eng_hiring_cache','eng_hiring_vacancies','eng_hiring_cache_runs',
                     'ats_companies','parser_jobs')
order by table_name, ordinal_position
"""

SCHEMA_INDEXES_SQL = """
select tablename, indexname, indexdef
from pg_indexes
where schemaname = 'public'
  and tablename in ('eng_hiring_cache','eng_hiring_vacancies','eng_hiring_cache_runs',
                    'ats_companies','parser_jobs')
order by tablename, indexname
"""


def _schema_sql(conn) -> str:
    cols = _rows(conn, SCHEMA_COLUMNS_SQL)
    idx = _rows(conn, SCHEMA_INDEXES_SQL)
    by_table: dict[str, list[dict]] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], []).append(c)

    out = ["-- Снимок DDL парсерных таблиц портала. Справочный файл, не миграция.",
           f"-- Снят {datetime.now(timezone.utc).isoformat()}, только чтение.", ""]
    for table, columns in by_table.items():
        out.append(f"create table public.{table} (")
        lines = []
        for c in columns:
            null = "" if c["is_nullable"] == "YES" else " not null"
            default = f" default {c['column_default']}" if c["column_default"] else ""
            lines.append(f"    {c['column_name']} {c['data_type']}{default}{null}")
        out.append(",\n".join(lines))
        out.append(");")
        out.append("")
    out.append("-- индексы")
    for i in idx:
        out.append(f"{i['indexdef']};")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Снимок парсера вакансий с портала (read-only)")
    parser.add_argument("--fresh-days", type=int, default=90,
                        help="глубина выгрузки вакансий в днях (по умолчанию 90)")
    parser.add_argument("--with-description", action="store_true",
                        help="выгрузить тексты вакансий (объём вырастает примерно в 8 раз)")
    parser.add_argument("--with-raw", action="store_true",
                        help="выгрузить сырой jsonb ответа ATS (самый тяжёлый вариант)")
    parser.add_argument("--out", type=Path, default=None,
                        help="каталог снимка (по умолчанию data/portal_snapshot/<дата>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только посчитать объёмы, ничего не писать на диск")
    args = parser.parse_args()

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = args.out or (config.data_dir() / "portal_snapshot" / day)

    print(f"Снимок портала → {out_dir}")
    print("Режим: только чтение. На портале ничего не создаётся и не удаляется.\n")

    conn = portal.connect()
    try:
        _assert_read_only(conn)
        print("read-only сессия подтверждена (default_transaction_read_only=on)")

        counts_before = _counts(conn)
        for table, n in counts_before.items():
            print(f"  {table:<24} {n:>10,} строк".replace(",", " "))

        fresh_n = _rows(conn, f"select count(*)::bigint as n from ({_vacancies_sql(args.fresh_days, False, False)}) s")[0]["n"]
        companies_n = _rows(conn, f"select count(*)::bigint as n from ({COMPANIES_SQL}) s")[0]["n"]
        print(f"\n  к выгрузке: {companies_n:,} компаний, {fresh_n:,} свежих вакансий "
              f"(за {args.fresh_days} дн.)".replace(",", " "))

        if args.dry_run:
            print("\n--dry-run: на диск ничего не записано.")
            return 0

        out_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, dict[str, Any]] = {}

        jobs = [
            ("companies.jsonl", COMPANIES_SQL, False),
            ("selected.jsonl", SELECTED_SQL, False),
            ("runs.jsonl", RUNS_SQL, False),
            ("parser_configs.jsonl", PARSER_CONFIGS_SQL, False),
            ("ats_companies.jsonl", ATS_COMPANIES_SQL, False),
            ("vacancies_fresh.jsonl.gz",
             _vacancies_sql(args.fresh_days, args.with_description, args.with_raw), True),
        ]

        print()
        for filename, sql, gz in jobs:
            path = out_dir / filename
            cursor_name = "snap_" + filename.split(".")[0]
            n = _write_jsonl(path, _stream(conn, sql, cursor_name), gz=gz)
            size_mb = path.stat().st_size / 1024 / 1024
            files[filename] = {"rows": n, "bytes": path.stat().st_size,
                               "sha256": _sha256(path)}
            print(f"  {filename:<26} {n:>9,} строк  {size_mb:>8.1f} МБ".replace(",", " "))

        schema_path = out_dir / "schema.sql"
        schema_path.write_text(_schema_sql(conn), encoding="utf-8")
        files["schema.sql"] = {"rows": None, "bytes": schema_path.stat().st_size,
                               "sha256": _sha256(schema_path)}
        print(f"  {'schema.sql':<26} {'':>9}       {schema_path.stat().st_size / 1024:>7.1f} КБ")

        counts_after = _counts(conn)
        changed = {t: [counts_before[t], counts_after[t]]
                   for t in SOURCE_TABLES if counts_before[t] != counts_after[t]}

        manifest = {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "mode": "read-only",
            "fresh_days": args.fresh_days,
            "with_description": args.with_description,
            "with_raw": args.with_raw,
            "source_counts_before": counts_before,
            "source_counts_after": counts_after,
            "source_changed": changed,
            "files": files,
        }
        (out_dir / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        print()
        if changed:
            print("ВНИМАНИЕ: counts исходных таблиц изменились во время снимка "
                  "(портал живёт своей жизнью, снимок мог захватить середину чужой записи):")
            for table, (before, after) in changed.items():
                print(f"  {table}: {before} → {after}")
        else:
            print("Сверка «до/после»: исходные таблицы не изменились.")
        print(f"Готово: {out_dir}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
