"""Сигналы для персонализации: оценка, свежесть, антиимитационная рубрика.

Рубрика сигналов найма (company research → person research → скоринг →
выбор якоря) в машинной форме: правила, проверяемые функциями, чтобы сигнал
нельзя было пропустить в письмо «на глазок».

Ключевая идея (план, 2.9): персонализация — это не факт упоминания компании,
а утверждение, которое ПЕРЕСТАЁТ БЫТЬ ВЕРНЫМ при подстановке другой компании.
Всё остальное — имитация, и она хуже её отсутствия: получатель узнаёт шаблон.

signal_value = personalization_value(1..5) × confidence(1..5) × freshness(0..1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# TTL по типам (план, 2.9). Пост двухмесячной давности в письме хуже, чем
# отсутствие сигнала: он показывает, что письмо готовили давно и не глядя.
TTL_DAYS: dict[str, int] = {
    "post": 21,
    "reaction": 14,
    "comment": 14,
    "job_change": 90,
    "hiring": 45,
    "funding": 180,
    "new_case": 150,
    "new_service": 180,
    "market_entry": 180,
    "integration": 180,
    "pricing_change": 120,
    "growth": 120,
    "news": 60,
}
DEFAULT_TTL_DAYS = 90

# Стоп-лист банальностей: формулировки, верные для кого угодно (правило 5).
BANALITY_PATTERNS = [
    r"\bувидел[а]? ваш\b", r"\bsaw your (profile|website|company)\b",
    r"\bfound (you|your company) on\b", r"\bнаш[её]л вас\b",
    r"\bwork(s|ing)? in the .{0,30} (industry|space|sector)\b",
    r"\bработаете в сфере\b", r"\bу вас есть сайт\b",
    r"\byour team of \d+\b", r"\bкоманда из \d+\b",
    r"\bimpressive (work|growth|team)\b", r"\bвпечатляющ",
    r"\blove what you('| a)re doing\b", r"\bнравится, что вы делаете\b",
    r"\bреальный лидер (рынка|отрасли)\b", r"\bleader in the\b",
]
_BANALITY = [re.compile(p, re.IGNORECASE) for p in BANALITY_PATTERNS]

READY_MIN_SIGNALS = 2
READY_MIN_SCORE = 40          # из 100, после нормировки двух лучших сигналов


@dataclass
class Signal:
    """Один сигнал. Без evidence_url и цитаты confidence принудительно низкий."""
    type: str
    statement: str
    level: str = "company"                   # company | person
    evidence_url: str | None = None
    evidence_quote: str | None = None
    observed_at: datetime | None = None
    personalization_value: int = 3           # 1..5, до проверок
    confidence: int = 3                      # 1..5, до проверок
    ttl_days: int | None = None
    offer_link: str | None = None            # чем сигнал связан с оффером
    provider: str | None = None
    notes: list[str] = field(default_factory=list)


def is_banal(text: str) -> bool:
    """Утверждение верно почти для любой компании — это не персонализация."""
    return any(rx.search(text or "") for rx in _BANALITY)


def substitution_test(statement: str, company_name: str | None,
                      other_name: str = "Acme Corp") -> bool:
    """Тест подстановки (правило 1). True — сигнал ПРОШЁЛ, то есть специфичен.

    Механическая проверка: убираем имя компании и смотрим, осталось ли в
    утверждении хоть что-то проверяемое — число, дата, имя собственное, цитата,
    ссылка. Фраза «работают в финтехе» после подстановки остаётся верной и
    ничего такого не содержит — значит, это не персонализация.

    Это грубый фильтр от заведомого шаблона, а не замена смыслового суждения:
    финальную оценку даёт LLM или человек. Задача функции — не пропустить
    очевидную имитацию дальше по конвейеру.
    """
    text = (statement or "").strip()
    if not text:
        return False
    if is_banal(text):
        return False
    if company_name:
        text = re.sub(re.escape(company_name), other_name, text, flags=re.IGNORECASE)
    # Проверяемая конкретика: числа, годы, суммы, проценты, кавычки, ссылки,
    # либо имя собственное, не совпадающее с подставленным.
    if re.search(r"\d", text) or re.search(r"[«\"“].+[»\"”]", text) or "http" in text:
        return True
    proper = re.findall(r"\b[A-ZА-Я][a-zа-яё]{2,}\b", text)
    return any(p.lower() not in other_name.lower() for p in proper)


def freshness(signal: Signal, now: datetime | None = None) -> float:
    """1.0 — сегодня, 0.0 — за пределами TTL. Линейно между."""
    if signal.observed_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    observed = signal.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    ttl = signal.ttl_days or TTL_DAYS.get(signal.type, DEFAULT_TTL_DAYS)
    age_days = (now - observed) / timedelta(days=1)
    if age_days < 0:
        return 1.0
    return max(0.0, 1.0 - age_days / ttl)


def evaluate(signal: Signal, company_name: str | None = None,
             now: datetime | None = None) -> Signal:
    """Прогнать сигнал через рубрику и вернуть его с исправленными оценками.

    Правила из плана (2.9), каждое понижает оценку и оставляет след в notes:
      1. Тест подстановки не пройден    → personalization_value <= 2
      2. Вне TTL                        → freshness 0, сигнал мёртв
      3. Нет связи с оффером            → personalization_value <= 3
      4. Нет источника или цитаты       → confidence <= 2
      5. Банальность из стоп-листа      → personalization_value = 1
    """
    result = Signal(**{**signal.__dict__, "notes": list(signal.notes)})

    if not result.evidence_url or not result.evidence_quote:
        if result.confidence > 2:
            result.confidence = 2
        result.notes.append("нет источника или цитаты → confidence ≤ 2")

    if is_banal(result.statement):
        result.personalization_value = 1
        result.notes.append("банальность из стоп-листа → ценность 1")
    elif not substitution_test(result.statement, company_name):
        if result.personalization_value > 2:
            result.personalization_value = 2
        result.notes.append("не прошёл тест подстановки → ценность ≤ 2")

    if not result.offer_link:
        if result.personalization_value > 3:
            result.personalization_value = 3
        result.notes.append("нет связки «сигнал → боль → оффер» → ценность ≤ 3")

    if freshness(result, now) <= 0:
        result.notes.append("вне TTL — в письмо не идёт")

    return result


def signal_value(signal: Signal, now: datetime | None = None) -> float:
    """0..25 — вклад одного сигнала (5 × 5 × свежесть)."""
    return signal.personalization_value * signal.confidence * freshness(signal, now)


def score(signals: list[Signal], company_name: str | None = None,
          now: datetime | None = None) -> dict:
    """signal_score 0..100 по двум лучшим живым сигналам + готовность лида.

    Два, а не все: третий сигнал письмо не улучшает, а объём базы съедает.
    Сколько на самом деле стоит второй сигнал — отдельная гипотеза для P1
    (трек `thin` против `ready`).
    """
    evaluated = [evaluate(s, company_name, now) for s in signals]
    alive = [s for s in evaluated if freshness(s, now) > 0]
    ranked = sorted(alive, key=lambda s: -signal_value(s, now))
    best = ranked[:2]

    raw = sum(signal_value(s, now) for s in best)
    normalized = int(round(min(100.0, raw / 50.0 * 100)))   # максимум 2 × 25 = 50

    if len(best) >= READY_MIN_SIGNALS and normalized >= READY_MIN_SCORE:
        readiness = "ready"
    elif len(best) >= 1:
        readiness = "thin"
    else:
        readiness = "generic"

    return {
        "signal_score": normalized,
        "readiness": readiness,
        "alive": len(alive),
        "total": len(evaluated),
        "signals": evaluated,
        "best": best,
        "reasons": [n for s in evaluated for n in s.notes],
    }
