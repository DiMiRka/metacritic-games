"""Ограничение частоты запросов по IP со скользящим окном

Счётчики живут в памяти процесса: при нескольких процессах uvicorn лимит фактически
умножится на их число. Сейчас web работает одним процессом, ограничение описано в README
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.core.config import settings

MAX_KEYS = 50_000


class RateLimiter:
    def __init__(self, name: str, limit: Callable[[], int], window_seconds: float = 60.0) -> None:
        self.name = name
        self._limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def reset(self) -> None:
        self._hits.clear()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def hit(self, key: str, now: float | None = None) -> float | None:
        """Учитывает запрос; если лимит исчерпан, возвращает сколько секунд ждать"""
        now = time.monotonic() if now is None else now
        if len(self._hits) > MAX_KEYS:
            self._prune(now)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self._limit():
            return hits[0] + self.window - now
        hits.append(now)
        return None

    async def __call__(self, request: Request) -> None:
        # За Caddy адрес клиента приходит из X-Forwarded-For, которому доверяет uvicorn
        ip = request.client.host if request.client else "unknown"
        wait = self.hit(ip)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов, попробуйте чуть позже",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )


catalog_limit = RateLimiter("catalog", lambda: settings.rate_limit_catalog_per_minute)
api_limit = RateLimiter("api", lambda: settings.rate_limit_api_per_minute)
login_limit = RateLimiter("login", lambda: settings.rate_limit_login_per_minute)
LIMITERS = (catalog_limit, api_limit, login_limit)
