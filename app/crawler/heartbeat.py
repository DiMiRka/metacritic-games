from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawler.models import WorkerHeartbeat, WorkerStatus

# Сигнал раз в 15 секунд: три пропущенных подряд, и экземпляр считается неживым
WORKER_STALE_SECONDS = 45


async def live_workers(session: AsyncSession) -> list[str]:
    """Экземпляры воркера, которые недавно подавали сигнал и не остановлены"""
    rows = await session.scalars(
        select(WorkerHeartbeat.worker)
        .where(
            WorkerHeartbeat.updated_at >= func.now() - timedelta(seconds=WORKER_STALE_SECONDS),
            WorkerHeartbeat.status != WorkerStatus.STOPPED,
        )
        .order_by(WorkerHeartbeat.worker)
    )
    return list(rows)


async def beat(
    session_factory: async_sessionmaker[AsyncSession],
    worker: str,
    status: str | None = None,
    current_task: str | None = None,
    next_collect_at: datetime | None = None,
) -> None:
    """Обновляет heartbeat; без status только продлевает updated_at"""
    stmt = insert(WorkerHeartbeat).values(
        worker=worker,
        status=status or "idle",
        current_task=current_task,
        next_collect_at=next_collect_at,
    )
    changes = {"updated_at": func.now()}
    if status is not None:
        changes |= {
            "status": stmt.excluded.status,
            "current_task": stmt.excluded.current_task,
            "next_collect_at": stmt.excluded.next_collect_at,
        }
    async with session_factory() as session, session.begin():
        await session.execute(stmt.on_conflict_do_update(index_elements=["worker"], set_=changes))


async def cleanup_heartbeats(
    session_factory: async_sessionmaker[AsyncSession], older_than: timedelta = timedelta(days=1)
) -> int:
    """Строки экземпляров, остановленных штатно (так уходят старые контейнеры при деплое: у
    новых другой hostname), и тех, кто давно молчит. Недавно замолчавшие остаются: монитор
    должен показывать, что воркер упал, а не прятать его"""
    async with session_factory() as session, session.begin():
        result = await session.execute(
            delete(WorkerHeartbeat).where(
                (WorkerHeartbeat.status == WorkerStatus.STOPPED)
                | (WorkerHeartbeat.updated_at < func.now() - older_than)
            )
        )
        return result.rowcount
