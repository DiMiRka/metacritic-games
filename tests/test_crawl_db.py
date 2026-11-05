from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.crawler.processor import process_game
from app.crawler.runner import CRAWL_LOCK_KEY, day_bounds, done_today, run_crawl
from app.models import CrawlPhase, CrawlState, Game, GamePlatform, Run, RunStatus, WorkerHeartbeat
from tests.conftest import FakeClient

GAMES = {"elden-ring": "composer_elden-ring.json", "starshapes": "composer_starshapes.json"}


async def count(session_factory: async_sessionmaker[AsyncSession], model) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_upsert_is_idempotent(session_factory) -> None:
    client = FakeClient(games=GAMES)
    now = datetime.now(settings.tz)

    assert await process_game(session_factory, client, "elden-ring", now) is True
    assert await process_game(session_factory, client, "elden-ring", now) is False

    assert await count(session_factory, Game) == 1
    assert await count(session_factory, GamePlatform) == 5
    async with session_factory() as session:
        rows = dict(
            (await session.execute(select(GamePlatform.platform, GamePlatform.userscore))).all()
        )
    assert rows["playstation-5"] == 8.4
    assert rows["pc"] == 7.6
    lead_skipped = [p for _, p in client.user_stats_calls if p == "playstation-5"]
    assert lead_skipped == []
    assert len(client.user_stats_calls) == 8


async def test_removed_platform_is_deleted(session_factory) -> None:
    now = datetime.now(settings.tz)
    await process_game(session_factory, FakeClient(games=GAMES), "elden-ring", now)

    async with session_factory() as session, session.begin():
        game = await session.scalar(select(Game))
        session.add(GamePlatform(game_id=game.id, platform="dreamcast", platform_name="Dreamcast"))
    await process_game(session_factory, FakeClient(games=GAMES), "elden-ring", now)

    assert await count(session_factory, GamePlatform) == 5


async def _run(db_engine, session_factory, client, now=None):
    return await run_crawl(
        "schedule",
        client=client,
        engine=db_engine,
        session_factory=session_factory,
        batch_size=20,
        now=now,
    )


async def test_run_processes_batch_and_records_errors(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring", "starshapes", "no-such-game"], games=GAMES)
    run_id = await _run(db_engine, session_factory, client)

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        today, _ = day_bounds(datetime.now(settings.tz))
        state = await session.get(CrawlState, today)
        heartbeat = await session.get(WorkerHeartbeat, "crawler")

    assert run.status == RunStatus.SUCCESS
    assert (run.processed, run.created, run.updated) == (3, 2, 0)
    assert [e["slug"] for e in run.errors] == ["no-such-game"]
    assert "GameNotFoundError" in run.errors[0]["error"]
    assert run.finished_at is not None
    assert (state.phase, state.next_page) == (CrawlPhase.BROWSE, 1)
    assert heartbeat.status == "idle"


async def test_done_today_includes_processed_and_failed(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring", "starshapes", "no-such-game"], games=GAMES)
    await _run(db_engine, session_factory, client)

    slugs = ["elden-ring", "starshapes", "no-such-game", "fresh-game"]
    now = datetime.now(settings.tz)
    async with session_factory() as session:
        today = await done_today(session, slugs, day_bounds(now)[1])
        tomorrow = await done_today(session, slugs, day_bounds(now + timedelta(days=1))[1])
    assert today == {"elden-ring", "starshapes", "no-such-game"}
    assert tomorrow == set()


async def test_second_run_same_day_takes_nothing_new(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring", "starshapes"], games=GAMES)
    await _run(db_engine, session_factory, client)
    async with session_factory() as session, session.begin():
        today, _ = day_bounds(datetime.now(settings.tz))
        state = await session.get(CrawlState, today)
        state.phase, state.next_page = CrawlPhase.NEW_RELEASES, 1
    run_id = await _run(db_engine, session_factory, client)

    async with session_factory() as session:
        run = await session.get(Run, run_id)
    assert run.processed == 0


async def test_new_day_starts_over(db_engine, session_factory) -> None:
    client = FakeClient(new_releases=["elden-ring", "starshapes"], games=GAMES)
    now = datetime.now(settings.tz)
    await _run(db_engine, session_factory, client, now)
    run_id = await _run(db_engine, session_factory, client, now + timedelta(days=1))

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        states = (await session.scalars(select(CrawlState).order_by(CrawlState.day))).all()
    assert (run.processed, run.created, run.updated) == (2, 0, 2)
    assert len(states) == 2


async def test_skip_when_lock_is_held(db_engine: AsyncEngine, session_factory) -> None:
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": CRAWL_LOCK_KEY})
        try:
            assert await _run(db_engine, session_factory, FakeClient()) is None
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CRAWL_LOCK_KEY})
    assert await count(session_factory, Run) == 0
