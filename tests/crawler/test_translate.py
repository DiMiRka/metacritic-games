from datetime import timedelta

from sqlalchemy import select, update

from app.catalog.models import Game
from app.core.clock import now_tz
from app.crawler import stages as stages_module
from app.enrichment.translation import save_translation, translation_key
from tests.conftest import FakeClient, FakeSummarizer
from tests.crawler.test_pipeline import GAMES, items, run_pipeline


async def game(session_factory) -> Game:
    async with session_factory() as session:
        return await session.scalar(select(Game).where(Game.slug == "elden-ring"))


def translations(summarizer: FakeSummarizer) -> int:
    return len(summarizer.translations)


async def test_description_translated_once(db_engine, session_factory, monkeypatch) -> None:
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    summarizer = FakeSummarizer()
    run_id = await run_pipeline(db_engine, session_factory, client, summarizer)

    saved = await game(session_factory)
    assert saved.description_ru == f"[ru] {saved.description}"
    assert saved.description_ru_key == translation_key(saved.description, "fake-model")
    assert ("elden-ring", "translate", "updated") in await items(session_factory, run_id)

    # Тот же текст и та же модель: перевод не повторяется
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    await run_pipeline(db_engine, session_factory, client, summarizer)
    assert translations(summarizer) == 1


async def test_new_model_retranslates(db_engine, session_factory, monkeypatch) -> None:
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer("m1"))
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    second = FakeSummarizer("m2")
    await run_pipeline(db_engine, session_factory, client, second)
    assert translations(second) == 1


async def test_translation_not_saved_if_description_changed_meanwhile(session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(Game(slug="g", title="G", genres=[], description="old text"))
    async with session_factory() as session, session.begin():
        await session.execute(update(Game).values(description="new text"))
    async with session_factory() as session, session.begin():
        game_id = await session.scalar(select(Game.id))
        assert not await save_translation(session, game_id, "old text", "[ru] old", "m")
    async with session_factory() as session:
        assert (await session.scalar(select(Game.description_ru))) is None


async def test_llm_failure_keeps_game(db_engine, session_factory, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "job_max_attempts", 1)
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(fail=True))
    saved = await game(session_factory)
    assert saved.description and saved.description_ru is None
