"""Мост: строка «компания наняла» → `Signal` из антиимитационной рубрики P2.

Без этого моста парсер остаётся просто выгрузкой вакансий. Рубрика в
`lib/signals.py` требует от сигнала того же, что и от письма: проверяемой
конкретики, источника с цитатой, живого TTL и связки с оффером. Здесь сигнал
собирается так, чтобы он проходил эти проверки не случайно, а по построению:

- в утверждении есть **число и срок** («3 вакансии за 9 дней») — это то, что
  ломается при подстановке другой компании, то есть проходит тест подстановки;
- `evidence_url` — ссылка на конкретную вакансию, `evidence_quote` — её тайтл;
- `observed_at` — дата публикации от площадки, TTL типа `hiring` = 45 дней;
- `offer_link` заполняет вызывающий: связка «наём → боль → оффер» зависит от
  клиента, и придумывать её здесь было бы ровно той имитацией, против которой
  рубрика и написана. Без неё ценность сигнала честно режется до 3.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.signals import Signal, score

TYPE = "hiring"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def statement(row: dict, now: datetime | None = None) -> str:
    """Утверждение, которое перестаёт быть верным при подстановке другой компании."""
    now = now or datetime.now(timezone.utc)
    company = row.get("company_name") or row.get("slug")
    count = row.get("new_jobs") or 0
    latest = _parse(row.get("latest_at"))
    days = max(0, (now - latest).days) if latest else None
    titles = [t for t in (row.get("titles") or []) if t][:2]

    roles = f" — {', '.join(titles)}" if titles else ""
    when = f"за последние {days} дн." if days is not None else "недавно"
    plural = "вакансию" if count == 1 else "вакансии" if 2 <= count <= 4 else "вакансий"
    return f"{company} открыла {count} {plural} {when}{roles}"


def to_signal(row: dict, offer_link: str | None = None,
              now: datetime | None = None) -> Signal:
    urls = [u for u in (row.get("urls") or []) if u]
    titles = [t for t in (row.get("titles") or []) if t]
    return Signal(
        type=TYPE,
        statement=statement(row, now),
        level="company",
        evidence_url=urls[0] if urls else row.get("careers_url"),
        evidence_quote=titles[0] if titles else None,
        observed_at=_parse(row.get("latest_at")),
        # Ценность растёт с числом вакансий: одна открытая позиция — слабый повод,
        # пять за две недели — уже заметное движение. Потолок 4, пятёрку в этой
        # рубрике заслуживает только person-level сигнал.
        personalization_value=min(4, 2 + (row.get("new_jobs") or 0) // 3),
        confidence=4,                      # площадка — первоисточник, не догадка
        offer_link=offer_link,
        provider=f"ats:{row.get('source')}",
    )


def evaluate_rows(rows: list[dict], offer_link: str | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Прогнать сигналы через рубрику и вернуть строки со `signal_score`.

    `readiness` тут почти всегда `thin`: рубрика считает лид готовым от двух
    живых сигналов, а наём — один. Это не баг выгрузки, а её честная граница:
    второй сигнал приносит стадия extract, а не парсер вакансий.
    """
    out = []
    for row in rows:
        signal = to_signal(row, offer_link=offer_link, now=now)
        result = score([signal], company_name=row.get("company_name"), now=now)
        evaluated = result["signals"][0]
        out.append({**row,
                    "statement": evaluated.statement,
                    "signal_score": result["signal_score"],
                    "readiness": result["readiness"],
                    "evidence_url": evaluated.evidence_url,
                    "signal_notes": "; ".join(evaluated.notes)})
    return out
