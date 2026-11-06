"""Показатели из базы для Prometheus: общие для всех экземпляров, поэтому их отдаёт web.
Счётчики самого воркера (запросы к Metacritic, LLM, задачи) он отдаёт со своего порта
"""

import math

from prometheus_client import Gauge
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.models import Run, RunItemStatus, RunStatus, WorkerHeartbeat
from app.jobs.models import Breaker, BreakerState, Job, JobKind, JobStatus
from app.monitoring.models import BackupRun, BackupStatus

QUEUE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DEAD)
FINAL_ITEM_STATUSES = (
    RunItemStatus.CREATED,
    RunItemStatus.UPDATED,
    RunItemStatus.SKIPPED,
    RunItemStatus.FAILED,
)

QUEUE = Gauge("jobs_queue", "Задачи в очереди по видам и статусам", ["kind", "status"])
LAST_SUCCESS_AGE = Gauge(
    "crawler_last_success_age_seconds",
    "Сколько секунд назад закончился последний успешный проход, +Inf если не было",
)
LAST_RUN_DURATION = Gauge(
    "crawler_last_run_duration_seconds", "Длительность последнего завершённого прохода"
)
CURRENT_RUN_AGE = Gauge(
    "crawler_current_run_seconds", "Сколько идёт текущий проход, 0 если прохода нет"
)
HEARTBEAT_AGE = Gauge(
    "crawler_worker_heartbeat_age_seconds",
    "Сколько секунд назад пришёл последний heartbeat любого воркера, +Inf если не было",
)
ITEMS_LAST_HOUR = Gauge(
    "crawler_items_last_hour", "Итоги этапов за последний час по статусам", ["status"]
)
ERROR_RATIO = Gauge(
    "crawler_error_ratio_last_hour", "Доля этапов, которые сдались, среди завершённых за час"
)
BREAKER_OPEN = Gauge("breaker_open", "Автомат открыт или ждёт пробы: 1, закрыт: 0", ["name"])
BACKUP_AGE = Gauge(
    "backup_last_success_age_seconds",
    "Сколько секунд назад начался последний бэкап, прошедший проверку восстановления",
)
BACKUP_SIZE = Gauge("backup_last_size_bytes", "Размер последнего успешного бэкапа")
BACKUP_FAILED = Gauge("backup_last_run_failed", "Последний завершённый бэкап не удался: 1")


def _age(value: float | None) -> float:
    return math.inf if value is None else max(0.0, float(value))


async def refresh(session: AsyncSession) -> None:
    """Обновляет показатели перед каждым опросом Prometheus"""
    counts = {
        (kind, status): n
        for kind, status, n in await session.execute(
            select(Job.kind, Job.status, func.count())
            .where(Job.status.in_(QUEUE_STATUSES))
            .group_by(Job.kind, Job.status)
        )
    }
    for kind in JobKind:
        for status in QUEUE_STATUSES:
            QUEUE.labels(kind.value, status.value).set(counts.get((kind, status), 0))

    since = func.extract("epoch", func.now() - func.max(Run.finished_at))
    LAST_SUCCESS_AGE.set(
        _age(await session.scalar(select(since).where(Run.status == RunStatus.SUCCESS)))
    )
    last_duration = await session.scalar(
        select(func.extract("epoch", Run.finished_at - Run.started_at))
        .where(Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    LAST_RUN_DURATION.set(float(last_duration or 0))
    current = await session.scalar(
        select(func.extract("epoch", func.now() - func.min(Run.started_at))).where(
            Run.status == RunStatus.RUNNING
        )
    )
    CURRENT_RUN_AGE.set(float(current or 0))
    HEARTBEAT_AGE.set(
        _age(
            await session.scalar(
                select(func.extract("epoch", func.now() - func.max(WorkerHeartbeat.updated_at)))
            )
        )
    )

    items = dict(
        (
            await session.execute(
                text(
                    "SELECT status, count(*) FROM run_items "
                    "WHERE created_at >= now() - interval '1 hour' GROUP BY status"
                )
            )
        ).all()
    )
    for status in FINAL_ITEM_STATUSES:
        ITEMS_LAST_HOUR.labels(status.value).set(items.get(status, 0))
    finished = sum(items.get(status, 0) for status in FINAL_ITEM_STATUSES)
    ERROR_RATIO.set(items.get(RunItemStatus.FAILED, 0) / finished if finished else 0.0)

    for name, state in await session.execute(select(Breaker.name, Breaker.state)):
        BREAKER_OPEN.labels(name).set(0 if state == BreakerState.CLOSED else 1)

    last_success = (
        await session.execute(
            select(func.extract("epoch", func.now() - BackupRun.started_at), BackupRun.size_bytes)
            .where(BackupRun.status == BackupStatus.SUCCESS, BackupRun.restore_checked)
            .order_by(BackupRun.started_at.desc())
            .limit(1)
        )
    ).first()
    BACKUP_AGE.set(_age(last_success[0] if last_success else None))
    BACKUP_SIZE.set(float(last_success[1] or 0) if last_success else 0)
    last_status = await session.scalar(
        select(BackupRun.status)
        .where(BackupRun.status != BackupStatus.RUNNING)
        .order_by(BackupRun.started_at.desc())
        .limit(1)
    )
    BACKUP_FAILED.set(1 if last_status == BackupStatus.FAILED else 0)
