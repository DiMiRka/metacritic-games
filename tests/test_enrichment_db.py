from datetime import datetime

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.crawler.processor import process_game
from app.llm import SummaryError
from app.models import EMBEDDING_DIM, Game, GamePlatform, ReviewSummary
from app.similar import similar_games
from tests.conftest import FakeClient, FakeEmbedder, FakeSummarizer

GAMES = {"elden-ring": "composer_elden-ring.json", "starshapes": "composer_starshapes.json"}


async def process(session_factory, slug, summarizer=None, embedder=None) -> bool:
    return await process_game(
        session_factory,
        FakeClient(games=GAMES),
        slug,
        datetime.now(settings.tz),
        summarizer,
        embedder,
    )


async def summaries(session_factory) -> dict[str, ReviewSummary]:
    async with session_factory() as session:
        rows = (await session.scalars(select(ReviewSummary))).all()
    return {row.source: row for row in rows}


async def test_summaries_created_for_both_sources(session_factory) -> None:
    summarizer = FakeSummarizer()
    await process(session_factory, "elden-ring", summarizer)

    rows = await summaries(session_factory)
    assert set(rows) == {"critic", "user"}
    assert rows["critic"].likes == ["critic плюс"]
    assert rows["critic"].reviews_count == 10
    assert rows["user"].reviews_count == 30
    assert rows["user"].model == "fake-model"


async def test_summary_not_regenerated_for_same_reviews(session_factory) -> None:
    summarizer = FakeSummarizer()
    await process(session_factory, "elden-ring", summarizer)
    await process(session_factory, "elden-ring", summarizer)

    assert len(summarizer.calls) == 2


async def test_summary_regenerated_when_hash_or_model_changes(session_factory) -> None:
    await process(session_factory, "elden-ring", FakeSummarizer())
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ReviewSummary)
            .where(ReviewSummary.source == "critic")
            .values(reviews_hash="stale")
        )

    summarizer = FakeSummarizer()
    await process(session_factory, "elden-ring", summarizer)
    assert [source for _, source, _ in summarizer.calls] == ["critic"]

    other_model = FakeSummarizer(model="other-model")
    await process(session_factory, "elden-ring", other_model)
    assert len(other_model.calls) == 2


async def test_no_summary_without_reviews(session_factory) -> None:
    summarizer = FakeSummarizer()
    await process(session_factory, "starshapes", summarizer)

    assert summarizer.calls == []
    assert await summaries(session_factory) == {}


async def test_llm_failure_keeps_game_and_raises(session_factory) -> None:
    with pytest.raises(SummaryError):
        await process(session_factory, "elden-ring", FakeSummarizer(fail=True))

    async with session_factory() as session:
        game = await session.scalar(select(Game))
    assert game.slug == "elden-ring"
    assert game.last_processed_at is not None


async def test_embedding_saved_and_kept_without_embedder(session_factory) -> None:
    await process(session_factory, "elden-ring", embedder=FakeEmbedder())
    await process(session_factory, "elden-ring")

    async with session_factory() as session:
        game = await session.scalar(select(Game))
    assert game.embedding is not None
    assert len(game.embedding) == EMBEDDING_DIM


def unit(*hot: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for i in hot:
        vector[i] = 1.0
    return vector


async def add_game(session, slug, embedding=None, genres=(), developer=None, platforms=()):
    game = Game(
        slug=slug,
        title=slug,
        embedding=embedding,
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
