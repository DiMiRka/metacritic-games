from datetime import timedelta

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawler.models import WorkerHeartbeat


async def beat(
    session_factory: async_sessionmaker[AsyncSession],
    worker: str,
    status: str | None = None,
    current_task: str | None = None,
) -> None:
    """Обновляет heartbeat; без status только продлевает updated_at"""
    stmt = insert(WorkerHeartbeat).values(
        worker=worker, status=status or "idle", current_task=current_task
    )
    changes = {"updated_at": func.now()}
    if status is not None:
        changes |= {"status": stmt.excluded.status, "current_task": stmt.excluded.current_task}
    async with session_factory() as session, session.begin():
        await session.execute(stmt.on_conflict_do_update(index_elements=["worker"], set_=changes))


async def cleanup_heartbeats(
    session_factory: async_sessionmaker[AsyncSession], older_than: timedelta = timedelta(days=1)
) -> int:
    """Строки экземпляров, которые давно не подавали признаков жизни"""
    async with session_factory() as session, session.begin():
        result = await session.execute(
            delete(WorkerHeartbeat).where(WorkerHeartbeat.updated_at < func.now() - older_than)
        )
        return result.rowcount
