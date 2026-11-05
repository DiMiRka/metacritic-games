"""Обход целиком через очередь: collect → scrape_game → summarize и embed"""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.catalog.models import Game, GamePlatform, GameReview, ReviewSummary
from app.core.clock import day_bounds, now_tz
from app.core.config import settings
from app.core.db import asyncpg_dsn
from app.crawler import stages as stages_module
from app.crawler.lock import CRAWL_LOCK_KEY
from app.crawler.models import CrawlState, Run, RunItem
from app.crawler.runs import done_today, run_stats
from app.crawler.stages import CrawlStages, enqueue_collect
from app.jobs.models import Job
from app.worker.main import Worker, wait_for_run
from tests.conftest import FakeClient, FakeEmbedder, FakeSummarizer, create_user

GAMES = {"elden-ring": "composer_elden-ring.json", "starshapes": "composer_starshapes.json"}
REQUEST = httpx.Request("GET", "https://backend.metacritic.com/x")


@pytest.fixture(autouse=True)
def fast_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings, "job_poll_seconds", 0.05)
    monkeypatch.setattr(settings, "job_retry_base_seconds", 0.01)
    monkeypatch.setattr(settings, "job_retry_max_seconds", 0.05)


class FlakyClient(FakeClient):
    """Первый запрос карточки каждой игры отвечает 503"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed: set[str] = set()

    async def game(self, slug: str):
        if slug not in self.failed:
            self.failed.add(slug)
            response = httpx.Response(503, request=REQUEST)
            raise httpx.HTTPStatusError("503", request=REQUEST, response=response)
        return await super().game(slug)


class FailingEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("модель не загрузилась")


async def run_pipeline(
    db_engine,
    session_factory,
    client,
    summarizer=None,
    embedder=None,
    trigger: str = "schedule",
    requested_by: int | None = None,
) -> int | None:
    stages = CrawlStages(
        client=client,
        engine=db_engine,
        session_factory=session_factory,
        summarizer=summarizer,
        embedder=embedder,
        batch_size=20,
    )
    async with session_factory() as session, session.begin():
        collect_id = await enqueue_collect(session, trigger, requested_by)
    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    worker = Worker(stages, session_factory, "test-worker", dsn=dsn)
    task = asyncio.create_task(worker.run(schedule=False))
    try:
        return await asyncio.wait_for(wait_for_run(session_factory, collect_id), 30)
    finally:
        worker.stop.set()
        await task


async def items(session_factory, run_id) -> list[tuple[str, str, str]]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(RunItem).where(RunItem.run_id == run_id).order_by(RunItem.id)
        )
        return [(i.slug, i.stage, i.status) for i in rows]


async def get_run(session_factory, run_id) -> Run:
    async with session_factory() as session:
        return await session.get(Run, run_id)


async def test_full_run(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring", "starshapes", "no-such-game"], games=GAMES)
    run_id = await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(), FakeEmbedder()
    )

    run = await get_run(session_factory, run_id)
    assert run.status == "partial"
    assert run.finished_at is not None
    recorded = await items(session_factory, run_id)
    assert ("no-such-game", "scrape", "failed") in recorded
    assert {(s, st) for s, st, status in recorded if status in ("created", "updated")} == {
        ("elden-ring", "scrape"),
        ("starshapes", "scrape"),
        ("elden-ring", "summarize_critic"),
        ("elden-ring", "summarize_user"),
        ("elden-ring", "embed"),
        ("starshapes", "embed"),
    }
    async with session_factory() as session:
        stats = (await run_stats(session, [run_id]))[run_id]
        reviews = await session.scalar(select(func.count()).select_from(GameReview))
        summaries = await session.scalar(select(func.count()).select_from(ReviewSummary))
        embedded = await session.scalar(select(func.count(Game.embedding)))
        today, _ = day_bounds(now_tz())
        state = await session.get(CrawlState, today)
        jobs = dict(
            (await session.execute(select(Job.kind, func.count()).group_by(Job.kind))).all()
        )
    assert (stats.processed, stats.created, stats.updated, stats.failed) == (3, 2, 0, 1)
    assert reviews == 40
    assert summaries == 2
    assert embedded == 2
    assert state.phase == "browse"
    assert jobs == {"collect": 1, "scrape_game": 3, "summarize": 2, "embed": 2}


async def test_upsert_is_idempotent_across_days(db_engine, session_factory, monkeypatch) -> None:
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    summarizer = FakeSummarizer()
    await run_pipeline(db_engine, session_factory, client, summarizer, FakeEmbedder())

    tomorrow = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: tomorrow)
    run_id = await run_pipeline(db_engine, session_factory, client, summarizer, FakeEmbedder())

    async with session_factory() as session:
        games = await session.scalar(select(func.count()).select_from(Game))
        platforms = await session.scalar(select(func.count()).select_from(GamePlatform))
    assert (games, platforms) == (1, 5)
    # Отзывы те же, резюме не пересчитывалось, эмбеддинг считается заново
    assert len(summarizer.calls) == 2
    assert await items(session_factory, run_id) == [
        ("elden-ring", "scrape", "updated"),
        ("elden-ring", "embed", "updated"),
    ]


async def test_second_run_same_day_takes_nothing(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client)
    async with session_factory() as session, session.begin():
        await session.execute(update(CrawlState).values(phase="new_releases", next_page=1))
    run_id = await run_pipeline(db_engine, session_factory, client)

    run = await get_run(session_factory, run_id)
    assert run.status == "success"
    assert await items(session_factory, run_id) == []


async def test_transient_error_is_retried(db_engine, session_factory) -> None:
    client = FlakyClient(new_releases=["elden-ring"], games=GAMES)
    run_id = await run_pipeline(db_engine, session_factory, client)

    assert await items(session_factory, run_id) == [
        ("elden-ring", "scrape", "retrying"),
        ("elden-ring", "scrape", "created"),
    ]
    assert (await get_run(session_factory, run_id)).status == "success"


async def test_llm_failure_does_not_block_game_and_embedding(
    db_engine, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "job_max_attempts", 2)
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    run_id = await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(fail=True), FakeEmbedder()
    )

    recorded = await items(session_factory, run_id)
    assert recorded[0] == ("elden-ring", "scrape", "created")
    assert ("elden-ring", "embed", "updated") in recorded
    assert recorded.count(("elden-ring", "summarize_critic", "retrying")) == 1
    assert ("elden-ring", "summarize_critic", "failed") in recorded
    assert (await get_run(session_factory, run_id)).status == "partial"
    async with session_factory() as session:
        stats = (await run_stats(session, [run_id]))[run_id]
    assert (stats.processed, stats.created) == (1, 1)


async def test_embedding_failure_keeps_old_vector(db_engine, session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, embedder=FakeEmbedder())

    tomorrow = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: tomorrow)
    run_id = await run_pipeline(db_engine, session_factory, client, embedder=FailingEmbedder())

    assert ("elden-ring", "embed", "failed") in await items(session_factory, run_id)
    async with session_factory() as session:
        game = await session.scalar(select(Game))
    assert game.embedding is not None


async def test_collect_skipped_when_lock_held(db_engine, session_factory) -> None:
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": CRAWL_LOCK_KEY})
        try:
            run_id = await run_pipeline(db_engine, session_factory, FakeClient())
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CRAWL_LOCK_KEY})
    assert run_id is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0


async def test_run_records_requester(db_engine, session_factory) -> None:
    admin_id = await create_user(session_factory, "boss")
    manual = await run_pipeline(
        db_engine, session_factory, FakeClient(), trigger="manual", requested_by=admin_id
    )
    run = await get_run(session_factory, manual)
    assert (run.trigger, run.requested_by) == ("manual", admin_id)


async def test_done_today_by_job_status(session_factory) -> None:
    from app.jobs.queue import enqueue

    async with session_factory() as session, session.begin():
        for slug, status in [("a", "queued"), ("b", "done"), ("c", "dead"), ("d", "cancelled")]:
            job_id = await enqueue(session, "scrape_game", {"slug": slug})
            await session.execute(update(Job).where(Job.id == job_id).values(status=status))
        await enqueue(session, "embed", {"slug": "e"})

    _, day_start = day_bounds(now_tz())
    async with session_factory() as session:
        today = await done_today(session, list("abcdef"), day_start)
        tomorrow = await done_today(session, list("abcdef"), day_start + timedelta(days=1))
    assert today == {"a", "b", "c"}
    assert tomorrow == set()
