from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.catalog.models import Game, ReviewSummary
from app.core.clock import day_bounds, now_tz
from app.core.config import settings
from app.crawler.models import CrawlState, Run, RunItem, RunStatus, WorkerHeartbeat
from app.crawler.runs import run_stats
from app.jobs.models import ACTIVE_STATUSES, Breaker, Job, JobKind, JobStatus

WORKER_STALE_SECONDS = 45
RECENT_RUNS = 10
RECENT_ERRORS = 10
RECENT_DEAD = 10


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
    unchanged: int
    errors_count: int
    duration_seconds: float | None
    requested_by: str | None


class ErrorOut(BaseModel):
    run_id: int
    created_at: datetime
    slug: str | None
    stage: str
    status: str
    error: str


class QueueRow(BaseModel):
    kind: str
    queued: int = 0
    running: int = 0
    dead: int = 0


class JobOut(BaseModel):
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    locked_by: str | None
    locked_until: datetime | None
    last_error: str | None
    finished_at: datetime | None


class BreakerOut(BaseModel):
    name: str
    state: str
    failures: int
    opened_until: datetime | None


class Counters(BaseModel):
    games_total: int
    processed_today: int
    summaries_total: int
    # Токены резюме, пересчитанных сегодня, и оценка стоимости по ценам из настроек
    llm_input_tokens_today: int
    llm_output_tokens_today: int
    llm_cost_today: float | None
    embeddings_total: int
    runs_today: int
    errors_today: int
    phase: str | None
    next_page: int | None


class MonitorSnapshot(BaseModel):
    now: datetime
    workers: list[WorkerOut]
    current_run: RunOut | None
    collect_active: bool
    runs: list[RunOut]
    errors: list[ErrorOut]
    queue: list[QueueRow]
    expired: list[JobOut]
    dead: list[JobOut]
    breakers: list[BreakerOut]
    counters: Counters


async def worker_alive(session: AsyncSession, now: datetime | None = None) -> bool:
    """Есть ли хотя бы один живой экземпляр воркера"""
    fresh = (now or now_tz()) - timedelta(seconds=WORKER_STALE_SECONDS)
    count = await session.scalar(
        select(func.count())
        .select_from(WorkerHeartbeat)
        .where(WorkerHeartbeat.updated_at >= fresh, WorkerHeartbeat.status != "stopped")
    )
    return bool(count)


async def _runs(session: AsyncSession, now: datetime) -> list[RunOut]:
    runs = (await session.scalars(select(Run).order_by(Run.id.desc()).limit(RECENT_RUNS))).all()
    stats = await run_stats(session, [r.id for r in runs])
    requester_ids = {r.requested_by for r in runs if r.requested_by}
    logins = {}
    if requester_ids:
        rows = await session.execute(select(User.id, User.login).where(User.id.in_(requester_ids)))
        logins = dict(rows.all())
    result = []
    for run in runs:
        s = stats.get(run.id)
        end = run.finished_at or (now if run.status == RunStatus.RUNNING else None)
        result.append(
            RunOut(
                id=run.id,
                trigger=run.trigger,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                processed=s.processed if s else 0,
                created=s.created if s else 0,
                updated=s.updated if s else 0,
                unchanged=s.unchanged if s else 0,
                errors_count=s.failed if s else 0,
                duration_seconds=(end - run.started_at).total_seconds() if end else None,
                requested_by=logins.get(run.requested_by) if run.requested_by else None,
            )
        )
    return result


async def _queue(session: AsyncSession) -> list[QueueRow]:
    rows = await session.execute(
        select(Job.kind, Job.status, func.count())
        .where(Job.status.in_([*ACTIVE_STATUSES, JobStatus.DEAD]))
        .group_by(Job.kind, Job.status)
    )
    by_kind = {kind: QueueRow(kind=kind) for kind in JobKind}
    for kind, status, count in rows:
        setattr(by_kind[kind], status, count)
    return list(by_kind.values())


async def _llm_usage(session: AsyncSession, day_start: datetime) -> dict[str, Any]:
    tokens_in, tokens_out = (
        await session.execute(
            select(
                func.coalesce(func.sum(ReviewSummary.input_tokens), 0),
                func.coalesce(func.sum(ReviewSummary.output_tokens), 0),
            ).where(ReviewSummary.generated_at >= day_start)
        )
    ).one()
    return {
        "llm_input_tokens_today": tokens_in,
        "llm_output_tokens_today": tokens_out,
        "llm_cost_today": llm_cost(tokens_in, tokens_out),
    }


def _job_out(job: Job) -> JobOut:
    return JobOut.model_validate(job, from_attributes=True)


def llm_cost(input_tokens: int, output_tokens: int) -> float | None:
    price_in, price_out = settings.llm_price_input_per_mtok, settings.llm_price_output_per_mtok
    if not (price_in or price_out):
        return None
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


async def snapshot(session: AsyncSession, now: datetime | None = None) -> MonitorSnapshot:
    now = now or now_tz()
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

    runs = await _runs(session, now)
    collect_active = bool(
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.kind == JobKind.COLLECT, Job.status.in_(ACTIVE_STATUSES))
        )
    )
    errors = (
        await session.scalars(
            select(RunItem)
            .where(RunItem.error.is_not(None))
            .order_by(RunItem.id.desc())
            .limit(RECENT_ERRORS)
        )
    ).all()
    expired = (
        await session.scalars(
            select(Job)
            .where(Job.status == JobStatus.RUNNING, Job.locked_until < func.now())
            .order_by(Job.locked_until)
        )
    ).all()
    dead = (
        await session.scalars(
            select(Job)
            .where(Job.status == JobStatus.DEAD)
            .order_by(Job.finished_at.desc().nulls_last(), Job.id.desc())
            .limit(RECENT_DEAD)
        )
    ).all()

    games_total, processed_today, embeddings_total = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Game.last_processed_at >= day_start),
                func.count(Game.embedding),
            ).select_from(Game)
        )
    ).one()
    runs_today = await session.scalar(
        select(func.count()).select_from(Run).where(Run.started_at >= day_start)
    )
    errors_today = await session.scalar(
        select(func.count())
        .select_from(RunItem)
        .where(RunItem.created_at >= day_start, RunItem.status == "failed")
    )
    state = await session.get(CrawlState, today)

    return MonitorSnapshot(
        now=now,
        workers=workers,
        current_run=next((r for r in runs if r.status == RunStatus.RUNNING), None),
        collect_active=collect_active,
        runs=runs,
        errors=[
            ErrorOut(
                run_id=e.run_id,
                created_at=e.created_at,
                slug=e.slug,
                stage=e.stage,
                status=e.status,
                error=e.error,
            )
            for e in errors
        ],
        queue=await _queue(session),
        expired=[_job_out(j) for j in expired],
        dead=[_job_out(j) for j in dead],
        breakers=[
            BreakerOut.model_validate(b, from_attributes=True)
            for b in (await session.scalars(select(Breaker).order_by(Breaker.name))).all()
        ],
        counters=Counters(
            **await _llm_usage(session, day_start),
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
