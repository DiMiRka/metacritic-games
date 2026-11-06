from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.catalog.models import Game

PLATFORM_BONUS = 0.03
MAX_PLATFORM_BONUS = 3
CANDIDATES_FACTOR = 5
DEVELOPER_WEIGHT = 1.5


def _platforms(game: Game) -> set[str]:
    return {p.platform for p in game.platforms}


async def _by_embedding(session: AsyncSession, game: Game, limit: int) -> list[Game]:
    distance = Game.embedding.cosine_distance(game.embedding)
    rows = (
        await session.execute(
            select(Game, distance.label("distance"))
            .options(selectinload(Game.platforms))
            # Векторы разных моделей несравнимы, даже при одинаковой размерности
            .where(
                Game.id != game.id,
                Game.embedding.is_not(None),
                Game.embedding_model.is_not_distinct_from(game.embedding_model),
            )
            .order_by(distance)
            .limit(limit * CANDIDATES_FACTOR)
        )
    ).all()
    own = _platforms(game)

    def score(row) -> float:
        shared = len(own & _platforms(row.Game))
        return (1 - row.distance) + PLATFORM_BONUS * min(shared, MAX_PLATFORM_BONUS)

    return [row.Game for row in sorted(rows, key=score, reverse=True)[:limit]]


async def _by_metadata(
    session: AsyncSession, game: Game, limit: int, exclude: set[int]
) -> list[Game]:
    conditions = []
    if game.genres:
        conditions.append(Game.genres.overlap(game.genres))
    if game.developer:
        conditions.append(Game.developer == game.developer)
    if not conditions:
        return []

    candidates = (
        await session.scalars(
            select(Game)
            .options(selectinload(Game.platforms))
            .where(Game.id.not_in(exclude | {game.id}), or_(*conditions))
            .order_by(Game.release_date.desc().nulls_last(), Game.id)
            .limit(limit * CANDIDATES_FACTOR * 4)
        )
    ).all()
    genres, own = set(game.genres), _platforms(game)

    def score(other: Game) -> float:
        return (
            len(genres & set(other.genres))
            + DEVELOPER_WEIGHT * (other.developer == game.developer)
            + 0.5 * min(len(own & _platforms(other)), MAX_PLATFORM_BONUS)
        )

    return sorted(candidates, key=score, reverse=True)[:limit]


async def similar_games(session: AsyncSession, game: Game, limit: int = 6) -> list[Game]:
    """Ближайшие по эмбеддингу с бонусом за общие платформы, добор по жанрам и разработчику"""
    if "platforms" not in game.__dict__:
        await session.refresh(game, ["platforms"])

    found = await _by_embedding(session, game, limit) if game.embedding is not None else []
    if len(found) < limit:
        found += await _by_metadata(session, game, limit - len(found), {g.id for g in found})
    return found
