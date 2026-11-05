import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app.core.clock import now_tz
from app.jobs.models import Job
from app.jobs.queue import (
    claim,
    complete,
    enqueue,
    extend_lease,
    fail,
    reap_expired,
    release,
    retry_dead,
    retry_delay,
)

W1, W2 = "worker-1", "worker-2"


async def put(session_factory, kind="embed", **kwargs) -> int | None:
    async with session_factory() as s, s.begin():
        return await enqueue(s, kind, {"n": 1}, **kwargs)


async def take(session_factory, worker=W1, kinds=("embed",), limit=10, lease=60):
    async with session_factory() as s, s.begin():
        return await claim(s, worker, list(kinds), limit, lease)


async def job(session_factory, job_id) -> Job:
    async with session_factory() as s:
        return await s.get(Job, job_id)


async def test_dedupe_while_active(session_factory) -> None:
    first = await put(session_factory, dedupe_key="embed:1")
    assert await put(session_factory, dedupe_key="embed:1") is None
    assert await put(session_factory, dedupe_key="embed:2") is not None

    [claimed] = await take(session_factory, limit=1)
    assert claimed.id == first
    assert await put(session_factory, dedupe_key="embed:1") is None
    async with session_factory() as s, s.begin():
        await complete(s, first, W1)
    assert await put(session_factory, dedupe_key="embed:1") is not None


async def test_claim_order_kinds_and_run_after(session_factory) -> None:
    low = await put(session_factory, priority=0)
    high = await put(session_factory, priority=10)
    await put(session_factory, kind="summarize")
    await put(session_factory, run_after=now_tz() + timedelta(hours=1))

    claimed = await take(session_factory)

    assert [j.id for j in claimed] == sorted([low, high])
    assert {j.attempts for j in claimed} == {1}
    stored = await job(session_factory, high)
    assert (stored.status, stored.locked_by, stored.attempts) == ("running", W1, 1)
    assert stored.locked_until > now_tz()


async def test_concurrent_claims_do_not_overlap(session_factory) -> None:
    for _ in range(6):
        await put(session_factory)
    batches = await asyncio.gather(
        *(take(session_factory, worker=f"w{i}", limit=2) for i in range(4))
    )
    ids = [j.id for batch in batches for j in batch]
    assert len(ids) == len(set(ids)) == 6


async def test_only_owner_can_finish(session_factory) -> None:
    job_id = await put(session_factory)
    [claimed] = await take(session_factory)
    async with session_factory() as s, s.begin():
        assert await complete(s, job_id, W2) is False
        assert await extend_lease(s, job_id, W2, 60) is False
        assert await fail(s, claimed, W2, "x", retryable=True) is None
        assert await extend_lease(s, job_id, W1, 600) is True
        assert await complete(s, job_id, W1) is True
    stored = await job(session_factory, job_id)
    assert (stored.status, stored.locked_by) == ("done", None)
    assert stored.finished_at is not None


async def test_retry_then_dead_after_max_attempts(session_factory) -> None:
    job_id = await put(session_factory, max_attempts=2)
    [claimed] = await take(session_factory)
    async with session_factory() as s, s.begin():
        assert await fail(s, claimed, W1, "HTTP 503: boom", retryable=True) == "queued"
    stored = await job(session_factory, job_id)
    assert stored.run_after > now_tz()
    assert stored.last_error == "HTTP 503: boom"

    async with session_factory() as s, s.begin():
        await s.execute(update(Job).values(run_after=func.now()))
    [claimed] = await take(session_factory)
    assert claimed.attempts == 2
    async with session_factory() as s, s.begin():
        assert await fail(s, claimed, W1, "HTTP 503", retryable=True) == "dead"


async def test_permanent_error_is_dead_at_once(session_factory) -> None:
    job_id = await put(session_factory)
    [claimed] = await take(session_factory)
    async with session_factory() as s, s.begin():
        assert await fail(s, claimed, W1, "GameNotFoundError", retryable=False) == "dead"
    assert (await job(session_factory, job_id)).attempts == 1


def test_retry_delay_grows_with_cap(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "job_retry_base_seconds", 10)
    monkeypatch.setattr(settings, "job_retry_max_seconds", 100)
    assert 7.5 <= retry_delay(1) <= 12.5
    assert 30 <= retry_delay(3) <= 50
    assert retry_delay(10) == 100


async def test_reaper_returns_expired_leases(session_factory) -> None:
    alive = await put(session_factory)
    crashed = await put(session_factory)
    exhausted = await put(session_factory, max_attempts=1)
    await take(session_factory)
    past = now_tz() - timedelta(seconds=1)
    async with session_factory() as s, s.begin():
        await s.execute(update(Job).where(Job.id != alive).values(locked_until=past))
        assert await reap_expired(s) == 2

    states = {}
    for job_id in (alive, crashed, exhausted):
        stored = await job(session_factory, job_id)
        states[job_id] = (stored.status, stored.attempts, stored.locked_by)
    assert states[alive] == ("running", 1, W1)
    assert states[crashed] == ("queued", 1, None)
    assert states[exhausted][0] == "dead"


async def test_release_refunds_attempt(session_factory) -> None:
    job_id = await put(session_factory)
    await take(session_factory)
    later = now_tz() + timedelta(minutes=5)
    async with session_factory() as s, s.begin():
        assert await release(s, job_id, W1, run_after=later)
    stored = await job(session_factory, job_id)
    assert (stored.status, stored.attempts, stored.locked_by) == ("queued", 0, None)
    assert stored.run_after >= later - timedelta(seconds=1)


async def test_retry_dead_resets_attempts(session_factory) -> None:
    job_id = await put(session_factory)
    [claimed] = await take(session_factory)
    async with session_factory() as s, s.begin():
        await fail(s, claimed, W1, "x", retryable=False)
        assert await retry_dead(s, job_id) is True
        assert await retry_dead(s, job_id) is False
    stored = await job(session_factory, job_id)
    assert (stored.status, stored.attempts) == ("queued", 0)


async def test_enqueue_sends_notify(session_factory, db_engine) -> None:
    import asyncpg

    from app.core.db import asyncpg_dsn

    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    received: list[str] = []
    conn = await asyncpg.connect(dsn)
    try:
        await conn.add_listener("jobs", lambda *args: received.append(args[-1]))
        await put(session_factory, kind="summarize")
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
    finally:
        await conn.close()
    assert received == ["summarize"]


def test_http_error_helper_is_valid() -> None:
    request = httpx.Request("GET", "https://x.test")
    with pytest.raises(httpx.HTTPStatusError):
        httpx.Response(500, request=request).raise_for_status()


async def test_payload_roundtrip(session_factory) -> None:
    async with session_factory() as s, s.begin():
        await enqueue(s, "summarize", {"game_id": 7, "source": "critic"}, run_id=None)
    [claimed] = await take(session_factory, kinds=("summarize",))
    assert claimed.payload == {"game_id": 7, "source": "critic"}
    async with session_factory() as s:
        assert (await s.scalar(select(Job.kind))) == "summarize"
