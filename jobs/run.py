#!/usr/bin/env python
"""CLI единого парсера вакансий.

    python jobs/run.py import-snapshot          снимок портала → локальный реестр и история
    python jobs/run.py run --limit 200          обход компаний, запись дельты
    python jobs/run.py feeds --pages 3          ленты-агрегаторы: покрытие вне ATS
    python jobs/run.py signals --days 30        компании с сигналом найма
    python jobs/run.py export --out out.csv     выгрузить собранные вакансии в CSV
    python jobs/run.py status                   что в базе и как отработали адаптеры

Прогон устроен так, чтобы его можно было прервать и продолжить: компании берутся
в порядке «кого дольше всех не опрашивали», результат каждой пишется сразу.
Ctrl+C закрывает прогон статусом `interrupted`, а не теряет всё сделанное —
портальный парсер на ошибке одной компании ронял весь заход.
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import config, console                                  # noqa: E402
from jobs import bridge, discover, domains, feeds, sources       # noqa: E402
from jobs.fetch import FetchError, Http                          # noqa: E402
from jobs.normalize import GEO_PRESETS                           # noqa: E402
from jobs.store import Store, read_jsonl                         # noqa: E402

console.init()


def db_path() -> Path:
    return config.data_dir() / "jobs" / "jobs.db"


def latest_snapshot() -> Path | None:
    base = config.data_dir() / "portal_snapshot"
    if not base.exists():
        return None
    days = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    return days[-1] if days else None


# --- import-snapshot ----------------------------------------------------------

def cmd_import(args: argparse.Namespace) -> int:
    snapshot = Path(args.dir) if args.dir else latest_snapshot()
    if not snapshot or not snapshot.exists():
        print("Снимок портала не найден. Сначала: python jobs/portal_snapshot.py")
        return 1

    companies_file = snapshot / "companies.jsonl"
    vacancies_file = snapshot / "vacancies_fresh.jsonl.gz"
    print(f"Импорт из {snapshot}")

    with Store(db_path()) as store:
        rows, resolved = [], 0
        for row in read_jsonl(companies_file):
            if row.get("source") not in sources.REGISTRY:
                continue                       # jobhive и прочие мета-источники — не адаптеры
            domain, domain_source = domains.resolve([
                ("portal_site_url", row.get("company_site_url")),
                ("careers_url", row.get("careers_url")),
            ])
            if domain:
                resolved += 1
            rows.append({
                "source": row["source"], "slug": row["slug"],
                "company_name": row.get("company_name"),
                "careers_url": row.get("careers_url"),
                "site_url": row.get("company_site_url"),
                "domain": domain, "domain_source": domain_source,
                "country_code": row.get("country_code"),
                "origin": "portal_snapshot",
            })
        imported = store.upsert_companies(rows)
        pct = 100.0 * resolved / imported if imported else 0.0
        print(f"  компаний в реестре: {imported}  (домен известен у {resolved}, {pct:.1f}%)")

        if vacancies_file.exists():
            # Мета-источники портала (jobhive и др.) адаптерами не являются, но их
            # URL'ы содержат площадку и slug — бесплатное пополнение реестра теми
            # компаниями, которых в нём иначе не будет вовсе.
            found = list(discover.from_rows(read_jsonl(vacancies_file)))
            fresh = [c for c in found if c["source"] in sources.REGISTRY]
            added = store.upsert_companies(fresh)
            by_source: dict[str, int] = {}
            for c in fresh:
                by_source[c["source"]] = by_source.get(c["source"], 0) + 1
            print(f"  найдено по URL: {added} компаний "
                  f"({', '.join(f'{k} {v}' for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]))})")

        if args.with_history and vacancies_file.exists():
            print("  заливаю историю вакансий (без неё первый прогон объявит новыми все)...")
            seeded = store.seed_vacancies(read_jsonl(vacancies_file))
            print(f"  вакансий в истории: {seeded}")
        elif args.with_history:
            print(f"  !  {vacancies_file.name} не найден — история не залита")

        overview = store.overview()
        print(f"\n  итого: {overview['companies']} компаний, "
              f"{overview['vacancies_total']} вакансий, "
              f"домен у {overview['companies_with_domain']}")
    return 0


# --- run ----------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    codes = sources.codes(args.sources)
    http = Http(delay=args.delay, timeout=args.timeout)

    with Store(db_path()) as store:
        planned = store.companies(sources=codes, limit=args.limit)
        if not planned:
            print("В реестре нет компаний. Сначала: python jobs/run.py import-snapshot")
            return 1

        run_id = store.start_run(codes, args.geo, len(planned))
        print(f"Прогон #{run_id}: {len(planned)} компаний, источники: {', '.join(codes)}")
        if args.dry_run:
            by_source: dict[str, int] = {}
            for row in planned:
                by_source[row["source"]] = by_source.get(row["source"], 0) + 1
            for code, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
                print(f"  {code:<18} {n:>6} компаний")
            store.finish_run(run_id, "dry-run", {})
            return 0

        totals = {"companies_ok": 0, "companies_failed": 0, "vacancies_seen": 0,
                  "vacancies_new": 0, "vacancies_closed": 0}
        per_source: dict[str, dict] = {code: {"companies_ok": 0, "companies_failed": 0,
                                              "vacancies": 0, "vacancies_new": 0,
                                              "sample_error": None, "truncated": 0,
                                              "note": None} for code in codes}
        status = "completed"
        try:
            for index, row in enumerate(planned, 1):
                code, slug = row["source"], row["slug"]
                adapter = sources.get(code)
                stats = per_source[code]
                try:
                    vacancies = adapter.fetch(http, slug)
                except (FetchError, ValueError, KeyError, TypeError) as exc:
                    message = str(exc)[:300]
                    store.mark_company(code, slug, ok=False, error=message)
                    totals["companies_failed"] += 1
                    stats["companies_failed"] += 1
                    stats["sample_error"] = stats["sample_error"] or message
                    if args.verbose:
                        print(f"  [{index}/{len(planned)}] {code}/{slug}: {message}")
                    continue

                if adapter.last_note:
                    # Обрезание пагинации не должно быть тихим: неполный обход
                    # иначе неотличим от полного.
                    stats["truncated"] += 1
                    stats["note"] = adapter.last_note
                    print(f"  [{index}/{len(planned)}] {code}/{slug}: {adapter.last_note}")

                delta = store.apply_company_result(
                    code, slug, vacancies, run_id=run_id,
                    baseline=row["last_ok_at"] is None)
                store.mark_company(code, slug, ok=True)
                _learn_domain(store, code, slug, vacancies, row["company_name"])

                totals["companies_ok"] += 1
                totals["vacancies_seen"] += delta.seen
                totals["vacancies_new"] += delta.new
                totals["vacancies_closed"] += delta.closed
                stats["companies_ok"] += 1
                stats["vacancies"] += delta.seen
                stats["vacancies_new"] += delta.new

                if delta.new or delta.closed or args.verbose:
                    print(f"  [{index}/{len(planned)}] {code}/{slug}: "
                          f"{delta.seen} вакансий, +{delta.new} новых, "
                          f"−{delta.closed} закрытых"
                          + (f", {delta.reopened} открылись заново" if delta.reopened else ""),
                          flush=True)

                if index % 20 == 0:
                    # чтобы вкладка «Вакансии» показывала живой прогресс, а не 0 из N
                    store.progress_run(run_id, totals)
                    for src, stat in per_source.items():
                        store.record_source(run_id, src, stat)
        except KeyboardInterrupt:
            status = "interrupted"
            print("\nПрервано пользователем — то, что успели, сохранено.")
        except Exception:                                    # noqa: BLE001
            status = "failed"
            traceback.print_exc()
        finally:
            totals["http_requests"] = http.stats.requests
            totals["http_retries"] = http.stats.retries
            for code, stats in per_source.items():
                store.record_source(run_id, code, stats)
            store.finish_run(run_id, status, totals)

        print(f"\nПрогон #{run_id} — {status}")
        print(f"  компаний: {totals['companies_ok']} ок, {totals['companies_failed']} ошибок")
        print(f"  вакансий: {totals['vacancies_seen']} видно, "
              f"+{totals['vacancies_new']} новых, −{totals['vacancies_closed']} закрыто")
        print(f"  HTTP: {http.stats.requests} запросов, {http.stats.retries} ретраев, "
              f"{http.stats.seconds_waiting:.0f} с в паузах")
        errors = {str(k): v for k, v in sorted(http.stats.by_status.items()) if k >= 400}
        if errors:
            print(f"  ответы 4xx/5xx: {errors}")
    return 0


def _learn_domain(store: Store, source: str, slug: str, vacancies,
                  company_name: str | None = None) -> None:
    """Домен компании из того, что уже пришло в ответе — без единого лишнего запроса.

    Порядок = порядок доверия. Сначала явные поля (Teamtailor отдаёт sameAs из
    schema.org, Recruitee — кастомный careers_url), и только потом текст вакансии,
    да и то лишь при совпадении домена с названием компании. Записанный домен
    никогда не перетирается: `upsert_companies` бережёт уже известный.
    """
    for vacancy in vacancies:
        domain, domain_source = domains.resolve([
            ("schema_org", vacancy.company_site_url),
            ("careers_url", vacancy.careers_url),
        ])
        if not domain:
            # Имя компании есть не во всех ответах (Greenhouse отдаёт, Lever нет),
            # поэтому фолбэк на имя из реестра — без имени сверять домен не с чем.
            domain, domain_source = domains.from_description(
                vacancy.company_name or company_name, vacancy.description)
        if domain:
            store.upsert_companies([{
                "source": source, "slug": slug, "domain": domain,
                "domain_source": domain_source, "origin": "adapter"}])
            return


# --- feeds --------------------------------------------------------------------

def cmd_feeds(args: argparse.Namespace) -> int:
    """Ленты-агрегаторы: покрытие компаний, которых нет ни на одном ATS-борде."""
    codes = feeds.codes(args.feeds)
    http = Http(delay=args.delay, timeout=args.timeout)

    with Store(db_path()) as store:
        run_id = store.start_run(codes, args.geo, len(codes))
        print(f"Прогон лент #{run_id}: {', '.join(codes)}, страниц на ленту: {args.pages}")
        totals = {"companies_ok": 0, "companies_failed": 0, "vacancies_seen": 0,
                  "vacancies_new": 0, "vacancies_closed": 0}
        status = "completed"
        try:
            for code in codes:
                feed = feeds.get(code)
                try:
                    vacancies = feed.fetch(http, pages=args.pages)
                except (FetchError, ValueError, KeyError, TypeError) as exc:
                    message = str(exc)[:300]
                    totals["companies_failed"] += 1
                    store.record_source(run_id, code, {"companies_failed": 1,
                                                       "sample_error": message})
                    print(f"  {code}: ошибка — {message}", flush=True)
                    continue

                # Компании лент живут в том же реестре, но обходу не подлежат:
                # адаптера у них нет, лента забирается целиком.
                companies = {}
                for vacancy in vacancies:
                    companies.setdefault((vacancy.source, vacancy.slug), {
                        "source": vacancy.source, "slug": vacancy.slug,
                        "company_name": vacancy.company_name,
                        "country_code": vacancy.country_code,
                        "origin": "feed"})
                store.upsert_companies(companies.values())
                stats = store.apply_feed_result(vacancies, run_id=run_id)

                totals["companies_ok"] += stats["companies"]
                totals["vacancies_seen"] += stats["seen"]
                totals["vacancies_new"] += stats["new"]
                store.record_source(run_id, code, {
                    "companies_ok": stats["companies"], "vacancies": stats["seen"],
                    "vacancies_new": stats["new"],
                    "note": "лента: вакансии не закрываются по разнице множеств"})
                print(f"  {code:<12} {stats['seen']:>5} вакансий, "
                      f"{stats['companies']:>4} компаний, +{stats['new']} новых", flush=True)
        except KeyboardInterrupt:
            status = "interrupted"
            print("\nПрервано — сделанное сохранено.")
        finally:
            totals["http_requests"] = http.stats.requests
            totals["http_retries"] = http.stats.retries
            store.finish_run(run_id, status, totals)

        print(f"\nПрогон лент #{run_id} — {status}")
        print(f"  вакансий {totals['vacancies_seen']}, из них новых {totals['vacancies_new']}, "
              f"компаний {totals['companies_ok']}")
    return 0


# --- export -------------------------------------------------------------------

EXPORT_COLUMNS = [
    ("source", "площадка"), ("slug", "slug"), ("source_job_id", "id вакансии"),
    ("company_name", "компания"), ("domain", "домен"), ("title", "вакансия"),
    ("url", "ссылка"), ("department", "отдел"), ("seniority", "уровень"),
    ("employment_type", "занятость"), ("workplace", "формат"),
    ("location_raw", "локация"), ("city", "город"), ("country_code", "страна"),
    ("salary_min", "зп от"), ("salary_max", "зп до"), ("salary_currency", "валюта"),
    ("published_at", "опубликована"), ("published_precision", "точность даты"),
    ("first_seen_at", "впервые увидели"), ("last_seen_at", "последний раз видели"),
    ("closed_at", "закрыта"), ("baseline", "базовый срез"), ("careers_url", "карьерная страница"),
]


SIGNAL_COLUMNS = [
    ("company_name", "компания"), ("domain", "домен"), ("source", "площадка"),
    ("slug", "slug"), ("new_jobs", "новых вакансий"), ("latest_at", "последняя"),
    ("job_countries_joined", "гео вакансий"), ("company_country", "гео компании"),
    ("careers_url", "карьерная страница"), ("titles_joined", "роли"),
    ("statement", "утверждение"), ("evidence_url", "источник"),
    ("signal_score", "signal_score"), ("readiness", "готовность"),
    ("domain_source", "откуда домен"),
]


def cmd_export(args: argparse.Namespace) -> int:
    geo = GEO_PRESETS.get(args.geo)
    if geo is None:
        print(f"Неизвестный гео-пресет {args.geo!r}. Есть: {', '.join(GEO_PRESETS)}")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with Store(db_path()) as store, open(out, "w", encoding="utf-8-sig", newline="") as fh:
        # utf-8-sig: без BOM Excel открывает кириллицу кракозябрами, а файл идёт
        # человеку в таблицу, а не в скрипт.
        writer = csv.writer(fh, delimiter=args.delimiter)
        writer.writerow([label for _, label in EXPORT_COLUMNS])
        for row in store.vacancies(days=args.days,
                                   sources=args.sources or None,
                                   countries=sorted(geo) or None,
                                   open_only=not args.include_closed,
                                   require_domain=args.require_domain,
                                   limit=args.limit):
            writer.writerow([row.get(key) for key, _ in EXPORT_COLUMNS])
            written += 1

    size = out.stat().st_size / 1024 / 1024
    print(f"Выгружено вакансий: {written}")
    print(f"CSV: {out}  ({size:.1f} МБ)")
    if written >= args.limit:
        print(f"  ВНИМАНИЕ: упёрлись в --limit {args.limit}, часть строк не попала в файл.")
    return 0


# --- signals ------------------------------------------------------------------

def cmd_signals(args: argparse.Namespace) -> int:
    geo = GEO_PRESETS.get(args.geo)
    if geo is None:
        print(f"Неизвестный гео-пресет {args.geo!r}. Есть: {', '.join(GEO_PRESETS)}")
        return 1

    with Store(db_path()) as store:
        rows = store.hiring_signals(days=args.days, min_new=args.min_new,
                                    max_new=args.max_new,
                                    countries=sorted(geo) or None,
                                    require_domain=args.require_domain,
                                    limit=args.limit)
    if not rows:
        print("Сигналов нет. Либо прогон ещё не делался, либо окно слишком узкое.")
        return 0

    rows = bridge.evaluate_rows(rows, offer_link=args.offer_link)

    total = rows[0].get("total_matched") or len(rows)
    tail = f" (показано {len(rows)})" if total > len(rows) else ""
    print(f"Компаний с сигналом найма за {args.days} дн.: {total}{tail}")
    for row in rows[:20]:
        domain = row["domain"] or "домен неизвестен"
        print(f"  {row['new_jobs']:>3} вак.  {(row['company_name'] or row['slug'])[:42]:<44}"
              f" {domain:<28} {row['source']:<16} score {row['signal_score']:>3}")
    if len(rows) > 20:
        print(f"  ... и ещё {len(rows) - 20}")

    if not args.offer_link:
        print("\n  Связка «сигнал → боль → оффер» не задана (--offer-link), поэтому "
              "рубрика режет ценность до 3 из 5. Это ожидаемо для сырой выгрузки.")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["company", "domain", "source", "slug", "new_jobs",
                             "latest_at", "country", "careers_url", "titles",
                             "statement", "evidence_url", "signal_score", "readiness",
                             "domain_source"])
            for row in rows:
                writer.writerow([row["company_name"], row["domain"], row["source"],
                                 row["slug"], row["new_jobs"], row["latest_at"],
                                 row["company_country"], row["careers_url"],
                                 "; ".join(row["titles"]), row["statement"],
                                 row["evidence_url"], row["signal_score"],
                                 row["readiness"], row["domain_source"]])
        print(f"\nCSV: {out}")
    return 0


# --- status -------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    with Store(db_path()) as store:
        overview = store.overview()
        print(f"База: {db_path()}")
        print(f"  компаний: {overview['companies']} "
              f"(активных {overview['companies_active']}, "
              f"с доменом {overview['companies_with_domain']})")
        print(f"  вакансий: {overview['vacancies_total']} "
              f"(открытых {overview['vacancies_open']})")

        print("\n  по источникам:")
        for row in overview["by_source"]:
            print(f"    {row['source']:<18} компаний {row['companies']:>6} "
                  f"(активных {row['active']:>6})  открытых вакансий {row['open_jobs']:>7}")

        runs = store.runs(limit=args.runs)
        if runs:
            print("\n  последние прогоны:")
            for run in runs:
                print(f"    #{run['id']:<4} {str(run['started_at'])[:16]}  {run['status']:<12}"
                      f" компаний {run['companies_ok']}/{run['companies_planned']}"
                      f"  +{run['vacancies_new']} новых  −{run['vacancies_closed']} закрыто")
            print("\n  адаптеры в последнем прогоне:")
            for row in store.run_sources(runs[0]["id"]):
                mark = "ок " if row["companies_failed"] == 0 else "!! "
                tail = ""
                if row["sample_error"]:
                    tail = f"  {row['sample_error'][:60]}"
                elif row["truncated"]:
                    tail = f"  обрезано у {row['truncated']} компаний"
                print(f"    {mark}{row['source']:<18} ок {row['companies_ok']:>5}, "
                      f"ошибок {row['companies_failed']:>5}, вакансий {row['vacancies']:>7}"
                      f"{tail}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Единый парсер вакансий")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import-snapshot", help="снимок портала → локальный реестр")
    p_import.add_argument("--dir", help="каталог снимка (по умолчанию последний)")
    p_import.add_argument("--with-history", action="store_true", default=True,
                          help="залить историю вакансий (по умолчанию да)")
    p_import.add_argument("--no-history", dest="with_history", action="store_false")
    p_import.set_defaults(func=cmd_import)

    p_run = sub.add_parser("run", help="обход компаний")
    p_run.add_argument("--sources", nargs="*", help="коды источников (по умолчанию все)")
    p_run.add_argument("--limit", type=int, default=200, help="сколько компаний за прогон")
    p_run.add_argument("--geo", default="en+eu", choices=sorted(GEO_PRESETS))
    p_run.add_argument("--delay", type=float, default=1.0, help="пауза на хост, секунд")
    p_run.add_argument("--timeout", type=float, default=25.0)
    p_run.add_argument("--dry-run", action="store_true", help="показать план, не ходить в сеть")
    p_run.add_argument("--verbose", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_sig = sub.add_parser("signals", help="компании с сигналом найма")
    p_sig.add_argument("--days", type=int, default=30)
    p_sig.add_argument("--min-new", type=int, default=1)
    p_sig.add_argument("--max-new", type=int, default=None,
                       help="верхний порог: отсекает джоб-борды и кадровые агентства")
    p_sig.add_argument("--offer-link",
                       help="связка «наём → боль → оффер»; без неё рубрика режет ценность до 3")
    p_sig.add_argument("--geo", default="en+eu", choices=sorted(GEO_PRESETS))
    p_sig.add_argument("--require-domain", action="store_true",
                       help="только компании с известным доменом (готовые к письму)")
    p_sig.add_argument("--limit", type=int, default=500)
    p_sig.add_argument("--out", help="выгрузить CSV")
    p_sig.set_defaults(func=cmd_signals)

    p_feeds = sub.add_parser("feeds", help="ленты-агрегаторы: покрытие вне ATS-бордов")
    p_feeds.add_argument("--feeds", nargs="*", help="коды лент (по умолчанию все)")
    p_feeds.add_argument("--pages", type=int, default=1, help="страниц на ленту")
    p_feeds.add_argument("--geo", default="en+eu", choices=sorted(GEO_PRESETS))
    p_feeds.add_argument("--delay", type=float, default=1.0)
    p_feeds.add_argument("--timeout", type=float, default=25.0)
    p_feeds.set_defaults(func=cmd_feeds)

    p_exp = sub.add_parser("export", help="выгрузить собранные вакансии в CSV")
    p_exp.add_argument("--out", default="data/jobs/vacancies.csv")
    p_exp.add_argument("--days", type=int, default=None,
                       help="только вакансии свежее N дней (по умолчанию все)")
    p_exp.add_argument("--sources", nargs="*", help="фильтр по площадкам")
    p_exp.add_argument("--geo", default="all", choices=sorted(GEO_PRESETS))
    p_exp.add_argument("--require-domain", action="store_true")
    p_exp.add_argument("--include-closed", action="store_true",
                       help="включить закрытые вакансии")
    p_exp.add_argument("--limit", type=int, default=200_000)
    p_exp.add_argument("--delimiter", default=",",
                       help="разделитель CSV; для русского Excel часто нужен ;")
    p_exp.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status", help="состояние базы и адаптеров")
    p_status.add_argument("--runs", type=int, default=5)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
