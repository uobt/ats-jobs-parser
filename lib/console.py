"""Консоль Windows по умолчанию в cp1251 и падает на любом символе вне неё
(→, ⛔, ✓, длинное тире). Скрипты печатают по-русски и со стрелками, поэтому
вывод принудительно переводится в UTF-8 при старте.

Вызывается первой строкой main() каждого CLI-скрипта.
"""

from __future__ import annotations

import sys


def init() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Поток перенаправлен во что-то, что не умеет reconfigure — не критично.
            pass
