from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.models import Game
from app.catalog.queries import GamePage
from app.catalog.similar import Reason, SimilarDescription, SimilarGame


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


class NamesReasonOut(BaseModel):
    """Общие жанры, студии или платформы, названия в порядке текущей игры"""

    kind: Literal["genres", "developer", "platforms"]
    values: list[str]


class DescriptionReasonOut(BaseModel):
    """Близкое описание: similarity = 1 − косинусное расстояние эмбеддингов"""

    kind: Literal["description"]
    similarity: float
    level: Literal["close", "very_close"]


ReasonOut = Annotated[NamesReasonOut | DescriptionReasonOut, Field(discriminator="kind")]


def reason_out(reason: Reason) -> NamesReasonOut | DescriptionReasonOut:
    if isinstance(reason, SimilarDescription):
        return DescriptionReasonOut(
            kind="description", similarity=round(reason.similarity, 2), level=reason.level
        )
    return NamesReasonOut(kind=reason.kind, values=list(reason.names))


class SimilarOut(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    # Почему игра попала в похожие. Пусто, если её нашёл эмбеддинг, но ни одна причина
    # не прошла порог, тогда сходство общее: по описанию и жанрам
    reasons: list[ReasonOut]

    @classmethod
    def from_similar(cls, item: SimilarGame) -> "SimilarOut":
        game = item.game
        return cls(
            slug=game.slug,
            title=game.title,
            cover_url=game.cover_url,
            reasons=[reason_out(r) for r in item.reasons],
        )


class GameDetailOut(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    developer: str | None
    description: str | None
    # Перевод описания на русский, пока его нет — null
    description_ru: str | None
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
    def from_game(cls, game: Game, similar: list[SimilarGame]) -> "GameDetailOut":
        summaries = {s.source: SummaryOut.model_validate(s) for s in game.summaries}
        return cls(
            slug=game.slug,
            title=game.title,
            cover_url=game.cover_url,
            developer=game.developer,
            description=game.description,
            description_ru=game.description_ru,
            video_url=game.video_url,
            release_date=game.release_date,
            genres=game.genres,
            platforms=[PlatformOut.model_validate(p) for p in game.platforms],
            critic_summary=summaries.get("critic"),
            user_summary=summaries.get("user"),
            letsplay=LetsplayOut.model_validate(game.letsplay) if game.letsplay else None,
            similar=[SimilarOut.from_similar(s) for s in similar],
            updated_at=game.updated_at,
            last_processed_at=game.last_processed_at,
        )


class ScorePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform: str
    metascore: int | None
    userscore: float | None
    observed_at: datetime
