"""Похожие игры и причины сходства.

Порядок выдачи задают баллы ниже, причины только объясняют уже выбранное и считаются
по загруженным объектам и расстоянию из запроса, без новых запросов в базу.
Слова для страницы собирает веб (app/web/templating.py), каталог отдаёт данные
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.catalog.models import Game

PLATFORM_BONUS = 0.03
MAX_PLATFORM_BONUS = 3
CANDIDATES_FACTOR = 5
DEVELOPER_WEIGHT = 1.5

# Пороги близости описаний для bge-small-en-v1.5, где косинусная близость сжата.
# Калибровка на dev-базе 9 ноября 2026: 436 игр, 3000 случайных пар и 2616 пар
# «игра и её 6 похожих». У случайных пар p50 = 0.59, p99 = 0.705, p99.5 = 0.721,
# p99.9 = 0.764, у похожих p50 = 0.70, p90 = 0.75.
# close: выше 0.72 лежит 0.6% случайных пар и 28% выдачи похожих.
# very_close: выше 0.78 лежит 0.1% случайных пар и 2.4% выдачи, общий жанр у половины таких пар.
# При смене модели эмбеддингов пороги нужно пересчитать
CLOSE_DESCRIPTION = 0.72
VERY_CLOSE_DESCRIPTION = 0.78

# Общие платформы — дополнение к другой причине и только когда их мало:
# «есть на PC и PS5» что-то говорит, «на всех пяти платформах» — шум
MAX_SHARED_PLATFORMS = 2

DEVELOPER_SEPARATOR = ", "


class Source(StrEnum):
    EMBEDDING = "embedding"
    METADATA = "metadata"


class Closeness(StrEnum):
    VERY_CLOSE = "very_close"
    CLOSE = "close"


@dataclass(frozen=True)
class SharedGenres:
    kind: ClassVar[str] = "genres"
    names: tuple[str, ...]


@dataclass(frozen=True)
class SameDeveloper:
    kind: ClassVar[str] = "developer"
    names: tuple[str, ...]


@dataclass(frozen=True)
class SimilarDescription:
    kind: ClassVar[str] = "description"
    similarity: float
    level: Closeness


@dataclass(frozen=True)
class SharedPlatforms:
    kind: ClassVar[str] = "platforms"
    names: tuple[str, ...]


Reason = SharedGenres | SameDeveloper | SimilarDescription | SharedPlatforms


@dataclass(frozen=True)
class SimilarGame:
    game: Game
    source: Source
    # Причины по важности: разработчик, жанры, описание, платформы.
    # Пусто, когда игру подобрал эмбеддинг, а близость ниже порога
    reasons: tuple[Reason, ...]
    # 1 − косинусное расстояние, только у найденных по эмбеддингу
    similarity: float | None = None


def _platforms(game: Game) -> set[str]:
    return {p.platform for p in game.platforms}


def closeness(similarity: float) -> Closeness | None:
    # Сравнивается округлённое значение, как его отдаёт API: иначе 0.7795 показалось бы
    # как «0.78, close» при пороге very_close 0.78
    similarity = round(similarity, 2)
    if similarity >= VERY_CLOSE_DESCRIPTION:
        return Closeness.VERY_CLOSE
    if similarity >= CLOSE_DESCRIPTION:
        return Closeness.CLOSE
    return None


def _studios(developer: str | None) -> list[str]:
    return [s.strip() for s in (developer or "").split(DEVELOPER_SEPARATOR) if s.strip()]


def explain(
    game: Game, other: Game, source: Source, similarity: float | None = None
) -> SimilarGame:
    """Причины, по которым other похожа на game. Названия берутся у текущей игры и в её порядке"""
    reasons: list[Reason] = []

    other_studios = {s.casefold() for s in _studios(other.developer)}
    if studios := tuple(s for s in _studios(game.developer) if s.casefold() in other_studios):
        reasons.append(SameDeveloper(studios))

    other_genres = {g.casefold() for g in other.genres or ()}
    if genres := tuple(dict.fromkeys(g for g in game.genres or () if g.casefold() in other_genres)):
        reasons.append(SharedGenres(genres))

    if similarity is not None and (level := closeness(similarity)):
        reasons.append(SimilarDescription(similarity, level))

    other_platforms = _platforms(other)
    shared = sorted({p.platform_name for p in game.platforms if p.platform in other_platforms})
    if reasons and 0 < len(shared) <= MAX_SHARED_PLATFORMS:
        reasons.append(SharedPlatforms(tuple(shared)))

    return SimilarGame(other, source, tuple(reasons), similarity)


async def _by_embedding(session: AsyncSession, game: Game, limit: int) -> list[tuple[Game, float]]:
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

    return [(row.Game, 1 - row.distance) for row in sorted(rows, key=score, reverse=True)[:limit]]


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


async def similar_games(session: AsyncSession, game: Game, limit: int = 6) -> list[SimilarGame]:
    """Ближайшие по эмбеддингу с бонусом за общие платформы, добор по жанрам и разработчику"""
    if "platforms" not in game.__dict__:
        await session.refresh(game, ["platforms"])

    found = [
        explain(game, other, Source.EMBEDDING, similarity)
        for other, similarity in (
            await _by_embedding(session, game, limit) if game.embedding is not None else []
        )
    ]
    if len(found) < limit:
        exclude = {s.game.id for s in found}
        found += [
            explain(game, other, Source.METADATA)
            for other in await _by_metadata(session, game, limit - len(found), exclude)
        ]
    return found
