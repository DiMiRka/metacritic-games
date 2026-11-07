"""Поиск летсплея: YouTube Data API, если задан ключ, иначе поиск yt-dlp без ключа.
Кончилась дневная квота API — тот же запрос уходит в yt-dlp
"""

import asyncio
import re
import unicodedata
from dataclasses import dataclass

import httpx
from loguru import logger

from app.youtube.errors import translate_ytdlp_error

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


def pick_most_popular(
    candidates: list[Candidate], game_title: str, min_duration_seconds: int
) -> Candidate | None:
    """Самый просматриваемый ролик про эту игру, достаточно длинный для летсплея.
    Трейлеры, нарезки и шортсы отсекает длительность. Если среди найденного есть ролики
    с признаками летсплея в названии, выбор идёт среди них"""
    suitable = [
        c
        for c in candidates
        if (c.duration_seconds or 0) >= min_duration_seconds
        and mentions_game(c.title, game_title)
        and not about_other_game(c.title, game_title)
    ]
    marked = [c for c in suitable if looks_like_letsplay(c.title)]
    return max(marked or suitable, key=lambda c: c.views or 0, default=None)


def parse_iso_duration(value: str | None) -> int | None:
    """PT1H2M3S из YouTube Data API в секунды"""
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


class QuotaExceededError(Exception):
    """Дневная квота YouTube Data API исчерпана"""


async def search_api(client: httpx.AsyncClient, api_key: str, query: str) -> list[Candidate]:
    found = await client.get(
        f"{API_URL}/search",
        params={
            "part": "id",
            "q": query,
            "type": "video",
            # По релевантности: при viewCount YouTube почти не учитывает «let's play» в запросе
            # и отдаёт просто популярные ролики про игру. Популярность выбирается уже среди них
            "order": "relevance",
            "maxResults": SEARCH_RESULTS,
            "key": api_key,
        },
    )
    if found.status_code == 403 and "quota" in found.text.lower():
        raise QuotaExceededError(found.text[:200])
    found.raise_for_status()
    ids = [item["id"]["videoId"] for item in found.json().get("items", [])]
    if not ids:
        return []
    details = await client.get(
        f"{API_URL}/videos",
        params={"part": "snippet,statistics,contentDetails", "id": ",".join(ids), "key": api_key},
    )
    details.raise_for_status()
    return [
        Candidate(
            video_id=item["id"],
            title=item["snippet"]["title"],
            channel=item["snippet"].get("channelTitle"),
            views=int(item.get("statistics", {}).get("viewCount", 0)) or None,
            duration_seconds=parse_iso_duration(item.get("contentDetails", {}).get("duration")),
        )
        for item in details.json().get("items", [])
    ]


def _search_ytdlp_sync(query: str, options: dict) -> list[Candidate]:
    import yt_dlp

    params = {**options, "extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            found = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise translate_ytdlp_error(exc) from exc
    return [
        Candidate(
            video_id=entry["id"],
            title=entry.get("title") or "",
            channel=entry.get("channel") or entry.get("uploader"),
            views=entry.get("view_count"),
            duration_seconds=int(entry["duration"]) if entry.get("duration") else None,
        )
        for entry in found.get("entries") or []
        if entry.get("id")
    ]


async def search_ytdlp(query: str, options: dict) -> list[Candidate]:
    return await asyncio.to_thread(_search_ytdlp_sync, query, options)


async def search(
    client: httpx.AsyncClient, api_key: str, query: str, ytdlp_options: dict
) -> list[Candidate]:
    if api_key:
        try:
            return await search_api(client, api_key, query)
        except QuotaExceededError:
            logger.warning("квота YouTube Data API исчерпана, поиск через yt-dlp")
    return await search_ytdlp(query, ytdlp_options)
