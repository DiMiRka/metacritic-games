"""Получение игры с Metacritic и сохранение её в каталог"""

from datetime import datetime

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import Game, GamePlatform, GameReview, ReviewSource
from app.core.errors import PermanentError
from app.scraper.client import MetacriticClient
from app.scraper.parsers import (
    GameData,
    PlatformScore,
    Review,
    parse_game,
    parse_reviews,
    parse_user_score,
)

REVIEWS_LIMIT = 30


class GameNotFoundError(PermanentError):
    pass


class ScrapeFormatError(PermanentError):
    """Ответ Metacritic не удалось разобрать: скорее всего, поменялся формат"""


_PARSE_ERRORS = (KeyError, TypeError, AttributeError, ValueError)


async def fetch_game(client: MetacriticClient, slug: str) -> GameData:
    raw = await client.game(slug)
    try:
        data = parse_game(raw)
    except _PARSE_ERRORS as exc:
        raise ScrapeFormatError(f"карточка {slug}: {exc!r}") from exc
    if data is None:
        raise GameNotFoundError(slug)

    platforms = []
    for p in data.platforms:
        if p.platform == data.lead_platform:
            userscore = data.lead_userscore
        else:
            stats = await client.user_stats(slug, p.platform)
            try:
                userscore = parse_user_score(stats)
            except _PARSE_ERRORS as exc:
                raise ScrapeFormatError(f"оценки {slug}/{p.platform}: {exc!r}") from exc
        platforms.append(PlatformScore(p.platform, p.name, p.metascore, userscore))
    data.platforms = platforms
    return data


async def fetch_reviews(
    client: MetacriticClient, data: GameData
) -> dict[ReviewSource, list[Review]]:
    reviews = {ReviewSource.CRITIC: [], ReviewSource.USER: []}
    try:
        if data.critic_reviews_total:
            reviews[ReviewSource.CRITIC] = parse_reviews(
                await client.critic_reviews(data.slug, REVIEWS_LIMIT)
            )
        if data.user_reviews_total:
            reviews[ReviewSource.USER] = parse_reviews(
                await client.user_reviews(data.slug, REVIEWS_LIMIT)
            )
    except _PARSE_ERRORS as exc:
        raise ScrapeFormatError(f"отзывы {data.slug}: {exc!r}") from exc
    return reviews


async def upsert_game(
    session: AsyncSession, data: GameData, processed_at: datetime
) -> tuple[int, bool]:
    """Игра и её платформы; эмбеддинг не трогается, его пересчитывает отдельная задача"""
    values = {
        "slug": data.slug,
        "title": data.title,
        "cover_url": data.cover_url,
        "developer": data.developer,
        "description": data.description,
        "video_url": data.video_url,
        "release_date": data.release_date,
        "genres": data.genres,
        "last_processed_at": processed_at,
    }
    stmt = insert(Game).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Game.slug],
        set_={**{k: stmt.excluded[k] for k in values if k != "slug"}, "updated_at": func.now()},
    ).returning(Game.id, literal_column("(xmax = 0)").label("inserted"))
    game_id, inserted = (await session.execute(stmt)).one()

    if data.platforms:
        rows = [
            {
                "game_id": game_id,
                "platform": p.platform,
                "platform_name": p.name,
                "metascore": p.metascore,
                "userscore": p.userscore,
            }
            for p in data.platforms
        ]
        pstmt = insert(GamePlatform).values(rows)
        await session.execute(
            pstmt.on_conflict_do_update(
                constraint="uq_game_platforms_game_id_platform",
                set_={
                    "platform_name": pstmt.excluded.platform_name,
                    "metascore": pstmt.excluded.metascore,
                    "userscore": pstmt.excluded.userscore,
                },
            )
        )
    await session.execute(
        delete(GamePlatform).where(
            GamePlatform.game_id == game_id,
            GamePlatform.platform.not_in([p.platform for p in data.platforms]),
        )
    )
    return game_id, bool(inserted)


async def replace_reviews(
    session: AsyncSession, game_id: int, reviews: dict[ReviewSource, list[Review]]
) -> None:
    """Набор отзывов по игре и источнику заменяется целиком"""
    for source, items in reviews.items():
        await session.execute(
            delete(GameReview).where(GameReview.game_id == game_id, GameReview.source == source)
        )
        session.add_all(
            GameReview(
                game_id=game_id,
                source=source,
                position=position,
                text=review.text,
                score=review.score,
                author=(review.author or "")[:300] or None,
            )
            for position, review in enumerate(items)
        )
    await session.flush()


async def load_reviews(session: AsyncSession, game_id: int, source: str) -> list[Review]:
    rows = await session.execute(
        select(GameReview.text, GameReview.score, GameReview.author)
        .where(GameReview.game_id == game_id, GameReview.source == source)
        .order_by(GameReview.position)
    )
    return [Review(text, score, author) for text, score, author in rows]


async def save_embedding(session: AsyncSession, game_id: int, vector: list[float]) -> None:
    await session.execute(update(Game).where(Game.id == game_id).values(embedding=vector))
