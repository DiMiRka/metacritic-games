"""Очередь задач в Postgres: постановка, выборка с арендой, завершение, повторы"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.jobs.models import Job, JobStatus

CHANNEL = "jobs"
MAX_ERROR_LENGTH = 1000


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    run_id: int | None
    requested_by: int | None


async def notify(session: AsyncSession, payload: str = "") -> None:
    """Будит воркеры; доставляется при коммите, задача к этому моменту уже в таблице"""
    await session.execute(select(func.pg_notify(CHANNEL, payload)))


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 0,
    dedupe_key: str | None = None,
    run_id: int | None = None,
    requested_by: int | None = None,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
) -> int | None:
    """Ставит задачу и будит воркеры. None, если такая же работа уже в очереди"""
    values: dict[str, Any] = {
        "kind": kind,
        "payload": payload or {},
        "priority": priority,
        "dedupe_key": dedupe_key,
        "run_id": run_id,
        "requested_by": requested_by,
        "max_attempts": max_attempts or settings.job_max_attempts,
    }
    if run_after is not None:
        values["run_after"] = run_after
    stmt = (
        insert(Job)
        .values(**values)
        .on_conflict_do_nothing(
            # Условие частичного индекса должно быть литералом, иначе Postgres его не узнает
            index_elements=["dedupe_key"],
            index_where=text("status IN ('queued', 'running')"),
        )
        .returning(Job.id)
    )
    job_id = await session.scalar(stmt)
    if job_id is not None:
        await notify(session, kind)
    return job_id


_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', locked_by = :worker,
        locked_until = now() + make_interval(secs => :lease),
        attempts = attempts + 1, started_at = now()
    WHERE id IN (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now() AND kind = ANY(:kinds)
        ORDER BY priority DESC, run_after, id
        FOR UPDATE SKIP LOCKED LIMIT :limit
    )
    RETURNING id, kind, payload, attempts, max_attempts, run_id, requested_by
    """
)


async def claim(
    session: AsyncSession, worker: str, kinds: list[str], limit: int, lease: float
) -> list[ClaimedJob]:
    """Берёт до limit задач; вызывающий коммитит сразу, работа идёт вне этой транзакции"""
    if limit <= 0:
        return []
    rows = await session.execute(
        _CLAIM, {"worker": worker, "kinds": kinds, "limit": limit, "lease": float(lease)}
    )
    return sorted((ClaimedJob(**row._mapping) for row in rows), key=lambda j: j.id)


def _owned(job_id: int, worker: str) -> tuple:
    return (Job.id == job_id, Job.locked_by == worker, Job.status == JobStatus.RUNNING)


async def extend_lease(session: AsyncSession, job_id: int, worker: str, lease: float) -> bool:
    result = await session.execute(
        update(Job)
        .where(*_owned(job_id, worker))
        .values(locked_until=func.now() + timedelta(seconds=lease))
    )
    return result.rowcount == 1


async def complete(session: AsyncSession, job_id: int, worker: str) -> bool:
    """done; False, если аренду уже забрал кто-то другой и результат надо отбросить"""
    result = await session.execute(
        update(Job)
        .where(*_owned(job_id, worker))
        .values(
            status=JobStatus.DONE,
            locked_by=None,
            locked_until=None,
            finished_at=func.now(),
            last_error=None,
        )
    )
    return result.rowcount == 1


def retry_delay(attempts: int) -> float:
    base = settings.job_retry_base_seconds * 2 ** max(attempts - 1, 0)
    return min(base * random.uniform(0.75, 1.25), settings.job_retry_max_seconds)


async def fail(
    session: AsyncSession, job: ClaimedJob, worker: str, error: str, retryable: bool
) -> str | None:
    """Повтор с отсрочкой или dead. Возвращает новый статус, None если аренда потеряна"""
    retry = retryable and job.attempts < job.max_attempts
    values: dict[str, Any] = {
        "locked_by": None,
        "locked_until": None,
        "last_error": error[:MAX_ERROR_LENGTH],
    }
    if retry:
        status = JobStatus.QUEUED
        values["run_after"] = func.now() + timedelta(seconds=retry_delay(job.attempts))
    else:
        status = JobStatus.DEAD
        values["finished_at"] = func.now()
    result = await session.execute(
        update(Job).where(*_owned(job.id, worker)).values(status=status, **values)
    )
    return status if result.rowcount == 1 else None


async def release(
    session: AsyncSession, job_id: int, worker: str, run_after: datetime | None = None
) -> bool:
    """Возвращает задачу в очередь без траты попытки: остановка воркера или открытый автомат"""
    result = await session.execute(
        update(Job)
        .where(*_owned(job_id, worker))
        .values(
            status=JobStatus.QUEUED,
            attempts=Job.attempts - 1,
            locked_by=None,
            locked_until=None,
            run_after=run_after if run_after is not None else func.now(),
        )
    )
    return result.rowcount == 1


async def reap_expired(session: AsyncSession) -> int:
    """Задачи упавших воркеров: истёкшая аренда возвращает их в очередь

    Попытка уже засчитана при выборке. Если попытки кончились, задача становится dead,
    иначе задача, которая роняет воркер, крутилась бы бесконечно
    """
    expired = (Job.status == JobStatus.RUNNING, Job.locked_until < func.now())
    dead = await session.execute(
        update(Job)
        .where(*expired, Job.attempts >= Job.max_attempts)
        .values(
            status=JobStatus.DEAD,
            locked_by=None,
            locked_until=None,
            finished_at=func.now(),
            last_error="Аренда истекла, попытки кончились",
        )
    )
    requeued = await session.execute(
        update(Job)
        .where(*expired)
        .values(status=JobStatus.QUEUED, locked_by=None, locked_until=None, run_after=func.now())
    )
    return dead.rowcount + requeued.rowcount


async def retry_dead(session: AsyncSession, job_id: int) -> bool:
    """Ручной повтор dead-задачи с чистым счётчиком попыток.
    Через функцию retry_dead_job: прав на запись в jobs у сайта нет
    """
    return bool(await session.scalar(select(func.retry_dead_job(job_id))))
