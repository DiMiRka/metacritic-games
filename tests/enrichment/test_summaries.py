from sqlalchemy import select

from app.catalog.models import Game, ReviewSummary
from app.enrichment.llm import PROMPT_VERSION
from app.enrichment.summaries import reviews_hash, summary_outdated, update_summary
from app.scraper.parsers import Review
from tests.conftest import FakeSummarizer

REVIEWS = [Review(f"отзыв {i}", 8, None) for i in range(3)]


async def seed(session_factory, model: str = "m1") -> int:
    async with session_factory() as session, session.begin():
        game = Game(slug="g", title="G", genres=[])
        session.add(game)
        await session.flush()
        session.add(
            ReviewSummary(
                game_id=game.id,
                source="critic",
                likes=[],
                dislikes=[],
                reviews_count=3,
                reviews_hash=reviews_hash(REVIEWS),
                model=model,
            )
        )
        return game.id


async def test_summary_outdated(session_factory) -> None:
    game_id = await seed(session_factory)
    changed = [*REVIEWS[:2], Review("новый отзыв", 5, None)]
    async with session_factory() as session:
        assert not await summary_outdated(session, game_id, "critic", REVIEWS, "m1")
        assert not await summary_outdated(session, game_id, "critic", REVIEWS[::-1], "m1")
        assert await summary_outdated(session, game_id, "critic", changed, "m1")
        assert await summary_outdated(session, game_id, "critic", REVIEWS, "m2")
        assert await summary_outdated(session, game_id, "user", REVIEWS, "m1")
        assert not await summary_outdated(session, game_id, "user", REVIEWS[:2], "m1")


async def test_summary_outdated_on_prompt_version(session_factory, monkeypatch) -> None:
    game_id = await seed(session_factory)
    monkeypatch.setattr("app.enrichment.summaries.PROMPT_VERSION", 2)
    async with session_factory() as session:
        assert await summary_outdated(session, game_id, "critic", REVIEWS, "m1")


async def test_update_summary_stores_usage_and_prompt_version(session_factory) -> None:
    game_id = await seed(session_factory, model="old")
    summarizer = FakeSummarizer()

    assert await update_summary(session_factory, summarizer, game_id, "G", "critic", REVIEWS)
    assert not await update_summary(session_factory, summarizer, game_id, "G", "critic", REVIEWS)

    async with session_factory() as session:
        row = await session.scalar(select(ReviewSummary).where(ReviewSummary.game_id == game_id))
    assert (row.model, row.prompt_version) == (summarizer.model, PROMPT_VERSION)
    assert (row.input_tokens, row.output_tokens) == (300, 50)
    assert len(summarizer.calls) == 1
