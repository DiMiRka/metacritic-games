import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import now_tz
from app.core.config import settings
from app.core.db import asyncpg_dsn
from app.crawler.models import WorkerHeartbeat
from app.crawler.stages import CrawlStages
from app.jobs.models import Job
from app.jobs.queue import enqueue
from app.worker.main import Worker
from tests.conftest import FakeClient


@pytest.fixture(autouse=True)
def fast_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings, "job_poll_seconds", 0.05)
    monkeypatch.setattr(settings, "worker_shutdown_grace_seconds", 0.3)


class SlowClient(FakeClient):
    """Карточка игры «грузится» минуту: задача не успеет доделаться до остановки"""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def game(self, slug: str):
        self.started.set()
        await asyncio.sleep(60)


def make_worker(db_engine, session_factory, worker_id: str, client=None) -> Worker:
    stages = CrawlStages(
        client=client or FakeClient(),
        engine=db_engine,
        session_factory=session_factory,
        summarizer=None,
        embedder=None,
    )
    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    return Worker(stages, session_factory, worker_id, dsn=dsn)


async def heartbeats(session_factory) -> dict[str, tuple[str, str | None]]:
    async with session_factory() as session:
        rows = (await session.scalars(select(WorkerHeartbeat))).all()
    return {h.worker: (h.status, h.current_task) for h in rows}


async def test_each_instance_has_own_heartbeat(db_engine, session_factory) -> None:
    async with session_factory() as session, session.begin():
        old = now_tz() - timedelta(days=2)
        session.add(WorkerHeartbeat(worker="gone", status="idle", updated_at=old))
    first = make_worker(db_engine, session_factory, "host-a:1")
    second = make_worker(db_engine, session_factory, "host-b:2")
    tasks = [asyncio.create_task(w.run(schedule=False)) for w in (first, second)]
    for _ in range(100):
        during = await heartbeats(session_factory)
        if set(during) == {"host-a:1", "host-b:2"}:
            break
        await asyncio.sleep(0.05)
    for w in (first, second):
        w.stop.set()
    await asyncio.gather(*tasks)

    assert set(during) == {"host-a:1", "host-b:2"}
    assert {status for status, _ in during.values()} == {"idle"}
    after = await heartbeats(session_factory)
    assert after == {"host-a:1": ("stopped", None), "host-b:2": ("stopped", None)}


async def test_shutdown_returns_unfinished_job(db_engine, session_factory) -> None:
    client = SlowClient()
    async with session_factory() as session, session.begin():
        job_id = await enqueue(session, "scrape_game", {"slug": "elden-ring"})
    worker = make_worker(db_engine, session_factory, "host-a:1", client)
    task = asyncio.create_task(worker.run(schedule=False))
    await asyncio.wait_for(client.started.wait(), 5)

    await worker.heartbeat()
    during = await heartbeats(session_factory)
    worker.stop.set()
    await asyncio.wait_for(task, 5)

    assert during["host-a:1"] == ("running", f"scrape_game#{job_id}")
    async with session_factory() as session:
        job = await session.get(Job, job_id)
    assert (job.status, job.attempts, job.locked_by) == ("queued", 0, None)
    assert (await heartbeats(session_factory))["host-a:1"][0] == "stopped"


def test_worker_identity_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_id", "")
    assert ":" in settings.worker_identity
    monkeypatch.setattr(settings, "worker_id", "worker-1")
    assert settings.worker_identity == "worker-1"
