"""Дискавери компаний из URL вакансий: бесплатный способ пополнить реестр.

Ни одна из 12 площадок не публикует индекса своих клиентов — это не пробел
ресёрча, этого просто не существует. Но URL вакансии сам по себе содержит и
площадку, и slug компании, а URL'ов у нас много: в снимке портала лежит
мета-источник `jobhive`, внутри которого 25 платформ, включая три, которых
в реестре нет вообще (Workday, Rippling, Personio).

То есть дискавери для новых адаптеров уже оплачена — надо только распарсить
то, что и так лежит на диске. Платный SERP-путь (дорки `site:jobs.lever.co`)
остаётся на потом и здесь не используется.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator

# Каждый шаблон извлекает slug ровно так, как его потом ждёт адаптер.
# Workday — составной 'tenant|wdN|siteId', остальные — простой идентификатор.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    # Отрицательный просмотр вперёд обязателен: apply.workable.com/j/04068221FA —
    # это шорткод ВАКАНСИИ, а слаг компании лежит по apply.workable.com/{slug}.
    # Без него в реестр попадают десятки тысяч несуществующих «компаний».
    ("workable", re.compile(r"apply\.workable\.com/(?!j/)([a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("breezy", re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.I)),
    ("teamtailor", re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com", re.I)),
    ("bamboohr", re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("personio", re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9_-]+)", re.I)),
]

# Workday отдельно: нужно вытащить три части, а siteId стоит после
# необязательной локали (/en-US/, /fr-FR/ и т.п.).
WORKDAY = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)

# Служебные значения, которые матчатся шаблоном, но компанией не являются.
JUNK = {"embed", "job_board", "jobs", "job", "careers", "career", "search", "api",
        "static", "assets", "www", "en", "en-us", "list", "widget", "j"}


def from_url(url: str | None) -> tuple[str, str] | None:
    """URL вакансии → (источник, slug) или None."""
    if not url or "://" not in str(url):
        return None
    text = str(url)

    match = WORKDAY.search(text)
    if match:
        tenant, dc, site = match.group(1), match.group(2).lower(), match.group(3)
        if site.lower() in JUNK:
            return None
        return "workday", f"{tenant}|{dc}|{site}"

    for source, pattern in PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        slug = match.group(1)
        if not slug or slug.lower() in JUNK or len(slug) < 2:
            continue
        # SmartRecruiters чувствителен к регистру — его slug не приводим к нижнему.
        return source, slug if source == "smartrecruiters" else slug.lower()
    return None


def from_rows(rows: Iterable[dict], fields: tuple[str, ...] = (
        "vacancy_url", "careers_url", "url", "apply_url", "company_site_url")
        ) -> Iterator[dict]:
    """Строки снимка → кандидаты в реестр. Дедуп по (источник, slug)."""
    seen: set[tuple[str, str]] = set()
    for row in rows:
        found = None
        for field in fields:
            found = from_url(row.get(field))
            if found:
                break
        if not found or found in seen:
            continue
        seen.add(found)
        source, slug = found
        yield {"source": source, "slug": slug,
               "company_name": row.get("company_name"),
               "country_code": row.get("country_code"),
               "origin": "discovered_from_url"}
