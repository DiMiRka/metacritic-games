from datetime import datetime

import pytest
from sqlalchemy import select, update

from app.catalog.models import EMBEDDING_DIM, Game, ReviewSummary
from app.core.config import settings
from app.crawler.processor import EnrichmentError, process_game
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
    with pytest.raises(EnrichmentError):
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


class FailingEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("модель не загрузилась")


async def test_embedding_failure_saves_game_without_vector(session_factory) -> None:
    with pytest.raises(EnrichmentError, match="embedding: RuntimeError"):
        await process(session_factory, "elden-ring", embedder=FailingEmbedder())

    async with session_factory() as session:
        game = await session.scalar(select(Game))
    assert game.slug == "elden-ring"
    assert game.embedding is None


async def test_embedding_failure_keeps_old_vector_and_summaries(session_factory) -> None:
    await process(session_factory, "elden-ring", embedder=FakeEmbedder())
    summarizer = FakeSummarizer()
    with pytest.raises(EnrichmentError):
        await process(session_factory, "elden-ring", summarizer, FailingEmbedder())

    async with session_factory() as session:
        game = await session.scalar(select(Game))
    assert game.embedding is not None
    assert len(summarizer.calls) == 2
