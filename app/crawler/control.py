"""Управление обходом с сайта: без зависимостей этапов, LLM и клиента Metacritic"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.models import ACTIVE_STATUSES, Job, JobKind


async def request_manual_collect(session: AsyncSession, user_id: int) -> int | None:
    """Ручной запуск с сайта через функцию request_manual_collect: прав на запись в jobs
    у сайта нет. None, если collect уже в очереди или не прошла пауза между запусками
    """
    return await session.scalar(select(func.request_manual_collect(user_id)))


async def collect_active(session: AsyncSession) -> bool:
    """collect уже в очереди или выполняется"""
    count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == JobKind.COLLECT, Job.status.in_(ACTIVE_STATUSES))
    )
    return bool(count)
