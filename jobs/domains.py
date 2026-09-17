"""Домен компании — главная дыра портального парсера и обязательное условие письма.

В снимке портала домен известен у 26.4% записей Greenhouse и у 0.5-8% остальных:
756 736 вакансий дали всего 1 393 уникальных `company_site_url`. Компания без
домена бесполезна для аутрича, каким бы горячим ни был сигнал найма.

Здесь закрывается бесплатная часть задачи — та, где домен уже лежит в ответе ATS,
просто в разных полях и вперемешку с хостами самих площадок. Платный резолв
(поиск, обогащение) сюда не входит намеренно: правило проекта — ни одного
платного вызова без ручного approve, и дорогое только после дешёвого.

Каждый домен хранится с `domain_source` — откуда он взялся. Без этого через месяц
не отличить надёжный домен из schema.org от догадки.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlsplit

import tldextract

# Хосты самих ATS: careers_url у них указывает на площадку, а не на компанию.
# Записать такой домен как домен компании — значит отправить письмо в Greenhouse.
ATS_HOSTS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
    "recruitee.com", "breezy.hr", "teamtailor.com", "bamboohr.com", "personio.de",
    "personio.com", "rippling.com", "myworkdayjobs.com", "workday.com", "join.com",
    "comeet.co", "jazz.co", "jazzhr.com", "icims.com", "jobvite.com", "taleo.net",
    "successfactors.com", "avature.net", "phenompeople.com", "csod.com", "gem.com",
    "paycom.com", "paylocity.com", "adp.com", "ukg.com", "eurpoa.eu", "europa.eu",
    "linkedin.com", "indeed.com", "glassdoor.com", "welcometothejungle.com",
}

# Хостинги карьерных страниц и агрегаторы отзывов. Отдельно от ATS_HOSTS, потому
# что это не ATS: компания сидит на своём Greenhouse, а карьерная страница живёт
# на чужом домене. Найдено на живых данных — careerpuck.com оказался «доменом»
# сразу у семи компаний (Lyft, Udemy, Prenuvo и др.), comparably.com — у трёх.
# Любой домен, общий для нескольких несвязанных компаний, — кандидат в этот список.
CAREERS_HOSTS = {
    "careerpuck.com", "comparably.com", "getro.com", "applytojob.com",
    "myworkdaysite.com", "eightfold.ai", "pinpointhq.com", "dayforcehcm.com",
    "ultipro.com", "oraclecloud.com", "silkroad.com", "clearcompany.com",
    "hirehive.com", "jobscore.com", "recruiterbox.com", "trakstar.com",
    "hiringthing.com", "workforcenow.com", "brassring.com", "jobs.net",
    "smartrecruiters.net", "greenhouse.com", "builtin.com", "wellfound.com",
    "angel.co", "otta.com", "jobgether.com",
}

# Бесплатные почтовики и хостинги: домен есть, но он не корпоративный.
NON_CORPORATE = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "proton.me", "protonmail.com", "wixsite.com", "squarespace.com",
    "webflow.io", "github.io", "notion.site", "wordpress.com", "blogspot.com",
    "sites.google.com", "bit.ly", "linktr.ee",
}

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())      # без сетевых обновлений
_CLEAN = re.compile(r"^(www|www2|m|jobs|careers|career|work|apply|hiring|talent|join)\.")


def registrable(url_or_host: str | None) -> str | None:
    """URL или хост → регистрируемый домен (example.co.uk, а не jobs.example.co.uk)."""
    if not url_or_host:
        return None
    text = str(url_or_host).strip().lower()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    host = urlsplit(text).netloc or urlsplit(text).path
    host = host.split("@")[-1].split(":")[0].strip("/")
    if not host or "." not in host:
        return None
    host = _CLEAN.sub("", host)
    parts = _EXTRACT(host)
    if not parts.domain or not parts.suffix:
        return None
    return f"{parts.domain}.{parts.suffix}"


BLOCKED = ATS_HOSTS | CAREERS_HOSTS | NON_CORPORATE


def is_company_domain(domain: str | None) -> bool:
    """False для хостов площадок, хостингов карьерных страниц, почтовиков и конструкторов.

    Записать чужой домен как домен компании дороже, чем не записать никакого:
    пустое поле честно говорит «не знаем», а careerpuck.com у Lyft превращается
    в письмо не туда и в испорченную статистику по домену.
    """
    if not domain:
        return False
    return not any(domain == host or domain.endswith("." + host) for host in BLOCKED)


def resolve(candidates: Iterable[tuple[str, str | None]]) -> tuple[str | None, str | None]:
    """Первый годный домен из упорядоченного списка (источник, значение).

    Порядок кандидатов задаёт вызывающий и он же задаёт доверие: сначала то,
    что компания сама указала в разметке вакансии, потом то, что мы вывели.
    """
    for source, value in candidates:
        domain = registrable(value)
        if is_company_domain(domain):
            return domain, source
    return None, None


def from_company_row(row: dict) -> tuple[str | None, str | None]:
    """Домен компании из полей, которые уже есть в реестре и в ответах ATS."""
    return resolve([
        ("site_url", row.get("site_url") or row.get("company_site_url")),
        ("careers_url", row.get("careers_url")),
    ])


_URL_RX = re.compile(r"https?://[^\s\"'<>)\]}]+", re.IGNORECASE)
_NAME_JUNK = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|bv|nv|ag|sa|srl|spa|plc|co|corp|corporation|"
    r"company|group|holding|holdings|technologies|technology|labs|software|"
    r"solutions|services|systems|global|international|the)\b", re.IGNORECASE)
_ALNUM = re.compile(r"[^a-z0-9]+")


def _name_key(name: str | None) -> str:
    """Название компании → сплошная строка букв и цифр без юрформ и пробелов."""
    text = _NAME_JUNK.sub(" ", (name or "").lower())
    return _ALNUM.sub("", text)


def from_description(company_name: str | None, text: str | None,
                     max_urls: int = 60) -> tuple[str | None, str | None]:
    """Домен из текста вакансии — но ТОЛЬКО если он совпадает с названием компании.

    Ни одна из площадок не отдаёт сайт работодателя отдельным полем (проверено:
    ни Workable, ни Ashby, ни Breezy, ни SmartRecruiters), а в тексте вакансии
    ссылка на себя встречается часто. Проблема в том, что там же лежат ссылки
    на политику приватности, LinkedIn, карты и сам ATS.

    Отсюда жёсткое условие: домен засчитывается, только если его имя совпадает
    с названием компании (одно содержится в другом после вычистки юрформ).
    `Acme Technologies Inc` + `acme.com` → берём; `Acme` + `greenhouse.io` или
    `Acme` + `linkedin.com` → нет. Это сознательно строгий фильтр: пустое поле
    честнее, чем чужой домен в поле «куда писать».
    """
    key = _name_key(company_name)
    if not key or len(key) < 4 or not text:
        return None, None
    for url in _URL_RX.findall(str(text))[:max_urls]:
        domain = registrable(url)
        if not is_company_domain(domain):
            continue
        label = _ALNUM.sub("", domain.split(".")[0])
        if len(label) < 4:
            continue
        if label in key or key in label:
            return domain, "description_name_match"
    return None, None
