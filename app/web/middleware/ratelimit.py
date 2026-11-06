"""Ограничение частоты запросов по IP со скользящим окном

Лимит берётся из настроек приложения, счётчики живут в app.state: у каждого приложения
свои. Они в памяти процесса, при нескольких процессах uvicorn лимит фактически умножится
на их число. Сейчас web работает одним процессом, ограничение описано в README
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

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


class IpRateLimit:
    """Зависимость FastAPI: лимит по IP из настройки приложения с именем setting"""

    def __init__(self, name: str, setting: str) -> None:
        self.name = name
        self.setting = setting

    def limiter(self, request: Request) -> RateLimiter:
        limiters: dict[str, RateLimiter] = request.app.state.rate_limiters
        if self.name not in limiters:
            config = request.app.state.settings
            limiters[self.name] = RateLimiter(self.name, lambda: getattr(config, self.setting))
        return limiters[self.name]

    async def __call__(self, request: Request) -> None:
        # За Caddy адрес клиента приходит из X-Forwarded-For, которому доверяет uvicorn
        ip = request.client.host if request.client else "unknown"
        wait = self.limiter(request).hit(ip)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов, попробуйте чуть позже",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )


catalog_limit = IpRateLimit("catalog", "rate_limit_catalog_per_minute")
api_limit = IpRateLimit("api", "rate_limit_api_per_minute")
login_limit = IpRateLimit("login", "rate_limit_login_per_minute")
