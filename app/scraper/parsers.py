from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

IMAGE_BASE = "https://www.metacritic.com/a/img/catalog"


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
    return f"{IMAGE_BASE}{image['bucketPath']}" if image else None


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
        video_url=video.get("embedUrl") or video.get("manifestUrl"),
        release_date=_release_date(item.get("releaseDate")),
        genres=[g["name"] for g in item.get("genres") or [] if g.get("name")],
        platforms=platforms,
        lead_platform=_summary_platform(user_summary),
        lead_userscore=parse_user_score(user_summary),
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
