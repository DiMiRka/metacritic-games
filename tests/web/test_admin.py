import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

import asyncpg
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update

from app.auth.models import User
from app.core.clock import now_tz
from app.core.db import asyncpg_dsn
from app.crawler.lock import CRAWL_LOCK_KEY
from app.crawler.models import Run, RunItem, WorkerHeartbeat
from app.crawler.stages import enqueue_collect
from app.jobs.models import Job
from app.jobs.queue import CHANNEL, enqueue
from app.web.routes.admin import monitor_events, sse_event
from tests.conftest import app


@pytest.fixture
async def client(make_client) -> AsyncIterator[AsyncClient]:
    async with make_client("admin") as http:
        yield http


@contextlib.asynccontextmanager
async def crawl_lock(db_engine) -> AsyncIterator[None]:
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": CRAWL_LOCK_KEY})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CRAWL_LOCK_KEY})


async def add_heartbeat(session_factory, status="idle", age_seconds=0, worker="w1") -> None:
    async with session_factory() as session, session.begin():
        session.add(
            WorkerHeartbeat(
                worker=worker,
                status=status,
                current_task="scrape_game#7",
                updated_at=now_tz() - timedelta(seconds=age_seconds),
            )
        )


async def add_run(session_factory, minutes_ago: int) -> None:
    started = now_tz() - timedelta(minutes=minutes_ago)
    async with session_factory() as session, session.begin():
        session.add(
            Run(trigger="schedule", status="success", started_at=started, finished_at=started)
        )


async def dead_job(session_factory) -> int:
    async with session_factory() as session, session.begin():
        job_id = await enqueue(session, "scrape_game", {"slug": "broken"})
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status="dead", attempts=5, last_error="ScrapeFormatError: формат")
        )
    return job_id


# POST /api/runs


async def test_run_rejected_without_live_worker(client, session_factory) -> None:
    assert (await client.post("/api/v1/runs")).status_code == 503
    await add_heartbeat(session_factory, age_seconds=600)
    assert (await client.post("/api/v1/runs")).status_code == 503


async def test_run_conflict_when_collect_queued(client, session_factory) -> None:
    await add_heartbeat(session_factory)
    async with session_factory() as session, session.begin():
        await enqueue_collect(session, "schedule")
    response = await client.post("/api/v1/runs")
    assert response.status_code == 409
    assert response.json()["detail"] == "Проход уже идёт"


async def test_run_conflict_when_lock_held(client, db_engine, session_factory) -> None:
    await add_heartbeat(session_factory)
    async with crawl_lock(db_engine):
        assert (await client.post("/api/v1/runs")).status_code == 409


async def test_run_rate_limited_after_recent_run(client, session_factory) -> None:
    await add_heartbeat(session_factory)
    await add_run(session_factory, minutes_ago=4)

    response = await client.post("/api/v1/runs")

    assert response.status_code == 429
    assert 5 * 60 < int(response.headers["Retry-After"]) <= 6 * 60


async def test_run_accepted_writes_collect_job_and_notifies(
    client, session_factory, db_engine
) -> None:
    await add_heartbeat(session_factory)
    await add_run(session_factory, minutes_ago=11)
    received: list[str] = []
    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    conn = await asyncpg.connect(dsn)
    try:
        await conn.add_listener(CHANNEL, lambda *args: received.append(args[-1]))
        response = await client.post("/api/v1/runs")
        second = await client.post("/api/v1/runs")
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
    finally:
        await conn.close()

    assert response.status_code == 202
    assert second.status_code == 409
    async with session_factory() as session:
        job = await session.get(Job, response.json()["job_id"])
        admin_id = await session.scalar(select(User.id).where(User.role == "admin"))
    assert (job.kind, job.status, job.priority, job.requested_by) == (
        "collect",
        "queued",
        100,
        admin_id,
    )
    assert received == ["collect"]


# POST /api/jobs/{id}/retry


async def test_retry_dead_job(client, session_factory) -> None:
    job_id = await dead_job(session_factory)

    response = await client.post(f"/api/v1/jobs/{job_id}/retry")
    again = await client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert again.status_code == 404
    async with session_factory() as session:
        job = await session.get(Job, job_id)
    assert (job.status, job.attempts) == ("queued", 0)


async def test_retry_requires_admin_and_csrf(make_client, session_factory) -> None:
    job_id = await dead_job(session_factory)
    async with make_client("viewer") as viewer:
        assert (await viewer.post(f"/api/v1/jobs/{job_id}/retry")).status_code == 403
    async with make_client("admin") as admin:
        admin.headers.pop("X-CSRF-Token")
        assert (await admin.post(f"/api/v1/jobs/{job_id}/retry")).status_code == 403


# Страница и поток


async def test_monitor_page(client, session_factory) -> None:
    await add_heartbeat(session_factory, status="running")
    job_id = await dead_job(session_factory)
    async with session_factory() as session, session.begin():
        run = Run(trigger="manual", status="running")
        session.add(run)
        await session.flush()
        session.add(
            RunItem(
                run_id=run.id,
                slug="elden-ring",
                stage="summarize_critic",
                status="failed",
                error="RateLimitError: квота",
            )
        )
        await enqueue_collect(session, "manual")

    page = await client.get("/monitor")

    assert page.status_code == 200
    assert "<div data-running hidden>" in page.text
    assert "scrape_game#7" in page.text
    assert f'data-retry="{job_id}"' in page.text
    assert "ScrapeFormatError: формат" in page.text
    assert "RateLimitError: квота" in page.text
    assert "резюме критиков" in page.text


async def test_monitor_page_empty_database(client) -> None:
    page = await client.get("/monitor")
    assert page.status_code == 200
    assert "Проходов ещё не было" in page.text
    assert "data-running" not in page.text.split('<div id="monitor"')[1]


def test_sse_event_prefixes_every_line() -> None:
    assert sse_event("update", "a\nb") == "event: update\ndata: a\ndata: b\n\n"
    assert sse_event("update", "") == "event: update\ndata: \n\n"


async def test_monitor_events_stream(session_factory) -> None:
    await add_heartbeat(session_factory)
    events = [
        e async for e in monitor_events(session_factory, app.state.templates, interval=0, limit=2)
    ]

    assert len(events) == 2
    assert events[0].startswith("event: update\n")
    body = events[0].split("\n")[1:-2]
    assert all(line.startswith("data: ") for line in body)
    assert any("w1" in line for line in body)
