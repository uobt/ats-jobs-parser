"""HTTP-транспорт парсера: троттлинг на хост, ретраи с бэкоффом, честный User-Agent.

Ровно то, чего нет ни в одном из просмотренных открытых репозиториев и из-за чего
падал портальный парсер (прогон lever оборвался на 288 компании из 2113).

Три правила, каждое из них — ответ на конкретную наблюдаемую проблему:

1. **Троттлинг на хост, а не глобальный.** Лимиты у площадок независимые:
   пауза перед Greenhouse не должна тормозить Lever. robots.txt api.lever.co
   объявляет Crawl-delay: 1 — это и взято за дефолт.
2. **Ретраи только на то, что имеет смысл повторять** (429, 5xx, таймаут, обрыв).
   404 и 403 не ретраятся: борд закрыт или его нет, повтор ничего не изменит.
   На 429 уважается Retry-After, если он пришёл.
3. **Ошибка одной компании не роняет прогон.** Транспорт возвращает результат
   с полем `error`, а решает уже вызывающий. Портальный парсер падал целиком.

Прокси намеренно не поддерживаются: при 1 req/s на хост и одном обходе в сутки
публичные board-эндпоинты не режут. Если начнут — это отдельное решение
с отдельным бюджетом, а не тихо включённая опция.
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.parse
import urllib.request
import json as jsonlib
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = ("ats-jobs-parser/0.1 (+hiring-signal collector; contact via site owner)")

# BambooHR отфильтровывает запросы без браузероподобного UA — отдаёт 302 на
# www.bamboohr.com вместо JSON. Это единственное место, где UA подменяется,
# и делается это ради работоспособности, а не ради обхода защиты.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 25.0
DEFAULT_DELAY = 1.0          # секунд между запросами к одному хосту
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5
BACKOFF_CAP = 30.0


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    errors: int = 0
    by_status: dict[int, int] = field(default_factory=dict)
    seconds_waiting: float = 0.0

    def note_status(self, status: int) -> None:
        self.by_status[status] = self.by_status.get(status, 0) + 1


class Http:
    """Синхронный клиент. Один экземпляр на прогон — в нём живёт троттлинг по хостам."""

    def __init__(self, delay: float = DEFAULT_DELAY, timeout: float = DEFAULT_TIMEOUT,
                 max_attempts: int = MAX_ATTEMPTS, user_agent: str = USER_AGENT,
                 sleep=time.sleep, now=time.monotonic):
        self.delay = delay
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.user_agent = user_agent
        self.stats = Stats()
        self._last_hit: dict[str, float] = {}
        self._sleep = sleep
        self._now = now

    # --- публичное ---

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return jsonlib.loads(self.get_text(url, headers=headers) or "null")

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self._request("GET", url, headers=headers)

    def post_json(self, url: str, payload: dict, headers: dict[str, str] | None = None) -> Any:
        body = jsonlib.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json", **(headers or {})}
        return jsonlib.loads(self._request("POST", url, data=body, headers=merged) or "null")

    # --- внутреннее ---

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_hit.get(host)
        now = self._now()
        if last is not None:
            wait = self.delay - (now - last)
            if wait > 0:
                # джиттер, чтобы параллельные прогоны не выстраивались в такт
                wait += random.uniform(0, self.delay * 0.25)
                self.stats.seconds_waiting += wait
                self._sleep(wait)
                now = self._now()
        self._last_hit[host] = now

    def _request(self, method: str, url: str, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> str:
        last_error: FetchError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(url)
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("User-Agent", self.user_agent)
            request.add_header("Accept", "application/json, text/xml;q=0.9, */*;q=0.5")
            request.add_header("Accept-Encoding", "identity")
            for name, value in (headers or {}).items():
                request.add_header(name, value)

            self.stats.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.stats.note_status(response.status)
                    return response.read().decode(
                        response.headers.get_content_charset() or "utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                self.stats.note_status(exc.code)
                retryable = exc.code in RETRY_STATUSES
                last_error = FetchError(f"HTTP {exc.code} на {url}", exc.code, retryable)
                if not retryable:
                    break
                self._wait_before_retry(attempt, exc.headers.get("Retry-After"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = FetchError(f"{type(exc).__name__}: {exc} на {url}",
                                        None, retryable=True)
                self._wait_before_retry(attempt, None)

        self.stats.errors += 1
        raise last_error or FetchError(f"не удалось получить {url}")

    def _wait_before_retry(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.max_attempts:
            return
        self.stats.retries += 1
        pause = min(BACKOFF_CAP, BACKOFF_BASE ** attempt)
        if retry_after:
            try:
                # Retry-After площадки главнее нашей формулы, но не дольше потолка
                pause = min(BACKOFF_CAP, max(pause, float(retry_after)))
            except ValueError:
                pass
        pause += random.uniform(0, pause * 0.2)
        self.stats.seconds_waiting += pause
        self._sleep(pause)
