"""Адаптеры площадок: один источник — один класс, один контракт.

Все шаблоны эндпоинтов сверены живыми запросами 2026-08-05. Где вендор не
документирует ручку публично, это помечено в `risk` — такие адаптеры ломаются
первыми, поэтому у каждого есть свой ряд в статус-борде.

  official      вендор описал эндпоинт в публичной документации
  undocumented  ручка живая и анонимная, но в доке её нет
  internal      внутренний эндпоинт виджета/SPA, может смениться без предупреждения

Контракт адаптера — два метода:

    board_url(slug) -> str            человекочитаемая карьерная страница
    fetch(http, slug) -> list[Vacancy] нормализованные вакансии

Адаптер НЕ фильтрует и НЕ скорит. Это была главная беда `ats-job-scraper`, где
скоринг вварен внутрь каждой функции забора: вернуть «все вакансии компании»
такой парсер физически не умеет. Фильтрация — отдельный слой выше.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Iterable

from .fetch import BROWSER_UA, Http
from .normalize import (Vacancy, clean_title, country_code, country_from_location,
                        parse_dt, parse_relative_posted, seniority, strip_html,
                        workplace)


class Source:
    code: str = ""
    label: str = ""
    risk: str = "official"
    docs: str = ""
    date_in_list: bool = True          # есть ли дата публикации в списочном ответе
    notes: str = ""

    # Заполняется адаптером, когда выдача обрезана потолком пагинации. Читается
    # сразу после fetch() тем же потоком — прогон однопоточный. Существует, чтобы
    # обрезание не было тихим: «взяли 500 из 2000» должно попадать в лог прогона,
    # иначе неполный обход выглядит как полный.
    last_note: str | None = None

    def board_url(self, slug: str) -> str:
        raise NotImplementedError

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        raise NotImplementedError

    # общий хвост нормализации, чтобы адаптеры не повторяли одно и то же
    def _finish(self, vacancy: Vacancy) -> Vacancy:
        if not vacancy.country_code:
            vacancy.country_code = country_from_location(vacancy.location_raw)
        if not vacancy.seniority:
            vacancy.seniority = seniority(vacancy.title)
        if not vacancy.workplace:
            vacancy.workplace = workplace(vacancy.location_raw)
        return vacancy


def _as_list(payload: Any, *keys: str) -> list[dict]:
    """Площадки кладут массив то в корень, то под разными ключами."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


# --- Greenhouse ---------------------------------------------------------------

class Greenhouse(Source):
    code, label, risk = "greenhouse", "Greenhouse", "official"
    docs = "https://developers.greenhouse.io/job-board.html"
    notes = ("API-хост остался boards-api.greenhouse.io, хотя HTML-борд переехал "
             "на job-boards.greenhouse.io. Поле first_published есть в ответе, "
             "но в документации не описано — брать с фолбэком на updated_at.")

    def board_url(self, slug: str) -> str:
        return f"https://job-boards.greenhouse.io/{slug}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        out = []
        for job in _as_list(payload, "jobs"):
            published, precision = parse_dt(job.get("first_published"))
            if not published:
                published, precision = parse_dt(job.get("updated_at"))
                precision = "exact" if published else None
            location = (job.get("location") or {}).get("name")
            offices = [o.get("location") or o.get("name") for o in job.get("offices") or []]
            departments = [d.get("name") for d in job.get("departments") or [] if d.get("name")]
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("title")),
                url=job.get("absolute_url"),
                company_name=job.get("company_name"),
                careers_url=self.board_url(slug),
                location_raw=location,
                country_code=country_code(*[o for o in offices if o]) or country_from_location(location),
                department=departments[0] if departments else None,
                published_at=published, published_precision=precision,
                description=strip_html(job.get("content")),
            )))
        return out


# --- Lever --------------------------------------------------------------------

class Lever(Source):
    code, label, risk = "lever", "Lever", "official"
    docs = "https://github.com/lever/postings-api"
    notes = ("createdAt — epoch в миллисекундах, живой, но в README не описан. "
             "robots.txt объявляет Crawl-delay: 1. Часть клиентов отключает "
             "публичный postings-эндпоинт — это 404, а не ошибка парсера.")

    def board_url(self, slug: str) -> str:
        return f"https://jobs.lever.co/{slug}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        out = []
        for job in _as_list(payload):
            categories = job.get("categories") or {}
            salary = job.get("salaryRange") or {}
            published, precision = parse_dt(job.get("createdAt"))
            location = categories.get("location")
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("text")),
                url=job.get("hostedUrl"), apply_url=job.get("applyUrl"),
                careers_url=self.board_url(slug),
                location_raw=location,
                country_code=country_code(job.get("country")) or country_from_location(location),
                workplace=workplace(location, job.get("workplaceType")),
                department=categories.get("department") or categories.get("team"),
                employment_type=categories.get("commitment"),
                salary_min=salary.get("min"), salary_max=salary.get("max"),
                salary_currency=salary.get("currency"),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("descriptionPlain") or job.get("description")),
            )))
        return out


# --- Ashby --------------------------------------------------------------------

class Ashby(Source):
    code, label, risk = "ashby", "Ashby", "official"
    docs = "https://developers.ashbyhq.com/docs/public-job-posting-api"
    notes = ("publishedAt — это «когда опубликовали в последний раз»: при "
             "переоткрытии вакансии дата сбрасывается и даёт ложную свежесть. "
             "Официальный REST posting-api, а не старый внутренний GraphQL.")

    def board_url(self, slug: str) -> str:
        return f"https://jobs.ashbyhq.com/{slug}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
        out = []
        for job in _as_list(payload, "jobs"):
            if job.get("isListed") is False:
                continue
            address = ((job.get("address") or {}).get("postalAddress")) or {}
            published, precision = parse_dt(job.get("publishedAt"))
            location = job.get("location")
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("title")),
                url=job.get("jobUrl"), apply_url=job.get("applyUrl"),
                careers_url=self.board_url(slug),
                location_raw=location,
                city=address.get("addressLocality"), region=address.get("addressRegion"),
                country_code=country_code(address.get("addressCountry")) or country_from_location(location),
                workplace=workplace(location, job.get("isRemote"), job.get("workplaceType")),
                department=job.get("department") or job.get("team"),
                employment_type=job.get("employmentType"),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("descriptionPlain") or job.get("descriptionHtml")),
            )))
        return out


# --- Workable -----------------------------------------------------------------

class Workable(Source):
    code, label, risk = "workable", "Workable", "undocumented"
    docs = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    notes = ("published_on — только дата без времени: недельное окно считается, "
             "«сегодня против вчера» — нет. Поле experience даёт сеньорити "
             "прямым текстом, это редкость среди площадок.")

    def board_url(self, slug: str) -> str:
        return f"https://apply.workable.com/{slug}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
        company = payload.get("name") if isinstance(payload, dict) else None
        out = []
        for job in _as_list(payload, "jobs"):
            published, precision = parse_dt(job.get("published_on") or job.get("created_at"))
            locations = job.get("locations") or []
            first = locations[0] if locations and isinstance(locations[0], dict) else {}
            location = ", ".join(p for p in (job.get("city"), job.get("state"),
                                             job.get("country")) if p) or None
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug,
                source_job_id=str(job.get("shortcode") or job.get("id") or job.get("code")),
                title=clean_title(job.get("title")),
                url=job.get("url") or job.get("shortlink"),
                apply_url=job.get("application_url"),
                company_name=company, careers_url=self.board_url(slug),
                location_raw=location,
                city=job.get("city") or first.get("city"),
                region=job.get("state") or first.get("region"),
                country_code=country_code(first.get("countryCode"), job.get("country")),
                workplace=workplace(location, job.get("telecommuting")),
                department=job.get("department"),
                employment_type=job.get("employment_type"),
                seniority=None if not job.get("experience") else str(job["experience"]),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("description")),
            )))
        return out


# --- SmartRecruiters ----------------------------------------------------------

class SmartRecruiters(Source):
    code, label, risk = "smartrecruiters", "SmartRecruiters", "official"
    docs = "https://developers.smartrecruiters.com/docs/endpoints"
    notes = ("companyIdentifier чувствителен к регистру и обычно в CamelCase — "
             "частая причина пустого ответа. Документировано 10 req/s, "
             "пагинация offset/limit по totalFound.")
    PAGE = 100
    MAX_PAGES = 50                     # 5000 вакансий на компанию — потолок с логом

    def board_url(self, slug: str) -> str:
        return f"https://careers.smartrecruiters.com/{slug}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        self.last_note = None
        out: list[Vacancy] = []
        offset, total, pages = 0, None, 0
        while pages < self.MAX_PAGES:
            payload = http.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                f"?offset={offset}&limit={self.PAGE}")
            items = _as_list(payload, "content")
            if total is None and isinstance(payload, dict):
                total = payload.get("totalFound")
            for job in items:
                location = job.get("location") or {}
                published, precision = parse_dt(job.get("releasedDate"))
                loc_text = ", ".join(p for p in (location.get("city"), location.get("region"),
                                                 location.get("country")) if p) or None
                out.append(self._finish(Vacancy(
                    source=self.code, slug=slug, source_job_id=str(job.get("id")),
                    title=clean_title(job.get("name")),
                    url=(f"https://jobs.smartrecruiters.com/{slug}/{job.get('id')}"
                         if job.get("id") else None),
                    company_name=(job.get("company") or {}).get("name"),
                    careers_url=self.board_url(slug),
                    location_raw=loc_text, city=location.get("city"),
                    region=location.get("region"),
                    country_code=country_code(location.get("country")),
                    workplace=workplace(loc_text, location.get("remote")),
                    department=(job.get("department") or {}).get("label"),
                    employment_type=(job.get("typeOfEmployment") or {}).get("label"),
                    seniority=(job.get("experienceLevel") or {}).get("label"),
                    published_at=published, published_precision=precision,
                )))
            pages += 1
            offset += self.PAGE
            if not items or (total is not None and offset >= total):
                break
        if pages >= self.MAX_PAGES and total and offset < total:
            self.last_note = f"обрезано: взято {offset} из {total} вакансий"
        return out


# --- Recruitee ----------------------------------------------------------------

class Recruitee(Source):
    code, label, risk = "recruitee", "Recruitee", "official"
    docs = "https://docs.recruitee.com/reference/intro-to-careers-site-api"
    notes = ("Лучший набор полей из всех: created_at и published_at раздельно, "
             "три отдельных флага remote/hybrid/on_site, и careers_url отдаёт "
             "реальный кастомный домен компании — редкий бесплатный источник домена.")

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(f"https://{slug}.recruitee.com/api/offers/")
        out = []
        for job in _as_list(payload, "offers"):
            published, precision = parse_dt(job.get("published_at") or job.get("created_at"))
            salary = job.get("salary") or {}
            place = "remote" if job.get("remote") else (
                "hybrid" if job.get("hybrid") else ("onsite" if job.get("on_site") else None))
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("title")),
                url=job.get("careers_url"), apply_url=job.get("careers_apply_url"),
                careers_url=job.get("careers_url") or self.board_url(slug),
                location_raw=job.get("location"), city=job.get("city"),
                region=job.get("state_name"),
                country_code=country_code(job.get("country_code"), job.get("country")),
                workplace=place, department=job.get("department"),
                employment_type=job.get("employment_type_code"),
                seniority=job.get("experience_code"),
                salary_min=salary.get("min"), salary_max=salary.get("max"),
                salary_currency=salary.get("currency"),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("description")),
            )))
        return out


# --- Breezy HR ----------------------------------------------------------------

class Breezy(Source):
    code, label, risk = "breezy", "Breezy HR", "internal"
    docs = "нет публичной документации; /json питает хостящиеся карьерные страницы"
    notes = ("Breezy официально заявляет, что публичного API вакансий у них нет. "
             "Ручка живая, но из всех работающих у неё самый высокий риск закрытия — "
             "держать за адаптером и не строить на ней ключевые сценарии.")

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.breezy.hr"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(f"https://{slug}.breezy.hr/json")
        out = []
        for job in _as_list(payload, "positions"):
            location = job.get("location") or {}
            country = (location.get("country") or {}).get("name") if isinstance(
                location.get("country"), dict) else location.get("country")
            published, precision = parse_dt(job.get("published_date"))
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug,
                source_job_id=str(job.get("id") or job.get("friendly_id")),
                title=clean_title(job.get("name")),
                url=job.get("url"),
                company_name=(job.get("company") or {}).get("name"),
                careers_url=self.board_url(slug),
                location_raw=location.get("name"), city=location.get("city"),
                country_code=country_code(country),
                workplace=workplace(location.get("name"), location.get("is_remote")),
                department=job.get("department"),
                employment_type=(job.get("type") or {}).get("name"),
                published_at=published, published_precision=precision,
            )))
        return out


# --- Teamtailor ---------------------------------------------------------------

class Teamtailor(Source):
    code, label, risk = "teamtailor", "Teamtailor", "official"
    docs = "https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide"
    notes = ("Формат JSON Feed v1.1. Большая часть клиентов сидит на кастомных "
             "доменах, поэтому дорк по teamtailor.com их не видит — зато "
             "/jobs.json работает и на кастомном домене, это дешёвая проверка. "
             "Отдела и remote-флага в фиде нет.")

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(f"https://{slug}.teamtailor.com/jobs.json")
        home = payload.get("home_page_url") if isinstance(payload, dict) else None
        out = []
        for job in _as_list(payload, "items"):
            posting = job.get("_jobposting") or {}
            locations = posting.get("jobLocation") or []
            address = (locations[0].get("address") if locations and isinstance(locations[0], dict)
                       else {}) or {}
            published, precision = parse_dt(job.get("date_published") or posting.get("datePosted"))
            loc_text = ", ".join(str(p) for p in (address.get("addressLocality"),
                                                  address.get("addressRegion"),
                                                  address.get("addressCountry")) if p) or None
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("title") or posting.get("title")),
                url=job.get("url"),
                company_name=(posting.get("hiringOrganization") or {}).get("name"),
                company_site_url=(posting.get("hiringOrganization") or {}).get("sameAs"),
                careers_url=home or self.board_url(slug),
                location_raw=loc_text, city=address.get("addressLocality"),
                region=address.get("addressRegion"),
                country_code=country_code(address.get("addressCountry")),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("content_html") or posting.get("description")),
            )))
        return out


# --- BambooHR -----------------------------------------------------------------

class BambooHR(Source):
    code, label, risk = "bamboohr", "BambooHR", "internal"
    date_in_list = False
    docs = "нет публичной документации; /careers/list — эндпоинт виджета"
    notes = ("ДАТЫ НЕТ НИ ОДНОЙ — ни created, ни published, ни updated. Свежесть "
             "считается только собственным снапшот-диффом: новая вакансия — это "
             "id, которого не было в прошлом прогоне. Отсюда задержка в один цикл. "
             "Требует браузероподобный User-Agent, иначе 302 вместо JSON.")

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.bamboohr.com/careers"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(f"https://{slug}.bamboohr.com/careers/list",
                                headers={"User-Agent": BROWSER_UA,
                                         "Accept": "application/json"})
        out = []
        for job in _as_list(payload, "result"):
            location = job.get("location") or {}
            ats = job.get("atsLocation") or {}
            loc_text = ", ".join(str(p) for p in (location.get("city"),
                                                  location.get("state")) if p) or None
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("jobOpeningName")),
                url=f"https://{slug}.bamboohr.com/careers/{job.get('id')}",
                careers_url=self.board_url(slug),
                location_raw=loc_text, city=location.get("city") or ats.get("city"),
                region=location.get("state") or ats.get("state"),
                country_code=country_code(ats.get("country")),
                workplace=workplace(loc_text, job.get("isRemote")),
                department=job.get("departmentLabel"),
                employment_type=job.get("employmentStatusLabel"),
                published_at=None, published_precision="none",
            )))
        return out


# --- Personio -----------------------------------------------------------------

class Personio(Source):
    code, label, risk = "personio", "Personio", "official"
    docs = "https://developer.personio.de/docs/retrieving-open-job-positions"
    notes = ("XML, не JSON. Аудитория — DACH-SMB и mid-market: лучший источник "
             "под немецкоязычный рынок. seniority отдаётся явным полем. "
             "Хосты .personio.de и .personio.com оба живые — проверять оба.")

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.jobs.personio.de"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        text = http.get_text(f"https://{slug}.jobs.personio.de/xml?language=en")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ValueError(f"personio отдал не XML для {slug}: {exc}") from None
        out = []
        for position in root.iter("position"):
            def field(name: str) -> str | None:
                node = position.find(name)
                return (node.text or "").strip() or None if node is not None else None

            job_id = field("id")
            if not job_id:
                continue
            published, precision = parse_dt(field("createdAt"))
            office = field("office")
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=job_id,
                title=clean_title(field("name")),
                url=f"https://{slug}.jobs.personio.de/job/{job_id}",
                careers_url=self.board_url(slug),
                location_raw=office,
                department=field("department"),
                employment_type=field("employmentType") or field("schedule"),
                seniority=field("seniority"),
                published_at=published, published_precision=precision,
            )))
        return out


# --- Rippling -----------------------------------------------------------------

class Rippling(Source):
    code, label, risk = "rippling", "Rippling ATS", "internal"
    date_in_list = False
    docs = "публичной документации нет; ручка питает ats.rippling.com"
    notes = ("Список ВОЗВРАЩАЕТ ДУБЛИ: одна вакансия повторяется по числу локаций "
             "с тем же uuid — дедуп по uuid обязателен. Даты в списке нет, "
             "createdOn только в detail-эндпоинте (+1 запрос на вакансию).")

    def board_url(self, slug: str) -> str:
        return f"https://ats.rippling.com/{slug}/jobs"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        payload = http.get_json(
            f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
        seen: dict[str, Vacancy] = {}
        for job in _as_list(payload, "jobs", "results"):
            uuid = str(job.get("uuid") or job.get("id") or "")
            if not uuid:
                continue
            location = (job.get("workLocation") or {}).get("label")
            if uuid in seen:                       # тот же uuid с другой локацией
                existing = seen[uuid]
                if location and existing.location_raw and location not in existing.location_raw:
                    existing.location_raw = f"{existing.location_raw}; {location}"
                continue
            seen[uuid] = self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=uuid,
                title=clean_title(job.get("name")),
                url=job.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
                careers_url=self.board_url(slug),
                location_raw=location,
                department=(job.get("department") or {}).get("label"),
                published_at=None, published_precision="none",
            ))
        return list(seen.values())


# --- Workday ------------------------------------------------------------------

class Workday(Source):
    code, label, risk = "workday", "Workday", "undocumented"
    date_in_list = False
    docs = "публичной документации на CXS нет; де-факто стабилен много лет"
    notes = ("slug составной: 'tenant|wdN|siteId', например 'nvidia|wd5|NVIDIAExternalCareerSite'. "
             "В списке вместо даты относительный текст ('Posted Today', 'Posted 30+ Days Ago') — "
             "точная startDate только в detail. Для сигнала свежести хватает "
             "'Today'/'Yesterday'/'N Days Ago' из списка, это экономит тысячи запросов. "
             "limit на странице практически ограничен 20.")
    PAGE = 20
    MAX_PAGES = 25                     # 500 вакансий на арендатора; превышение логируется

    @staticmethod
    def parse_slug(slug: str) -> tuple[str, str, str] | None:
        parts = slug.split("|")
        return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None

    def board_url(self, slug: str) -> str:
        parsed = self.parse_slug(slug)
        if not parsed:
            return slug
        tenant, dc, site = parsed
        return f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"

    def fetch(self, http: Http, slug: str) -> list[Vacancy]:
        self.last_note = None
        parsed = self.parse_slug(slug)
        if not parsed:
            raise ValueError(
                f"workday-слаг должен быть 'tenant|wdN|siteId', получено {slug!r}")
        tenant, dc, site = parsed
        base = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        out: list[Vacancy] = []
        offset, total, pages = 0, None, 0
        while pages < self.MAX_PAGES:
            payload = http.post_json(f"{base}/jobs", {
                "appliedFacets": {}, "limit": self.PAGE,
                "offset": offset, "searchText": ""})
            items = _as_list(payload, "jobPostings")
            if total is None and isinstance(payload, dict):
                total = payload.get("total")
            for job in items:
                path = job.get("externalPath") or ""
                published, precision = parse_relative_posted(job.get("postedOn"))
                location = job.get("locationsText")
                out.append(self._finish(Vacancy(
                    source=self.code, slug=slug,
                    source_job_id=path.rsplit("/", 1)[-1] or str(job.get("bulletFields")),
                    title=clean_title(job.get("title")),
                    url=f"https://{tenant}.{dc}.myworkdayjobs.com/{site}{path}",
                    careers_url=self.board_url(slug),
                    location_raw=location,
                    published_at=published, published_precision=precision,
                )))
            pages += 1
            offset += self.PAGE
            if not items or (total is not None and offset >= total):
                break
        if pages >= self.MAX_PAGES and total and offset < total:
            self.last_note = f"обрезано: взято {offset} из {total} вакансий"
        return out


REGISTRY: dict[str, Source] = {
    s.code: s for s in (
        Greenhouse(), Lever(), Ashby(), Workable(), SmartRecruiters(), Recruitee(),
        Breezy(), Teamtailor(), BambooHR(), Personio(), Rippling(), Workday(),
    )
}

# Порядок по отдаче: сначала те, где дата в списке и объём большой.
DEFAULT_ORDER = ["greenhouse", "smartrecruiters", "lever", "ashby", "workable",
                 "breezy", "teamtailor", "recruitee", "personio", "rippling",
                 "workday", "bamboohr"]


def get(code: str) -> Source:
    if code not in REGISTRY:
        raise KeyError(f"неизвестный источник {code!r}; есть: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[code]


def codes(only: Iterable[str] | None = None) -> list[str]:
    if not only:
        return list(DEFAULT_ORDER)
    unknown = [c for c in only if c not in REGISTRY]
    if unknown:
        raise KeyError(f"неизвестные источники: {', '.join(unknown)}")
    return [c for c in DEFAULT_ORDER if c in set(only)]
