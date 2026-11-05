from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.catalog.models import Game, ReviewSummary
from app.core.clock import day_bounds
from app.core.config import settings
from app.crawler.lock import crawl_lock_held
from app.crawler.models import CrawlState, Run, RunStatus, WorkerHeartbeat

WORKER_STALE_SECONDS = 45
RECENT_RUNS = 10
RECENT_ERRORS = 10
# Запись осталась в running, а блокировку никто не держит: воркер упал посреди прохода
STALE = "stale"


class WorkerOut(BaseModel):
    worker: str
    status: str
    current_task: str | None
    updated_at: datetime
    alive: bool


class RunOut(BaseModel):
    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    processed: int
    created: int
    updated: int
    errors_count: int
    duration_seconds: float | None
    requested_by: str | None


class ErrorOut(BaseModel):
    run_id: int
    started_at: datetime
    slug: str | None
    error: str


class Counters(BaseModel):
    games_total: int
    processed_today: int
    summaries_total: int
    embeddings_total: int
    runs_today: int
    errors_today: int
    phase: str | None
    next_page: int | None


class MonitorSnapshot(BaseModel):
    now: datetime
    workers: list[WorkerOut]
    current_run: RunOut | None
    runs: list[RunOut]
    errors: list[ErrorOut]
    counters: Counters


def _run_out(run: Run, now: datetime, current_id: int | None, logins: dict[int, str]) -> RunOut:
    status = run.status
    if status == RunStatus.RUNNING and run.id != current_id:
        status = STALE
    end = run.finished_at or (now if status == RunStatus.RUNNING else None)
    return RunOut(
        id=run.id,
        trigger=run.trigger,
        status=status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        processed=run.processed,
        created=run.created,
        updated=run.updated,
        errors_count=len(run.errors),
        duration_seconds=(end - run.started_at).total_seconds() if end else None,
        requested_by=logins.get(run.requested_by) if run.requested_by else None,
    )


async def worker_alive(session: AsyncSession, now: datetime | None = None) -> bool:
    now = now or datetime.now(settings.tz)
    fresh = now - timedelta(seconds=WORKER_STALE_SECONDS)
    count = await session.scalar(
        select(func.count())
        .select_from(WorkerHeartbeat)
        .where(WorkerHeartbeat.updated_at >= fresh, WorkerHeartbeat.status != "stopped")
    )
    return bool(count)


async def snapshot(session: AsyncSession, now: datetime | None = None) -> MonitorSnapshot:
    now = now or datetime.now(settings.tz)
    today, day_start = day_bounds(now)
    fresh = now - timedelta(seconds=WORKER_STALE_SECONDS)

    heartbeats = (
        await session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker))
    ).all()
    workers = [
        WorkerOut(
            worker=h.worker,
            status=h.status,
            current_task=h.current_task,
            updated_at=h.updated_at,
            alive=h.updated_at >= fresh and h.status != "stopped",
        )
        for h in heartbeats
    ]

    runs = (await session.scalars(select(Run).order_by(Run.id.desc()).limit(RECENT_RUNS))).all()
    latest_running = next((r for r in runs if r.status == RunStatus.RUNNING), None)
    current = latest_running if await crawl_lock_held(session) else None
    current_id = current.id if current else None
    requester_ids = {r.requested_by for r in runs if r.requested_by}
    logins = (
        dict(
            (
                await session.execute(select(User.id, User.login).where(User.id.in_(requester_ids)))
            ).all()
        )
        if requester_ids
        else {}
    )

    error_rows = await session.execute(
        text(
            "SELECT r.id, r.started_at, e->>'slug', e->>'error' "
            "FROM runs r, jsonb_array_elements(r.errors) WITH ORDINALITY AS x(e, n) "
            "ORDER BY r.id DESC, n DESC LIMIT :limit"
        ),
        {"limit": RECENT_ERRORS},
    )

    games_total, processed_today, embeddings_total = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Game.last_processed_at >= day_start),
                func.count(Game.embedding),
            ).select_from(Game)
        )
    ).one()
    runs_today, errors_today = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(func.jsonb_array_length(Run.errors)), 0),
            ).where(Run.started_at >= day_start)
        )
    ).one()
    state = await session.get(CrawlState, today)

    return MonitorSnapshot(
        now=now,
        workers=workers,
        current_run=_run_out(current, now, current_id, logins) if current else None,
        runs=[_run_out(r, now, current_id, logins) for r in runs],
        errors=[
            ErrorOut(run_id=i, started_at=s, slug=slug, error=e) for i, s, slug, e in error_rows
        ],
        counters=Counters(
            games_total=games_total,
            processed_today=processed_today,
            summaries_total=await session.scalar(select(func.count()).select_from(ReviewSummary)),
            embeddings_total=embeddings_total,
            runs_today=runs_today,
            errors_today=errors_today,
            phase=state.phase if state else None,
            next_page=state.next_page if state else None,
        ),
    )
