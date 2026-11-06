from sqlalchemy import select

from app.catalog.models import EMBEDDING_DIM, Game, GamePlatform
from app.catalog.similar import similar_games


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

    assert [g.slug for g in found] == ["near-pc", "near", "far"]


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

    assert [g.slug for g in found] == ["two-genres", "same-dev", "one-genre"]
