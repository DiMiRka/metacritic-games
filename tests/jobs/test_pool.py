import asyncio
import contextlib

from sqlalchemy import select

from app.core.errors import PermanentError
from app.jobs.models import Job
from app.jobs.pool import Finish, JobPool, JobType
from app.jobs.queue import enqueue

WORKER = "pool-test"


async def put(session_factory, n: int, kind: str = "embed") -> list[int]:
    ids = []
    async with session_factory() as s, s.begin():
        for i in range(n):
            ids.append(await enqueue(s, kind, {"i": i}))
    return ids


async def statuses(session_factory) -> dict[int, tuple[str, int]]:
    async with session_factory() as s:
        jobs = (await s.scalars(select(Job).order_by(Job.id))).all()
    return {j.id: (j.status, j.attempts) for j in jobs}


@contextlib.asynccontextmanager
async def running(pool: JobPool):
    stop = asyncio.Event()
    task = asyncio.create_task(pool.run(stop))
    try:
        yield stop
    finally:
        stop.set()
        await task


async def wait_until(predicate, attempts: int = 250) -> None:
    for _ in range(attempts):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("условие не выполнилось")


def make_pool(session_factory, handler, *, concurrency=2, poll=0.05, on_finish=None, **kw):
    return JobPool(
        "test",
        {"embed": JobType(handler, on_finish)},
        concurrency,
        session_factory,
        WORKER,
        lease=60,
        poll=poll,
        **kw,
    )


async def test_runs_jobs_with_concurrency_limit(session_factory) -> None:
    ids = await put(session_factory, 5)
    now_running, peak = 0, 0

    async def handler(job):
        nonlocal now_running, peak
        now_running += 1
        peak = max(peak, now_running)
        await asyncio.sleep(0.05)
        now_running -= 1
        return job.payload["i"]

    pool = make_pool(session_factory, handler, concurrency=2)
    async with running(pool):

        async def all_done():
            return all(s == "done" for s, _ in (await statuses(session_factory)).values())

        await wait_until(all_done)
    assert peak == 2
    assert set(await statuses(session_factory)) == set(ids)


async def test_failures_are_classified(session_factory) -> None:
    await put(session_factory, 2)

    async def handler(job):
        if job.payload["i"] == 0:
            raise PermanentError("нет игры")
        raise ConnectionError("сеть")

    finishes: list[Finish] = []

    async def on_finish(session, job, finish):
        finishes.append(finish)

    pool = make_pool(session_factory, handler, on_finish=on_finish)
    async with running(pool):

        async def both_finished():
            return len(finishes) == 2

        await wait_until(both_finished)
    assert sorted(f.status for f in finishes) == ["dead", "queued"]
    async with session_factory() as s:
        errors = set((await s.scalars(select(Job.last_error))).all())
    assert "PermanentError: нет игры" in errors


async def test_finish_hook_runs_in_same_transaction(session_factory) -> None:
    [job_id] = await put(session_factory, 1)

    async def handler(job):
        return "ok"

    async def on_finish(session, job, finish):
        raise RuntimeError("хук упал")

    pool = make_pool(session_factory, handler, on_finish=on_finish)
    async with running(pool):
        await asyncio.sleep(0.3)
    # Хук упал, транзакция откатилась: задача осталась running до истечения аренды
    assert (await statuses(session_factory))[job_id][0] == "running"


async def test_notify_wakes_pool_before_poll(session_factory, db_engine) -> None:
    from app.core.db import asyncpg_dsn
    from app.jobs.listener import listen

    done = asyncio.Event()

    async def handler(job):
        done.set()

    pool = make_pool(session_factory, handler, poll=30)
    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    stop_listen = asyncio.Event()
    listener = asyncio.create_task(listen(dsn, "jobs", lambda _: pool.wake.set(), stop_listen))
    try:
        async with running(pool):
            await asyncio.sleep(0.3)
            await put(session_factory, 1)
            await asyncio.wait_for(done.wait(), 5)
    finally:
        stop_listen.set()
        await listener


async def test_drain_releases_unfinished_jobs(session_factory) -> None:
    [quick, slow] = await put(session_factory, 2)
    started = asyncio.Event()

    async def handler(job):
        if job.payload["i"] == 1:
            started.set()
            await asyncio.sleep(60)

    pool = make_pool(session_factory, handler)
    async with running(pool):
        await asyncio.wait_for(started.wait(), 5)
    await pool.drain(grace=0.2)

    states = await statuses(session_factory)
    assert states[quick] == ("done", 1)
    assert states[slow] == ("queued", 0)
    assert pool.active == {}


async def test_gate_defers_without_attempt(session_factory) -> None:
    from datetime import timedelta

    from app.core.clock import now_tz

    [job_id] = await put(session_factory, 1)
    calls = 0

    async def handler(job):
        nonlocal calls
        calls += 1

    async def gate(job):
        return now_tz() + timedelta(minutes=10)

    pool = make_pool(session_factory, handler, gate=gate)
    async with running(pool):
        await asyncio.sleep(0.3)
    assert calls == 0
    assert (await statuses(session_factory))[job_id] == ("queued", 0)


async def test_job_logs_carry_context_and_dead_is_error(session_factory, log_records) -> None:
    from loguru import logger
    from prometheus_client import REGISTRY

    async with session_factory() as s, s.begin():
        await enqueue(s, "embed", {"slug": "elden-ring", "game_id": 5})
    before = (
        REGISTRY.get_sample_value("jobs_finished_total", {"kind": "embed", "status": "dead"}) or 0
    )

    async def handler(job):
        logger.info("внутри задачи")
        raise PermanentError("нет игры")

    finishes: list[Finish] = []

    async def on_finish(session, job, finish):
        finishes.append(finish)

    pool = make_pool(session_factory, handler, on_finish=on_finish)
    async with running(pool):

        async def finished():
            return bool(finishes)

        await wait_until(finished)

    inside = next(r for r in log_records if r["message"] == "внутри задачи")
    assert inside["extra"]["slug"] == "elden-ring"
    assert inside["extra"]["game_id"] == 5
    assert inside["extra"]["kind"] == "embed"
    dead = next(r for r in log_records if "dead после" in r["message"])
    assert dead["level"].name == "ERROR"
    assert dead["exception"] is not None
    after = REGISTRY.get_sample_value("jobs_finished_total", {"kind": "embed", "status": "dead"})
    assert after == before + 1


async def test_deferred_error_waits_without_spending_attempt(session_factory) -> None:
    """Суточная квота: задача ждёт сброса в очереди, попытка не тратится, run_items не пишется"""
    from datetime import UTC, datetime, timedelta

    from app.core.errors import DeferredError

    [job_id] = await put(session_factory, 1)
    until = datetime.now(UTC) + timedelta(hours=3)
    finished: list[Finish] = []

    async def handler(job):
        raise DeferredError("квота исчерпана", until)

    async def on_finish(session, job, finish):
        finished.append(finish)

    pool = make_pool(session_factory, handler, on_finish=on_finish)
    async with running(pool):

        async def deferred() -> bool:
            async with session_factory() as s:
                job = await s.get(Job, job_id)
            # С запасом в часы: часы базы в Docker могут расходиться с часами теста
            return job.status == "queued" and job.run_after > datetime.now(UTC) + timedelta(hours=2)

        await wait_until(deferred)

    async with session_factory() as s:
        job = await s.get(Job, job_id)
    assert (job.status, job.attempts, job.locked_by) == ("queued", 0, None)
    assert abs((job.run_after - until).total_seconds()) < 1
    assert finished == []
