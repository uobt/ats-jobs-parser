"""Единая схема вакансии и приведение к ней разнородных ответов ATS.

Каждая площадка отдаёт своё: Lever — epoch в миллисекундах, Workable — дату без
времени, Greenhouse — ISO с таймзоной, Workday — строку «Posted 30+ Days Ago».
Ниже всё это сводится к одному типу `Vacancy`, чтобы дедуп, дельта и сигналы
работали одинаково, не зная про источник.

Правило проекта «никаких выдумок в данных» здесь означает: если поля нет —
оно None, а не догадка. `published_at=None` честнее, чем подставленная дата
прогона: по такой дате потом посчиталась бы ложная «свежая вакансия».
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

# ISO 3166-1 alpha-2 для стран, которые реально встречаются в выдаче ATS.
# Нужен, потому что площадки пишут страну как попало: 'United States', 'USA', 'US'.
COUNTRY_BY_NAME = {
    "united states": "us", "united states of america": "us", "usa": "us", "u.s.": "us",
    "u.s.a.": "us", "america": "us",
    "united kingdom": "gb", "uk": "gb", "great britain": "gb", "england": "gb",
    "scotland": "gb", "wales": "gb", "northern ireland": "gb",
    "canada": "ca", "australia": "au", "new zealand": "nz",
    "germany": "de", "deutschland": "de", "france": "fr", "spain": "es", "españa": "es",
    "italy": "it", "italia": "it", "netherlands": "nl", "the netherlands": "nl",
    "holland": "nl", "belgium": "be", "austria": "at", "österreich": "at",
    "switzerland": "ch", "schweiz": "ch", "sweden": "se", "norway": "no",
    "denmark": "dk", "finland": "fi", "ireland": "ie", "poland": "pl",
    "portugal": "pt", "czechia": "cz", "czech republic": "cz", "romania": "ro",
    "hungary": "hu", "greece": "gr", "bulgaria": "bg", "croatia": "hr",
    "slovakia": "sk", "slovenia": "si", "estonia": "ee", "latvia": "lv",
    "lithuania": "lt", "luxembourg": "lu", "malta": "mt", "cyprus": "cy",
    "india": "in", "singapore": "sg", "israel": "il", "brazil": "br", "mexico": "mx",
    "japan": "jp", "china": "cn", "south korea": "kr", "united arab emirates": "ae",
}

# Страны ЕС — для гео-фильтра «US + UK + CA + AU + EU».
EU = {"at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "gr", "hu",
      "ie", "it", "lv", "lt", "lu", "mt", "nl", "pl", "pt", "ro", "sk", "si", "es", "se"}
GEO_PRESETS = {
    "us": {"us"},
    "en": {"us", "gb", "ca", "au", "nz", "ie"},
    "en+eu": {"us", "gb", "ca", "au", "nz"} | EU,
    "all": set(),                                    # пустое множество = без фильтра
}

# Двухбуквенные коды штатов США. Нужны не для распознавания США, а РАДИ ЗАЩИТЫ
# от ложных стран: «San Francisco, CA» — это Калифорния, а не Канада; «Austin, TX» —
# не Тайвань; «Wilmington, DE» — не Германия; «Atlanta, GA» — не Грузия.
# Без этого списка гео-фильтр тихо раскидывает американские вакансии по Европе.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
}

# Провинции Канады — только те, что НЕ совпадают с кодом страны. Исключены
# NL (Ньюфаундленд против Нидерландов), SK (Саскачеван против Словакии),
# PE (Остров Принца Эдуарда против Перу), NU (Нунавут против Ниуэ),
# YT (Юкон против Майотты): в выдаче ATS страна встречается несравнимо чаще
# провинции, и «Amsterdam, NL» не должен превращаться в Канаду.
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NS", "NT", "ON", "QC"}

# ISO 3166-1 alpha-2 целиком. Сокращать список нельзя: усечённый набор молча
# выбрасывает валидные страны (на живых данных так потерялись sa, qa, gt, kw, ec).
# Коллизии с кодами штатов США разрешаются выше по порядку — там 'GA' это Джорджия
# штат, а не Грузия. Для наших гео это верный размен.
ISO_COUNTRIES = set(
    "ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj "
    "bl bm bn bo bq br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr "
    "cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr "
    "ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu "
    "id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz "
    "la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq "
    "mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf "
    "pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si "
    "sj sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr "
    "tt tv tw tz ua ug um uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw".split())

US_STATE = re.compile(r",\s*(" + "|".join(sorted(US_STATES)) + r")\b(?!\w)", re.IGNORECASE)

REMOTE_RX = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.IGNORECASE)
HYBRID_RX = re.compile(r"\bhybrid\b", re.IGNORECASE)

# Сеньорити выводится из тайтла только когда написано прямым текстом.
SENIORITY_RULES = [
    ("c_level", re.compile(r"\b(chief|c[teofmr]o|vp|vice president|head of|president)\b", re.I)),
    ("director", re.compile(r"\b(director|dir\.)\b", re.I)),
    ("manager", re.compile(r"\b(manager|mgr|lead|team lead|tl)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|staff|principal|architect)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|intern|internship|entry.level|graduate|trainee)\b", re.I)),
]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


@dataclass
class Vacancy:
    """Одна вакансия в единой схеме. Ключ дедупа — (source, slug, source_job_id)."""

    source: str
    slug: str
    source_job_id: str
    title: str
    url: str | None = None
    apply_url: str | None = None
    company_name: str | None = None
    company_site_url: str | None = None
    careers_url: str | None = None
    location_raw: str | None = None
    city: str | None = None
    region: str | None = None
    country_code: str | None = None
    workplace: str | None = None                 # remote | hybrid | onsite | None
    department: str | None = None
    seniority: str | None = None
    employment_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    published_at: datetime | None = None
    published_precision: str | None = None       # exact | day | relative | none
    description: str | None = None
    notes: list[str] = field(default_factory=list)

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.slug, self.source_job_id)

    def as_row(self) -> dict[str, Any]:
        row = {k: v for k, v in self.__dict__.items() if k != "notes"}
        row["published_at"] = self.published_at.isoformat() if self.published_at else None
        return row


# --- даты --------------------------------------------------------------------

def parse_dt(value: Any) -> tuple[datetime | None, str | None]:
    """Дата публикации → (datetime в UTC, точность). Возвращает (None, None), если
    распознать не удалось: подставлять «сегодня» нельзя, это ломает сигнал свежести."""
    if value is None or value == "":
        return None, None

    # epoch в миллисекундах (Lever) или в секундах
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        if num < 1e8:                     # заведомо не дата — мусор вроде счётчика
            return None, None
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc), "exact"
        except (OverflowError, OSError, ValueError):
            return None, None

    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), "exact"
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc), "day"

    text = str(value).strip()
    if not text:
        return None, None

    # 'YYYY-MM-DD HH:MM:SS UTC' (Recruitee)
    text = re.sub(r"\s+UTC$", "+00:00", text)
    # 'Z' → смещение, которое понимает fromisoformat
    iso = text.replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in text else text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        precision = "day" if len(text) == 10 else "exact"
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc), precision
    except ValueError:
        pass

    for fmt, precision in (("%Y-%m-%d", "day"), ("%d.%m.%Y", "day"), ("%m/%d/%Y", "day")):
        try:
            return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc), precision
        except ValueError:
            continue
    return None, None


def parse_relative_posted(text: str | None, now: datetime | None = None
                          ) -> tuple[datetime | None, str | None]:
    """Workday отдаёт в списке не дату, а текст: 'Posted Today', 'Posted 5 Days Ago',
    'Posted 30+ Days Ago'. Точную startDate можно взять только из detail-эндпоинта,
    то есть +1 запрос на каждую вакансию. Для сигнала свежести этого хватает:
    'Today'/'Yesterday'/'N Days Ago' разворачиваются в дату с точностью до дня,
    а '30+' честно остаётся нераспознанным — это не дата, а «давно»."""
    if not text:
        return None, None
    now = now or datetime.now(timezone.utc)
    low = text.lower()
    if "+" in low:                                   # '30+ Days Ago' — верхней границы нет
        return None, "relative"
    if "today" in low:
        return now.replace(hour=0, minute=0, second=0, microsecond=0), "day"
    if "yesterday" in low:
        from datetime import timedelta
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), "day"
    m = re.search(r"(\d+)\s*day", low)
    if m:
        from datetime import timedelta
        return (now - timedelta(days=int(m.group(1)))).replace(
            hour=0, minute=0, second=0, microsecond=0), "day"
    return None, "relative"


# --- текст и гео -------------------------------------------------------------

def strip_html(text: str | None, limit: int = 20_000) -> str | None:
    """Сначала раскодировать эскейп, потом снимать теги — не наоборот.

    Greenhouse отдаёт `content` эскейпленным (`&lt;p&gt;`), портал хранит его
    эскейпленным дважды. Если сперва снять теги, эскейпленные никуда не денутся
    и в описание попадёт `<p>Text</p>` вместо `Text`.
    """
    if not text:
        return None
    plain = html.unescape(html.unescape(str(text)))
    plain = _TAG.sub(" ", plain)
    plain = _WS.sub(" ", plain).strip()
    return plain[:limit] or None


def country_code(*candidates: Any) -> str | None:
    """Первый распознанный код страны из набора кандидатов (строки, dict'ы, None)."""
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, dict):
            candidate = (candidate.get("countryCode") or candidate.get("country_code")
                         or candidate.get("country") or candidate.get("name"))
        text = str(candidate).strip()
        if not text:
            continue
        if len(text) == 2 and text.isalpha():
            return text.lower()
        if len(text) == 3 and text.isalpha():         # ISO alpha-3 из join.com
            return {"usa": "us", "gbr": "gb", "can": "ca", "aus": "au",
                    "deu": "de", "fra": "fr", "nld": "nl", "esp": "es"}.get(text.lower())
        code = COUNTRY_BY_NAME.get(text.lower())
        if code:
            return code
    return None


def country_from_location(location: str | None) -> str | None:
    """Страна из свободной строки локации. Осторожно с двухбуквенным хвостом.

    Порядок важен: сначала полное имя страны, потом код штата США/провинции
    Канады, и только потом двухбуквенный код как страна. Наивная проверка
    «две буквы = ISO-код» превращает «San Francisco, CA» в Канаду, а таких
    строк в выдаче Greenhouse больше всего.
    """
    if not location:
        return None
    text = location.strip()
    parts = [part.strip() for part in re.split(r"[,|/()]", text) if part.strip()]

    # 1. Полное название страны в хвосте — самый надёжный случай.
    if parts:
        named = COUNTRY_BY_NAME.get(parts[-1].lower())
        if named:
            return named

    # 2. Полное название страны где угодно в строке.
    for name, code in COUNTRY_BY_NAME.items():
        if len(name) > 3 and re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return code

    # 3. Двухбуквенный хвост: сперва штаты и провинции, потом страны.
    #
    # Осознанный размен. Часть кодов штатов совпадает с кодами стран: DE, IN, IL,
    # CO, MA, TN, PA, GA. Полные названия стран уже разобраны выше, поэтому
    # оставшийся двухбуквенный хвост в выдаче ATS почти всегда американский штат
    # («Boston, MA» — Массачусетс, а не Марокко). Считаем такие коды штатами.
    # Цена ошибки мала: адаптеры почти везде отдают страну отдельным полем,
    # и эта функция — только фолбэк, когда поля нет.
    for part in reversed(parts):
        if len(part) != 2 or not part.isalpha():
            continue
        upper = part.upper()
        if upper in US_STATES:
            return "us"
        if upper in CA_PROVINCES:
            return "ca"
        if part.lower() in ISO_COUNTRIES:
            return part.lower()

    # 4. Код штата в середине строки: 'Remote - Austin, TX (hybrid)'.
    if US_STATE.search(text):
        return "us"
    return None


def workplace(location: str | None, *flags: Any) -> str | None:
    """remote / hybrid / onsite. Булев флаг площадки главнее текста локации."""
    for flag in flags:
        if isinstance(flag, bool) and flag:
            return "remote"
        if isinstance(flag, str) and flag:
            low = flag.lower()
            if "remote" in low:
                return "remote"
            if "hybrid" in low:
                return "hybrid"
            if "site" in low or "office" in low:
                return "onsite"
    if location:
        if HYBRID_RX.search(location):
            return "hybrid"
        if REMOTE_RX.search(location):
            return "remote"
    return None


def seniority(title: str | None) -> str | None:
    if not title:
        return None
    for level, rx in SENIORITY_RULES:
        if rx.search(title):
            return level
    return None


def clean_title(title: Any) -> str:
    text = strip_html(str(title or "")) or ""
    return text.strip()[:300]
