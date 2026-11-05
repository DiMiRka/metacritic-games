import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import asyncpg
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.auth.models import User
from app.catalog.models import Game
from app.core.config import settings
from app.crawler.lock import CRAWL_LOCK_KEY
from app.crawler.models import Run, WorkerHeartbeat
from app.crawler.requests import CHANNEL, asyncpg_dsn
from app.monitoring.snapshot import snapshot
from app.web.routes.admin import monitor_events, sse_event


@pytest.fixture
async def client(make_client) -> AsyncIterator[AsyncClient]:
    async with make_client("admin") as http:
        yield http


@contextlib.asynccontextmanager
async def crawl_lock(db_engine) -> AsyncIterator[None]:
    """Держит блокировку прохода, как это делает воркер"""
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": CRAWL_LOCK_KEY})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CRAWL_LOCK_KEY})


@pytest.fixture
def dsn(db_engine) -> str:
    return asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))


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


async def test_run_rejected_without_worker(client) -> None:
    response = await client.post("/api/runs")
    assert response.status_code == 503


async def test_run_rejected_when_worker_stale(client, session_factory) -> None:
    await add_heartbeat(session_factory, age_seconds=600)
    assert (await client.post("/api/runs")).status_code == 503


async def test_run_conflict_when_crawl_running(client, db_engine, session_factory) -> None:
    await add_heartbeat(session_factory, status="running")
    async with crawl_lock(db_engine):
        response = await client.post("/api/runs")
    assert response.status_code == 409
    assert response.json()["detail"] == "Проход уже идёт"


async def add_run(session_factory, minutes_ago: int) -> None:
    started = datetime.now(settings.tz) - timedelta(minutes=minutes_ago)
    async with session_factory() as session, session.begin():
        session.add(
            Run(trigger="schedule", status="success", started_at=started, finished_at=started)
        )


async def test_run_rate_limited_after_recent_run(client, session_factory) -> None:
    await add_heartbeat(session_factory)
    await add_run(session_factory, minutes_ago=4)

    response = await client.post("/api/runs")

    assert response.status_code == 429
    assert 5 * 60 < int(response.headers["Retry-After"]) <= 6 * 60
    assert "через 6 мин" in response.json()["detail"]


async def test_run_allowed_after_cooldown(client, session_factory, monkeypatch) -> None:
    await add_heartbeat(session_factory)
    await add_run(session_factory, minutes_ago=11)
    assert (await client.post("/api/runs")).status_code == 202

    monkeypatch.setattr(settings, "manual_run_cooldown_minutes", 30)
    assert (await client.post("/api/runs")).status_code == 429


async def test_run_accepted_sends_notify(client, session_factory, dsn) -> None:
    await add_heartbeat(session_factory)
    payloads: list[str] = []
    received = asyncio.Event()

    def on_notify(_conn, _pid, _channel, payload: str) -> None:
        payloads.append(payload)
        received.set()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.add_listener(CHANNEL, on_notify)
        response = await client.post("/api/runs")
        await asyncio.wait_for(received.wait(), 5)
    finally:
        await conn.close()
    async with session_factory() as db:
        admin_id = await db.scalar(select(User.id).where(User.role == "admin"))
    assert response.status_code == 202
    assert payloads == [str(admin_id)]


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


async def test_monitor_page_and_api(client, db_engine, session_factory) -> None:
    await seed_runs(session_factory)
    await add_heartbeat(session_factory, status="running")

    async with crawl_lock(db_engine):
        page = await client.get("/monitor")
        api = (await client.get("/api/monitor")).json()

    assert page.status_code == 200
    assert "<div data-running>" in page.text
    assert "3/20: elden-ring" in page.text
    assert api["current_run"]["processed"] == 3


async def test_running_record_without_lock_is_stale(client, session_factory) -> None:
    await seed_runs(session_factory)

    async with session_factory() as session:
        data = await snapshot(session)
    page = await client.get("/monitor")

    assert data.current_run is None
    assert [r.status for r in data.runs] == ["stale", "success"]
    assert data.runs[0].duration_seconds is None
    assert "<div data-running>" not in page.text
    assert "прерван" in page.text


async def test_monitor_page_empty_database(client) -> None:
    page = await client.get("/monitor")
    assert page.status_code == 200
    assert "Проходов ещё не было" in page.text
    assert "<div data-running>" not in page.text


def test_sse_event_prefixes_every_line() -> None:
    assert sse_event("update", "a\nb") == "event: update\ndata: a\ndata: b\n\n"
    assert sse_event("update", "") == "event: update\ndata: \n\n"


async def test_monitor_events_stream(session_factory) -> None:
    await add_heartbeat(session_factory)
    events = [e async for e in monitor_events(session_factory, interval=0, limit=2)]

    assert len(events) == 2
    assert events[0].startswith("event: update\n")
    body = events[0].split("\n")[1:-2]
    assert all(line.startswith("data: ") for line in body)
    assert any("crawler" in line for line in body)
