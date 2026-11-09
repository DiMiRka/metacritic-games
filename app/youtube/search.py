"""Поиск летсплея через YouTube Data API: search по релевантности, затем videos за точными
просмотрами и длительностью. Без ключа летсплеи не ищутся. yt-dlp здесь не участвует:
он нужен только для текста и звука ролика, которых API для чужих роликов не отдаёт
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from app.core.errors import RetryableError
from app.youtube.errors import YouTubeApiConfigError, YouTubeApiError

API_URL = "https://www.googleapis.com/youtube/v3"
SEARCH_RESULTS = 25
# Служебные слова не обязаны быть в названии ролика: «The Legend of Zelda» найдётся как «Zelda»
STOP_WORDS = frozenset({"the", "a", "an", "of", "and", "edition", "remastered", "remake"})
# Признаки прохождения в названии ролика
LETSPLAY_MARKERS = re.compile(
    r"let'?s ?play|playthrough|walkthrough|blind|gameplay|full game|part ?\d|#\d|episode|"
    r"прохождение|летсплей|стрим",
    re.IGNORECASE,
)
# Игры-площадки для пародий и модов: «играю за Hades в Minecraft» — это ролик не про Hades
OTHER_GAMES = frozenset({"minecraft", "roblox", "fortnite", "gmod", "sims", "terraria"})


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    channel: str | None
    views: int | None
    duration_seconds: int | None
    # 18+ на YouTube: без входа в аккаунт ни субтитров, ни звука не получить
    age_restricted: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def query_for(game_title: str) -> str:
    return f"{game_title} let's play"


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).lower().replace("'", "")
    return set(re.findall(r"[^\W_]+", text))


def mentions_game(video_title: str, game_title: str) -> bool:
    """В названии ролика есть все значимые слова названия игры"""
    wanted = _words(game_title) - STOP_WORDS or _words(game_title)
    return wanted <= _words(video_title)


def looks_like_letsplay(video_title: str) -> bool:
    return bool(LETSPLAY_MARKERS.search(video_title))


def about_other_game(video_title: str, game_title: str) -> bool:
    """Ролик про другую игру, где наша только упомянута: «Hades в Minecraft»"""
    return bool((_words(video_title) & OTHER_GAMES) - _words(game_title))


def rank_candidates(
    candidates: list[Candidate], game_title: str, min_duration_seconds: int
) -> list[Candidate]:
    """Подходящие ролики про эту игру, от лучшего к худшему: сначала с признаками
    прохождения в названии, внутри — по просмотрам. Трейлеры, нарезки и шортсы отсекает
    длительность. Следующие по списку пригодятся, если в первом нет речи"""
    suitable = [
        c
        for c in candidates
        if (c.duration_seconds or 0) >= min_duration_seconds
        and not c.age_restricted
        and mentions_game(c.title, game_title)
        and not about_other_game(c.title, game_title)
    ]
    return sorted(suitable, key=lambda c: (not looks_like_letsplay(c.title), -(c.views or 0)))


def pick_most_popular(
    candidates: list[Candidate], game_title: str, min_duration_seconds: int
) -> Candidate | None:
    ranked = rank_candidates(candidates, game_title, min_duration_seconds)
    return ranked[0] if ranked else None


def parse_iso_duration(value: str | None) -> int | None:
    """PT1H2M3S из YouTube Data API в секунды"""
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


class QuotaExceededError(RetryableError):
    """Дневная квота YouTube Data API исчерпана: летсплеи ждут её сброса"""


# Причины из ответа API. Квота сбрасывается в полночь по тихоокеанскому времени
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"})
# Ключ неверный, API не включён, ключ ограничен другим IP или приложением: само не пройдёт
CONFIG_REASONS = frozenset(
    {
        "keyInvalid",
        "keyExpired",
        "accessNotConfigured",
        "forbidden",
        "ipRefererBlocked",
        "API_KEY_INVALID",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "API_KEY_SERVICE_BLOCKED",
        "SERVICE_DISABLED",
    }
)
PACIFIC = ZoneInfo("America/Los_Angeles")


def _reasons(response: httpx.Response) -> set[str]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return set()
    reasons = {e.get("reason") for e in error.get("errors", []) if e.get("reason")}
    reasons |= {d.get("reason") for d in error.get("details", []) if d.get("reason")}
    if status := error.get("status"):
        reasons.add(status)
    return reasons


def next_quota_reset(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(PACIFIC)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=PACIFIC)
    return midnight.astimezone(UTC)


async def api_get(client: httpx.AsyncClient, api_key: str, method: str, params: dict) -> dict:
    """Запрос к YouTube Data API. Ключ уходит заголовком, а не в URL: текст ошибки с адресом
    запроса попадает в журналы, монитор и Sentry, и ключ не должен оказаться там"""
    response = await client.get(
        f"{API_URL}/{method}", params=params, headers={"X-Goog-Api-Key": api_key}
    )
    if response.is_success:
        return response.json()
    reasons = _reasons(response)
    if reasons & QUOTA_REASONS:
        raise QuotaExceededError(f"{method}: {sorted(reasons)}")
    if response.status_code in (400, 401, 403) and (
        reasons & CONFIG_REASONS or response.status_code in (401, 403)
    ):
        raise YouTubeApiConfigError(
            f"YouTube Data API {method}: HTTP {response.status_code} {sorted(reasons)}. "
            "Проверьте YOUTUBE_API_KEY, ограничения ключа и что YouTube Data API v3 включён"
        )
    raise YouTubeApiError(
        response.status_code, f"{method}: HTTP {response.status_code} {sorted(reasons)}"
    )


async def search_api(client: httpx.AsyncClient, api_key: str, query: str) -> list[Candidate]:
    found = await api_get(
        client,
        api_key,
        "search",
        {
            "part": "id",
            "q": query,
            "type": "video",
            # По релевантности: при viewCount YouTube почти не учитывает «let's play» в запросе
            # и отдаёт просто популярные ролики про игру. Популярность выбирается уже среди них
            "order": "relevance",
            "maxResults": SEARCH_RESULTS,
        },
    )
    ids = [item["id"]["videoId"] for item in found.get("items", [])]
    if not ids:
        return []
    details = await api_get(
        client,
        api_key,
        "videos",
        {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)},
    )
    return [
        Candidate(
            video_id=item["id"],
            title=item["snippet"]["title"],
            channel=item["snippet"].get("channelTitle"),
            views=int(item.get("statistics", {}).get("viewCount", 0)) or None,
            duration_seconds=parse_iso_duration(item.get("contentDetails", {}).get("duration")),
            age_restricted=item.get("contentDetails", {}).get("contentRating", {}).get("ytRating")
            == "ytAgeRestricted",
        )
        for item in details.get("items", [])
    ]


class SearchQuota:
    """Помнит, что квота кончилась: до сброса API не вызывается вовсе.
    Хранится в памяти процесса: после перезапуска это будет стоить один запрос"""

    def __init__(self) -> None:
        self.exhausted_until: datetime | None = None

    def available(self, now: datetime | None = None) -> bool:
        return self.exhausted_until is None or (now or datetime.now(UTC)) >= self.exhausted_until

    def exhausted(self, now: datetime | None = None) -> None:
        self.exhausted_until = next_quota_reset(now)


async def search(
    client: httpx.AsyncClient, api_key: str, query: str, quota: SearchQuota | None = None
) -> list[Candidate]:
    """Пока квота исчерпана, API не вызывается: ошибка сразу, автомат youtube откладывает
    задачи до сброса без траты попыток"""
    quota = quota or SearchQuota()
    if not quota.available():
        raise QuotaExceededError(
            f"квота YouTube Data API исчерпана до {quota.exhausted_until:%Y-%m-%d %H:%M} UTC"
        )
    try:
        return await search_api(client, api_key, query)
    except QuotaExceededError:
        quota.exhausted()
        logger.warning(
            "квота YouTube Data API исчерпана до {:%Y-%m-%d %H:%M} UTC", quota.exhausted_until
        )
        raise
