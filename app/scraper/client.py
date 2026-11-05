import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BASE_URL = "https://backend.metacritic.com"
NEW_RELEASES_LIMIT = 20
BROWSE_PAGE_SIZE = 24
CRITIC_PAGE_SIZE = 10
USER_PAGE_SIZE = 30
MAX_RETRY_AFTER = 120.0

_GAMES_FINDER = {"sortBy": "-releaseDate", "mcoTypeId": 13}


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - (now or datetime.now(UTC))).total_seconds()
    return min(max(delta, 0.0), MAX_RETRY_AFTER)


class MetacriticClient:
    def __init__(
        self,
        *,
        delay: float | None = None,
        max_retries: int = 4,
        backoff: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._delay = settings.request_delay if delay is None else delay
        self._max_retries = max_retries
        self._backoff = backoff
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    async def __aenter__(self) -> "MetacriticClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self._delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        return self._backoff * 2**attempt * random.uniform(0.75, 1.25)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            await self._throttle()
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if last:
                    raise
                pause = self._backoff_delay(attempt)
                log.warning("GET %s failed (%s), retry in %.1fs", path, exc, pause)
                await asyncio.sleep(pause)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if last:
                    response.raise_for_status()
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                pause = self._backoff_delay(attempt) if retry_after is None else retry_after
                log.warning("GET %s -> %s, retry in %.1fs", path, response.status_code, pause)
                await asyncio.sleep(pause)
                continue

            response.raise_for_status()
            return response.json()
        raise AssertionError("unreachable")

    async def new_releases(self) -> dict[str, Any]:
        params = {**_GAMES_FINDER, "metaScoreMin": 1, "offset": 0, "limit": NEW_RELEASES_LIMIT}
        return await self.get_json("/finder/metacritic/web", params)

    async def browse(self, page: int) -> dict[str, Any]:
        params = {
            **_GAMES_FINDER,
            "productType": "games",
            "offset": (page - 1) * BROWSE_PAGE_SIZE,
            "limit": BROWSE_PAGE_SIZE,
        }
        return await self.get_json("/finder/metacritic/web", params)

    async def game(self, slug: str) -> dict[str, Any]:
        return await self.get_json(f"/composer/metacritic/pages/games/{slug}/web")

    async def user_stats(self, slug: str, platform: str) -> dict[str, Any]:
        return await self.get_json(
            f"/reviews/metacritic/user/games/{slug}/platform/{platform}/stats/web"
        )

    async def critic_reviews(self, slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return await self._reviews(
            f"/reviews/metacritic/critic/games/{slug}/web", limit, CRITIC_PAGE_SIZE
        )

    async def user_reviews(self, slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return await self._reviews(
            f"/reviews/metacritic/user/games/{slug}/web", limit, USER_PAGE_SIZE
        )

    async def _reviews(self, path: str, limit: int, page_size: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while len(items) < limit:
            params = {"offset": len(items), "limit": page_size, "sort": "date"}
            data = (await self.get_json(path, params)).get("data") or {}
            page = data.get("items") or []
            items.extend(page)
            if len(page) < page_size or len(items) >= (data.get("totalResults") or 0):
                break
        return items[:limit]
