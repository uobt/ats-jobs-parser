"""Агрегаторы: ленты вакансий целиком, а не по одной компании.

Отличие от `jobs/sources.py` принципиальное и определяет всю логику вокруг.
Адаптер ATS отвечает на вопрос «что открыто у ЭТОЙ компании», и поэтому по нему
можно считать закрытие вакансий разницей множеств. Лента отвечает «что вообще
появилось на площадке за последние дни»: это скользящее окно, и отсутствие
вакансии в сегодняшней выдаче не значит, что её закрыли — она просто уехала
за край окна. Поэтому по лентам вакансии НИКОГДА не закрываются
(`Store.apply_feed_result`), а свежесть берётся только из даты публикации.

Что это даёт: компании, которых нет ни на одном отслеживаемом ATS-борде.
Чего это НЕ даёт: дискавери ATS-слагов. Проверено — все шесть лент ведут
ссылки на самих себя, а не на борд работодателя, так что вытащить из них
«эта компания сидит на Greenhouse» без отдельного запроса на каждую вакансию
нельзя. Обещать обратное было бы враньём.

Ключей не нужно нигде. Шесть лент проверены живыми запросами 2026-08-05.
"""

from __future__ import annotations

import re
from typing import Any

from .fetch import Http
from .normalize import (Vacancy, clean_title, country_code, country_from_location,
                        parse_dt, seniority, strip_html, workplace)

_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


def company_slug(name: str | None, fallback: str | None = None) -> str | None:
    """Имя компании → устойчивый slug. Ключ дедупа компании внутри ленты."""
    text = (name or fallback or "").strip().lower()
    slug = _SLUG_JUNK.sub("-", text).strip("-")
    return slug[:80] or None


def _as_items(payload: Any, key: str | None) -> list[dict]:
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    if isinstance(payload, dict) and key:
        value = payload.get(key)
        if isinstance(value, list):
            return [i for i in value if isinstance(i, dict)]
    return []


class Feed:
    code: str = ""
    label: str = ""
    docs: str = ""
    notes: str = ""
    risk: str = "official"
    date_in_list: bool = True

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        raise NotImplementedError

    def _finish(self, vacancy: Vacancy) -> Vacancy:
        if not vacancy.country_code:
            vacancy.country_code = country_from_location(vacancy.location_raw)
        if not vacancy.seniority:
            vacancy.seniority = seniority(vacancy.title)
        if not vacancy.workplace:
            vacancy.workplace = workplace(vacancy.location_raw)
        return vacancy


class RemoteOK(Feed):
    code, label = "remoteok", "RemoteOK"
    docs = "https://remoteok.com/api"
    notes = ("ПЕРВЫЙ элемент массива — не вакансия, а юридическая приписка; "
             "её обязательно отбрасывать. Отдаёт ровно ~100 последних вакансий, "
             "пагинации нет — параметр --pages на неё не влияет. "
             "Все вакансии удалённые по определению.")

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        items = _as_items(http.get_json("https://remoteok.com/api"), None)[1:]
        out = []
        for job in items:
            slug = company_slug(job.get("company"))
            if not slug:
                continue
            published, precision = parse_dt(job.get("date") or job.get("epoch"))
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("position")),
                url=job.get("url") or job.get("apply_url"),
                company_name=job.get("company"),
                location_raw=job.get("location"),
                workplace="remote",
                salary_min=job.get("salary_min") or None,
                salary_max=job.get("salary_max") or None,
                published_at=published, published_precision=precision,
                description=strip_html(job.get("description")))))
        return out


class Remotive(Feed):
    code, label = "remotive", "Remotive"
    docs = "https://remotive.com/api/remote-jobs"
    notes = ("Параметр limit есть, но на практике лента отдаёт около 30 вакансий "
             "независимо от него — --pages её глубже не раскопает. "
             "Локация — свободный текст candidate_required_location.")

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        limit = min(500, 100 * max(1, pages))
        payload = http.get_json(f"https://remotive.com/api/remote-jobs?limit={limit}")
        out = []
        for job in _as_items(payload, "jobs"):
            slug = company_slug(job.get("company_name"))
            if not slug:
                continue
            published, precision = parse_dt(job.get("publication_date"))
            location = job.get("candidate_required_location")
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("title")),
                url=job.get("url"),
                company_name=(job.get("company_name") or "").strip() or None,
                location_raw=location, workplace="remote",
                department=job.get("category"),
                employment_type=job.get("job_type"),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("description")))))
        return out


class Himalayas(Feed):
    code, label = "himalayas", "Himalayas"
    docs = "https://himalayas.app/jobs/api"
    notes = ("Отдаёт companySlug — единственная лента, где slug компании готовый. "
             "Есть seniority отдельным полем и ограничения по странам.")

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        out = []
        for page in range(max(1, pages)):
            payload = http.get_json(
                f"https://himalayas.app/jobs/api?limit=100&offset={page * 100}")
            items = _as_items(payload, "jobs")
            if not items:
                break
            for job in items:
                slug = company_slug(job.get("companySlug"), job.get("companyName"))
                if not slug:
                    continue
                published, precision = parse_dt(job.get("pubDate"))
                restrictions = job.get("locationRestrictions") or []
                levels = job.get("seniority") or []
                out.append(self._finish(Vacancy(
                    source=self.code, slug=slug,
                    source_job_id=str(job.get("guid") or job.get("applicationLink")),
                    title=clean_title(job.get("title")),
                    url=job.get("applicationLink"),
                    company_name=job.get("companyName"),
                    location_raw=", ".join(str(r) for r in restrictions) or None,
                    country_code=country_code(*restrictions) if restrictions else None,
                    workplace="remote",
                    employment_type=job.get("employmentType"),
                    seniority=levels[0] if levels else None,
                    salary_min=job.get("minSalary"), salary_max=job.get("maxSalary"),
                    salary_currency=job.get("currency"),
                    published_at=published, published_precision=precision,
                    description=strip_html(job.get("description") or job.get("excerpt")))))
        return out


class Arbeitnow(Feed):
    code, label = "arbeitnow", "Arbeitnow"
    docs = "https://www.arbeitnow.com/api/job-board-api"
    notes = ("Сильный крен в DACH — лучшая из лент под немецкий рынок. "
             "175 вакансий на страницу, пагинация через links.next.")

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        url = "https://www.arbeitnow.com/api/job-board-api"
        out = []
        for _ in range(max(1, pages)):
            payload = http.get_json(url)
            for job in _as_items(payload, "data"):
                slug = company_slug(job.get("company_name"))
                if not slug:
                    continue
                published, precision = parse_dt(job.get("created_at"))
                out.append(self._finish(Vacancy(
                    source=self.code, slug=slug,
                    source_job_id=str(job.get("slug") or job.get("url")),
                    title=clean_title(job.get("title")),
                    url=job.get("url"), company_name=job.get("company_name"),
                    location_raw=job.get("location"),
                    workplace="remote" if job.get("remote") else None,
                    employment_type=", ".join(job.get("job_types") or []) or None,
                    published_at=published, published_precision=precision,
                    description=strip_html(job.get("description")))))
            nxt = (payload.get("links") or {}).get("next") if isinstance(payload, dict) else None
            if not nxt:
                break
            url = nxt
        return out


class Jobicy(Feed):
    code, label = "jobicy", "Jobicy"
    docs = "https://jobicy.com/jobs-rss-feed"
    notes = "Есть jobGeo, jobLevel и зарплатная вилка с валютой. Параметр count."

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        count = min(50, 20 * max(1, pages))
        payload = http.get_json(f"https://jobicy.com/api/v2/remote-jobs?count={count}")
        out = []
        for job in _as_items(payload, "jobs"):
            slug = company_slug(job.get("companyName"))
            if not slug:
                continue
            published, precision = parse_dt(job.get("pubDate"))
            industry = job.get("jobIndustry") or []
            out.append(self._finish(Vacancy(
                source=self.code, slug=slug, source_job_id=str(job.get("id")),
                title=clean_title(job.get("jobTitle")),
                url=job.get("url"), company_name=job.get("companyName"),
                location_raw=job.get("jobGeo"),
                country_code=country_code(job.get("jobGeo")),
                workplace="remote",
                department=industry[0] if industry else None,
                employment_type=", ".join(job.get("jobType") or []) or None,
                seniority=job.get("jobLevel"),
                salary_min=job.get("salaryMin") or None,
                salary_max=job.get("salaryMax") or None,
                salary_currency=job.get("salaryCurrency"),
                published_at=published, published_precision=precision,
                description=strip_html(job.get("jobDescription") or job.get("jobExcerpt")))))
        return out


class TheMuse(Feed):
    code, label = "themuse", "The Muse"
    docs = "https://www.themuse.com/developers/api/v2"
    notes = ("Не удалённая лента, а обычная — единственная здесь с офисными "
             "вакансиями США. 20 на страницу, поэтому страниц нужно много. "
             "Осторожно: попадаются очень старые публикации, фильтр по дате обязателен.")

    def fetch(self, http: Http, pages: int = 1) -> list[Vacancy]:
        out = []
        for page in range(max(1, pages)):
            payload = http.get_json(f"https://www.themuse.com/api/public/jobs?page={page}")
            items = _as_items(payload, "results")
            if not items:
                break
            for job in items:
                company = job.get("company") or {}
                slug = company_slug(company.get("short_name"), company.get("name"))
                if not slug:
                    continue
                published, precision = parse_dt(job.get("publication_date"))
                locations = [loc.get("name") for loc in job.get("locations") or []
                             if isinstance(loc, dict)]
                levels = [lvl.get("name") for lvl in job.get("levels") or []
                          if isinstance(lvl, dict)]
                location = "; ".join(str(x) for x in locations if x) or None
                out.append(self._finish(Vacancy(
                    source=self.code, slug=slug, source_job_id=str(job.get("id")),
                    title=clean_title(job.get("name")),
                    url=(job.get("refs") or {}).get("landing_page"),
                    company_name=company.get("name"),
                    location_raw=location,
                    seniority=levels[0] if levels else None,
                    published_at=published, published_precision=precision,
                    description=strip_html(job.get("contents")))))
        return out


REGISTRY: dict[str, Feed] = {
    f.code: f for f in (RemoteOK(), Remotive(), Himalayas(), Arbeitnow(), Jobicy(), TheMuse())
}
DEFAULT_ORDER = ["arbeitnow", "himalayas", "remoteok", "remotive", "jobicy", "themuse"]


def get(code: str) -> Feed:
    if code not in REGISTRY:
        raise KeyError(f"неизвестная лента {code!r}; есть: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[code]


def codes(only=None) -> list[str]:
    if not only:
        return list(DEFAULT_ORDER)
    unknown = [c for c in only if c not in REGISTRY]
    if unknown:
        raise KeyError(f"неизвестные ленты: {', '.join(unknown)}")
    return [c for c in DEFAULT_ORDER if c in set(only)]
