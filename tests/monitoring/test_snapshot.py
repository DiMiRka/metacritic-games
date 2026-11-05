import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.catalog.models import Game
from app.core.config import settings
from app.crawler.lock import CRAWL_LOCK_KEY
from app.crawler.models import Run, WorkerHeartbeat
from app.monitoring.snapshot import snapshot
from tests.conftest import create_user


@contextlib.asynccontextmanager
async def crawl_lock(db_engine) -> AsyncIterator[None]:
    """Держит блокировку прохода, как это делает воркер"""
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": CRAWL_LOCK_KEY})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CRAWL_LOCK_KEY})


async def add_heartbeat(session_factory, status="idle", age_seconds=0) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            WorkerHeartbeat(
                worker="crawler",
                status=status,
                current_task="3/20: elden-ring",
                updated_at=datetime.now(settings.tz) - timedelta(seconds=age_seconds),
            )
        )


async def test_snapshot_shows_who_started_run(session_factory) -> None:
    admin_id = await create_user(session_factory, "boss")
    async with session_factory() as session, session.begin():
        session.add(Run(trigger="manual", status="success", requested_by=admin_id))
        session.add(Run(trigger="schedule", status="success"))

    async with session_factory() as session:
        data = await snapshot(session)

    assert [r.requested_by for r in data.runs] == [None, "boss"]


async def seed_runs(session_factory) -> None:
    now = datetime.now(settings.tz)
    async with session_factory() as session, session.begin():
        session.add(Game(slug="done", title="Done", genres=[], last_processed_at=now))
        session.add(Game(slug="old", title="Old", genres=[]))
        session.add(
            Run(
                trigger="schedule",
                status="success",
                started_at=now - timedelta(minutes=30),
                finished_at=now - timedelta(minutes=28),
                processed=20,
                created=18,
                updated=0,
                errors=[{"slug": "a", "error": "first"}, {"slug": "b", "error": "second"}],
            )
        )
        session.add(
            Run(
                trigger="manual",
                status="running",
                started_at=now - timedelta(seconds=90),
                processed=3,
                created=1,
                updated=2,
                errors=[],
            )
        )


async def test_snapshot(db_engine, session_factory) -> None:
    await seed_runs(session_factory)
    await add_heartbeat(session_factory, status="running")

    async with crawl_lock(db_engine), session_factory() as session:
        data = await snapshot(session)

    assert data.workers[0].alive is True
    assert data.current_run.trigger == "manual"
    assert 80 < data.current_run.duration_seconds < 200
    assert [r.status for r in data.runs] == ["running", "success"]
    assert data.runs[1].duration_seconds == pytest.approx(120, abs=1)
    assert [e.slug for e in data.errors] == ["b", "a"]
    c = data.counters
    assert (c.games_total, c.processed_today, c.runs_today, c.errors_today) == (2, 1, 2, 2)
