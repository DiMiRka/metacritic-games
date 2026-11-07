from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.catalog.models import Game
from app.catalog.queries import GamePage


class PlatformOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform: str
    platform_name: str
    metascore: int | None
    userscore: float | None


class GameListItem(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    release_date: date | None
    metascore: int | None
    userscore: float | None
    platforms: list[PlatformOut]


class GameListOut(BaseModel):
    items: list[GameListItem]
    total: int
    page: int
    pages: int

    @classmethod
    def from_page(cls, page: GamePage) -> "GameListOut":
        return cls(
            items=[
                GameListItem(
                    slug=row.game.slug,
                    title=row.game.title,
                    cover_url=row.game.cover_url,
                    release_date=row.game.release_date,
                    metascore=row.metascore,
                    userscore=row.userscore,
                    platforms=[PlatformOut.model_validate(p) for p in row.game.platforms],
                )
                for row in page.items
            ],
            total=page.total,
            page=page.page,
            pages=page.pages,
        )


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    likes: list[str]
    dislikes: list[str]
    reviews_count: int
    model: str
    generated_at: datetime


class LetsplayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    video_url: str | None
    title: str | None
    channel: str | None
    views: int | None
    duration_seconds: int | None
    conclusion: str | None
    verdict: str | None
    # captions: субтитры ролика, whisper: речь распознана сервисом
    transcript_source: str | None
    language: str | None
    checked_at: datetime | None


class SimilarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    title: str
    cover_url: str | None


class GameDetailOut(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    developer: str | None
    description: str | None
    video_url: str | None
    release_date: date | None
    genres: list[str]
    platforms: list[PlatformOut]
    critic_summary: SummaryOut | None
    user_summary: SummaryOut | None
    letsplay: LetsplayOut | None
    similar: list[SimilarOut]
    updated_at: datetime
    last_processed_at: datetime | None

    @classmethod
    def from_game(cls, game: Game, similar: list[Game]) -> "GameDetailOut":
        summaries = {s.source: SummaryOut.model_validate(s) for s in game.summaries}
        return cls(
            slug=game.slug,
            title=game.title,
            cover_url=game.cover_url,
            developer=game.developer,
            description=game.description,
            video_url=game.video_url,
            release_date=game.release_date,
            genres=game.genres,
            platforms=[PlatformOut.model_validate(p) for p in game.platforms],
            critic_summary=summaries.get("critic"),
            user_summary=summaries.get("user"),
            letsplay=LetsplayOut.model_validate(game.letsplay) if game.letsplay else None,
            similar=[SimilarOut.model_validate(g) for g in similar],
            updated_at=game.updated_at,
            last_processed_at=game.last_processed_at,
        )


class ScorePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform: str
    metascore: int | None
    userscore: float | None
    observed_at: datetime
