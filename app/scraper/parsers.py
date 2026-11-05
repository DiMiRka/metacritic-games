from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

IMAGE_BASE = "https://www.metacritic.com/a/img/catalog"
# Хосты взяты из реальных ответов Metacritic (tests/fixtures/composer_*.json)
IMAGE_HOSTS = frozenset({"www.metacritic.com"})
VIDEO_HOSTS = frozenset({"cdn.jwplayer.com"})
YOUTUBE_HOSTS = frozenset({"www.youtube.com"})


def safe_url(value: Any, allowed_hosts: Collection[str]) -> str | None:
    """Пропускает только https-ссылку на разрешённый хост, иначе None"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if (parts.hostname or "").lower() not in allowed_hosts:
        return None
    return value


@dataclass(frozen=True)
class PlatformScore:
    platform: str
    name: str
    metascore: int | None
    userscore: float | None = None


@dataclass
class GameData:
    slug: str
    title: str
    cover_url: str | None
    developer: str | None
    description: str | None
    video_url: str | None
    release_date: date | None
    genres: list[str]
    platforms: list[PlatformScore]
    lead_platform: str | None = None
    lead_userscore: float | None = None
    critic_reviews_total: int = 0
    user_reviews_total: int = 0


@dataclass(frozen=True)
class Review:
    text: str
    score: float | None
    author: str | None


def parse_listing(data: dict[str, Any]) -> list[str]:
    items = (data.get("data") or {}).get("items") or []
    return list(dict.fromkeys(item["slug"] for item in items if item.get("slug")))


def _components(data: dict[str, Any]) -> dict[str, Any]:
    return {
        comp.get("meta", {}).get("componentName"): comp.get("data") or {}
        for comp in data.get("components") or []
    }


def _cover_url(images: list[dict[str, Any]]) -> str | None:
    by_type = {img.get("typeName"): img for img in images if img.get("bucketPath")}
    image = by_type.get("cardImage") or by_type.get("mainImage")
    if image is None and images:
        image = next((img for img in images if img.get("bucketPath")), None)
    path = str(image["bucketPath"]) if image else ""
    return safe_url(f"{IMAGE_BASE}{path}", IMAGE_HOSTS) if path.startswith("/") else None


def _developer(production: dict[str, Any] | None) -> str | None:
    companies = (production or {}).get("companies") or []
    names = dict.fromkeys(c["name"] for c in companies if c.get("typeName") == "Developer")
    return ", ".join(names) or None


def _release_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _summary_platform(summary: dict[str, Any]) -> str | None:
    url = (summary.get("item") or {}).get("url") or ""
    return (parse_qs(urlparse(url).query).get("platform") or [None])[0]


def parse_user_score(data: dict[str, Any]) -> float | None:
    item = (data.get("data") or data).get("item") or {}
    # При малом числе отзывов сайт показывает «tbd»: API отдаёт score 0 и пустой sentiment
    if not item.get("reviewCount") or not item.get("score") or not item.get("sentiment"):
        return None
    return float(item["score"])


def parse_game(data: dict[str, Any]) -> GameData | None:
    components = _components(data)
    item = (components.get("product") or {}).get("item")
    if not item or not item.get("slug"):
        return None

    platforms = [
        PlatformScore(
            platform=p["slug"],
            name=p.get("name") or p["slug"],
            metascore=(p.get("criticScoreSummary") or {}).get("score"),
        )
        for p in item.get("platforms") or []
        if p.get("slug")
    ]
    video = item.get("video") or {}
    user_summary = components.get("user-score-summary") or {}

    return GameData(
        slug=item["slug"],
        title=item.get("title") or item["slug"],
        cover_url=_cover_url(item.get("images") or []),
        developer=_developer(item.get("production")),
        description=(item.get("description") or "").strip() or None,
        video_url=safe_url(video.get("embedUrl"), VIDEO_HOSTS)
        or safe_url(video.get("manifestUrl"), VIDEO_HOSTS),
        release_date=_release_date(item.get("releaseDate")),
        genres=[g["name"] for g in item.get("genres") or [] if g.get("name")],
        platforms=platforms,
        lead_platform=_summary_platform(user_summary),
        lead_userscore=parse_user_score(user_summary),
        critic_reviews_total=(components.get("critic-reviews") or {}).get("totalResults") or 0,
        user_reviews_total=(components.get("user-reviews") or {}).get("totalResults") or 0,
    )


def parse_reviews(items: list[dict[str, Any]]) -> list[Review]:
    reviews = []
    for item in items:
        text = (item.get("quote") or "").strip()
        if not text:
            continue
        author = item.get("publicationName") or item.get("author") or None
        score = item.get("score")
        reviews.append(
            Review(text=text, score=None if score is None else float(score), author=author)
        )
    return reviews
