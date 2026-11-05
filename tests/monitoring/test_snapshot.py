from datetime import timedelta

from sqlalchemy import func, update

from app.core.clock import now_tz
from app.crawler.models import Run, RunItem, WorkerHeartbeat
from app.jobs.models import Job
from app.jobs.queue import enqueue
from app.monitoring.snapshot import snapshot, worker_alive
from tests.conftest import create_user


async def seed(session_factory) -> int:
    admin_id = await create_user(session_factory, "boss")
    now = now_tz()
    async with session_factory() as session, session.begin():
        old = Run(
            trigger="schedule",
            status="partial",
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=28),
        )
        current = Run(trigger="manual", status="running", requested_by=admin_id)
        session.add_all([old, current])
        await session.flush()
        session.add_all(
            [
                RunItem(run_id=old.id, slug="a", stage="scrape", status="created"),
                RunItem(run_id=old.id, slug="b", stage="scrape", status="updated"),
                RunItem(run_id=old.id, slug="c", stage="scrape", status="retrying", error="503"),
                RunItem(run_id=old.id, slug="c", stage="scrape", status="failed", error="dead"),
                RunItem(
                    run_id=old.id, slug="a", stage="summarize_user", status="failed", error="llm"
                ),
            ]
        )
        for kind, status in [
            ("scrape_game", "queued"),
            ("scrape_game", "queued"),
            ("summarize", "running"),
            ("embed", "dead"),
        ]:
            job_id = await enqueue(session, kind, {"n": 1})
            await session.execute(update(Job).where(Job.id == job_id).values(status=status))
        expired = await enqueue(session, "embed", {"game_id": 1})
        await session.execute(
            update(Job)
            .where(Job.id == expired)
            .values(
                status="running",
                locked_by="dead-worker",
                locked_until=func.now() - timedelta(minutes=1),
            )
        )
        session.add(WorkerHeartbeat(worker="w1", status="running", current_task="embed#3"))
        session.add(
            WorkerHeartbeat(worker="w2", status="idle", updated_at=now - timedelta(hours=1))
        )
        return current.id


async def test_snapshot(session_factory) -> None:
    current_id = await seed(session_factory)

    async with session_factory() as session:
        data = await snapshot(session)
        alive = await worker_alive(session)

    assert alive is True
    assert [(w.worker, w.alive) for w in data.workers] == [("w1", True), ("w2", False)]
    assert data.current_run.id == current_id
    assert data.current_run.requested_by == "boss"
    old = data.runs[1]
    assert (old.processed, old.created, old.updated, old.errors_count) == (3, 1, 1, 2)
    assert [e.error for e in data.errors] == ["llm", "dead", "503"]
    queue = {q.kind: (q.queued, q.running, q.dead) for q in data.queue}
    assert queue["scrape_game"] == (2, 0, 0)
    assert queue["summarize"] == (0, 1, 0)
    assert queue["embed"] == (0, 1, 1)
    assert [j.locked_by for j in data.expired] == ["dead-worker"]
    assert [j.kind for j in data.dead] == ["embed"]
    assert data.collect_active is False
    assert data.counters.errors_today == 2


async def test_worker_alive_ignores_stopped(session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(WorkerHeartbeat(worker="w1", status="stopped"))
    async with session_factory() as session:
        assert await worker_alive(session) is False
