import pytest
from sqlalchemy import select

from app.catalog.models import EMBEDDING_DIM, Game, GamePlatform
from app.catalog.similar import (
    CLOSE_DESCRIPTION,
    VERY_CLOSE_DESCRIPTION,
    Closeness,
    SameDeveloper,
    SharedGenres,
    SharedPlatforms,
    SimilarDescription,
    Source,
    explain,
    similar_games,
)


def unit(*hot: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for i in hot:
        vector[i] = 1.0
    return vector


async def add_game(
    session, slug, embedding=None, genres=(), developer=None, platforms=(), model="m1"
):
    game = Game(
        slug=slug,
        title=slug,
        embedding=embedding,
        embedding_model=model if embedding is not None else None,
        genres=list(genres),
        developer=developer,
        platforms=[GamePlatform(platform=p, platform_name=p) for p in platforms],
    )
    session.add(game)
    return game


async def test_similar_by_embedding_with_platform_bonus(session_factory) -> None:
    async with session_factory() as session, session.begin():
        base = await add_game(session, "base", unit(0), platforms=["pc"])
        await add_game(session, "near", unit(0, 1), platforms=["ps5"])
        await add_game(session, "near-pc", unit(0, 1), platforms=["pc"])
        await add_game(session, "far", unit(5), platforms=["pc"])
        await add_game(session, "other-model", unit(0), platforms=["pc"], model="m2")

    async with session_factory() as session:
        base = await session.scalar(select(Game).where(Game.slug == "base"))
        found = await similar_games(session, base, limit=3)

    assert [s.game.slug for s in found] == ["near-pc", "near", "far"]
    assert {s.source for s in found} == {Source.EMBEDDING}
    # unit(0) и unit(0, 1): близость 0.71, ниже порога, причины «близкое описание» нет
    assert found[0].similarity == pytest.approx(0.7071, abs=1e-3)
    # Общая платформа одна, но других причин нет: строка будет нейтральной
    assert found[0].reasons == ()


async def test_similar_fallback_by_genres_and_developer(session_factory) -> None:
    async with session_factory() as session, session.begin():
        await add_game(session, "base", genres=["RPG", "Action"], developer="Studio")
        await add_game(session, "same-dev", genres=["Puzzle"], developer="Studio")
        await add_game(session, "two-genres", genres=["RPG", "Action"])
        await add_game(session, "one-genre", genres=["RPG"])
        await add_game(session, "unrelated", genres=["Sports"])

    async with session_factory() as session:
        base = await session.scalar(select(Game).where(Game.slug == "base"))
        found = await similar_games(session, base, limit=5)

    assert [s.game.slug for s in found] == ["two-genres", "same-dev", "one-genre"]
    reasons = {s.game.slug: s.reasons for s in found}
    assert reasons["two-genres"] == (SharedGenres(("RPG", "Action")),)
    assert reasons["same-dev"] == (SameDeveloper(("Studio",)),)
    assert reasons["one-genre"] == (SharedGenres(("RPG",)),)
    assert {s.source for s in found} == {Source.METADATA}
    assert all(s.similarity is None for s in found)


def game(genres=(), developer=None, platforms=()):
    return Game(
        slug="x",
        title="x",
        genres=list(genres),
        developer=developer,
        platforms=[GamePlatform(platform=p.lower(), platform_name=p) for p in platforms],
    )


def test_developers_compared_as_sets_ignoring_case() -> None:
    base = game(developer="FromSoftware, Bandai Namco")
    found = explain(base, game(developer="fromsoftware"), Source.METADATA)
    assert found.reasons == (SameDeveloper(("FromSoftware",)),)
    assert explain(base, game(developer="Bandai"), Source.METADATA).reasons == ()


def test_genres_in_current_game_order() -> None:
    base = game(genres=["Action RPG", "Souls-like", "Open World"])
    other = game(genres=["Open World", "Action RPG"])
    assert explain(base, other, Source.METADATA).reasons == (
        SharedGenres(("Action RPG", "Open World")),
    )


@pytest.mark.parametrize(
    ("similarity", "level"),
    [
        (VERY_CLOSE_DESCRIPTION, Closeness.VERY_CLOSE),
        (CLOSE_DESCRIPTION, Closeness.CLOSE),
        (CLOSE_DESCRIPTION - 0.01, None),
        # API показывает два знака, уровень считается по тому же числу
        (VERY_CLOSE_DESCRIPTION - 0.0005, Closeness.VERY_CLOSE),
        (CLOSE_DESCRIPTION - 0.006, None),
    ],
)
def test_description_level_by_calibrated_thresholds(similarity, level) -> None:
    reasons = explain(game(), game(), Source.EMBEDDING, similarity).reasons
    assert reasons == ((SimilarDescription(similarity, level),) if level else ())


def test_platforms_only_as_addition_and_when_few() -> None:
    base = game(genres=["RPG"], platforms=["PC", "PS5", "Xbox", "Switch"])
    two = game(genres=["RPG"], platforms=["PS5", "PC"])
    many = game(genres=["RPG"], platforms=["PC", "PS5", "Xbox"])
    alone = game(platforms=["PC"])

    assert explain(base, two, Source.METADATA).reasons[-1] == SharedPlatforms(("PC", "PS5"))
    assert explain(base, many, Source.METADATA).reasons == (SharedGenres(("RPG",)),)
    # Одни платформы — не причина: игру подобрал эмбеддинг по описанию
    assert explain(base, alone, Source.EMBEDDING, 0.6).reasons == ()
