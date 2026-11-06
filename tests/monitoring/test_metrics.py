import math
from datetime import timedelta

from prometheus_client import REGISTRY
from sqlalchemy import func, update

from app.crawler.models import Run, RunItem, WorkerHeartbeat
from app.jobs.models import Breaker, Job
from app.jobs.queue import enqueue
from app.monitoring.metrics import refresh


def sample(metric: str, **labels) -> float | None:
    return REGISTRY.get_sample_value(metric, labels)


async def test_refresh_on_empty_database(session_factory) -> None:
    async with session_factory() as session:
        await refresh(session)
    assert math.isinf(sample("crawler_last_success_age_seconds"))
    assert math.isinf(sample("crawler_worker_heartbeat_age_seconds"))
    assert sample("jobs_queue", kind="collect", status="queued") == 0
    assert sample("crawler_error_ratio_last_hour") == 0


async def test_refresh_reads_queue_runs_and_errors(session_factory) -> None:
    async with session_factory() as session, session.begin():
        done = Run(trigger="schedule", status="success")
        running = Run(trigger="manual", status="running")
        session.add_all([done, running])
        await session.flush()
        await session.execute(
            update(Run)
            .where(Run.id == done.id)
            .values(
                started_at=func.now() - timedelta(minutes=30),
                finished_at=func.now() - timedelta(minutes=20),
            )
        )
        await session.execute(
            update(Run)
            .where(Run.id == running.id)
            .values(started_at=func.now() - timedelta(minutes=5))
        )
        for status in ("created", "updated", "updated", "failed", "retrying"):
            session.add(RunItem(run_id=done.id, slug="g", stage="scrape", status=status))
        for n, (kind, status) in enumerate(
            (
                ("scrape_game", "queued"),
                ("scrape_game", "queued"),
                ("embed", "dead"),
            )
        ):
            job_id = await enqueue(session, kind, {"n": n})
            await session.execute(update(Job).where(Job.id == job_id).values(status=status))
        session.add(WorkerHeartbeat(worker="w", status="idle"))
        session.add(Breaker(name="llm", state="open", failures=3))

    async with session_factory() as session:
        await refresh(session)

    assert sample("jobs_queue", kind="scrape_game", status="queued") == 2
    assert sample("jobs_queue", kind="embed", status="dead") == 1
    assert 19 * 60 < sample("crawler_last_success_age_seconds") < 21 * 60
    assert 9 * 60 < sample("crawler_last_run_duration_seconds") < 11 * 60
    assert 4 * 60 < sample("crawler_current_run_seconds") < 6 * 60
    assert sample("crawler_worker_heartbeat_age_seconds") < 60
    assert sample("crawler_items_last_hour", status="updated") == 2
    assert sample("crawler_error_ratio_last_hour") == 0.25
    assert sample("breaker_open", name="llm") == 1


async def test_metrics_endpoint_internal_or_admin(make_client) -> None:
    async with make_client(ip="10.0.0.5") as internal:
        ok = await internal.get("/metrics")
    async with make_client(ip="8.8.8.8") as public:
        hidden = await public.get("/metrics")
    async with make_client("viewer", ip="8.8.4.4") as viewer:
        viewer_hidden = await viewer.get("/metrics")
    async with make_client("admin", ip="1.1.1.1") as admin:
        admin_ok = await admin.get("/metrics")

    assert ok.status_code == 200
    assert "jobs_queue" in ok.text
    assert ok.headers["content-type"].startswith("text/plain")
    assert (hidden.status_code, viewer_hidden.status_code) == (404, 404)
    assert admin_ok.status_code == 200


async def test_request_id_header(make_client) -> None:
    async with make_client() as client:
        response = await client.get("/health")
    assert len(response.headers["X-Request-ID"]) == 12
