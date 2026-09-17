"""Мост к базе портала (readonly-роль из research pack).

Правила из AGENTS.md пака соблюдаются кодом, а не дисциплиной:
- сессия принудительно read-only (default_transaction_read_only=on) —
  INSERT/UPDATE/DELETE упадут на уровне Postgres, что бы ни написал вызывающий;
- statement_timeout, чтобы кривой запрос не висел на чужой продовой базе;
- только public-схема, только SELECT.

Подключение: PORTAL_DB_URL в .env (в git не попадает). Это ОСНОВНАЯ операционная
база портала — аналитика Instantly там частично (каталог кампаний есть,
события пусты), лид-уровень мы по-прежнему берём из Instantly API сами.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import psycopg

from . import config

log = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 30_000
BATCH = 5_000


class PortalNotConfigured(RuntimeError):
    pass


def connect() -> psycopg.Connection:
    url = config.env("PORTAL_DB_URL")
    if not url:
        raise PortalNotConfigured(
            "PORTAL_DB_URL не задан в .env — подключение к порталу не настроено. "
            "Строка подключения лежит в research pack (.codex/config.toml).")
    return psycopg.connect(
        url, connect_timeout=15,
        options=(f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                 "-c default_transaction_read_only=on"),
    )


def _rows(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def projects(conn: psycopg.Connection) -> list[dict]:
    """Справочник проектов портала: канонические клиенты, статусы, команда."""
    with conn.cursor() as cur:
        cur.execute("""
            select id::text, name, client, status, specialist, manager,
                   project_type, lead_source, region, created_at
            from public.projects
            order by created_at desc""")
        return _rows(cur)


def campaign_catalog(conn: psycopg.Connection) -> list[dict]:
    """Каталог кампаний Instantly, который портал синхронизирует сам.

    Ценность против нашего пулла: он копился давно и содержит кампании,
    уже удалённые или недоступные через наш ключ.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select id::text, name, status, timestamp_created,
                   leads_count, contacted_count, emails_sent_count,
                   reply_count_unique, bounced_count, unsubscribed_count,
                   analytics_synced_at
            from public.instantly_campaign_catalog""")
        return _rows(cur)


def validation_lookup(conn: psycopg.Connection,
                      emails: Iterable[str]) -> dict[str, dict]:
    """История валидации по адресам: email → {result, quality, is_catch_all, checked_at}.

    Пустой ответ по адресу — это тоже ответ: адрес в валидатор портала не попадал.
    Именно так нашлась причина bounce на англ-контуре.
    """
    unique = sorted({(e or "").strip().lower() for e in emails if e})
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        for i in range(0, len(unique), BATCH):
            cur.execute("""
                select email_normalized, result, quality, is_catch_all, completed_at
                from public.email_validation_queue
                where email_normalized = any(%s) and status = 'completed'""",
                (unique[i:i + BATCH],))
            for email, result, quality, catch_all, at in cur.fetchall():
                out[email] = {"result": result, "quality": quality,
                              "is_catch_all": catch_all, "checked_at": at}
    return out


def domain_cache_lookup(conn: psycopg.Connection,
                        domains: Iterable[str]) -> dict[str, dict]:
    """Кэш доменов валидатора: MX, catch-all, disposable."""
    unique = sorted({(d or "").strip().lower() for d in domains if d})
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        for i in range(0, len(unique), BATCH):
            cur.execute("""
                select domain, mx_found, is_catch_all, is_disposable, checked_at
                from public.email_validation_domain_cache
                where domain = any(%s)""", (unique[i:i + BATCH],))
            for domain, mx, catch_all, disposable, at in cur.fetchall():
                out[domain] = {"mx_found": mx, "is_catch_all": catch_all,
                               "is_disposable": disposable, "checked_at": at}
    return out


def hiring_signals(conn: psycopg.Connection, domains: Iterable[str],
                   fresh_days: int = 45) -> dict[str, list[dict]]:
    """Сигналы найма для англ-компаний из eng_hiring_vacancies (по домену сайта)."""
    unique = sorted({(d or "").strip().lower() for d in domains if d})
    out: dict[str, list[dict]] = {}
    if not unique:
        return out
    with conn.cursor() as cur:
        for i in range(0, len(unique), BATCH):
            cur.execute("""
                select company_domain, title, location, url, published_at
                from public.eng_hiring_vacancies
                where lower(company_domain) = any(%s)
                  and published_at > now() - make_interval(days => %s)
                order by published_at desc""",
                (unique[i:i + BATCH], fresh_days))
            for domain, title, location, url, published in cur.fetchall():
                out.setdefault(domain, []).append({
                    "title": title, "location": location,
                    "url": url, "published_at": published})
    return out
