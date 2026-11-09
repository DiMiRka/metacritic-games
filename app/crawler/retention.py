"""Очистка старых служебных данных: задачи, проходы с их записями, сырые ответы,
просроченные сессии и старые попытки входа.

Удаление пачками, каждая в своей транзакции: длинных блокировок нет. Каталог, резюме и история
оценок не трогаются: это данные сервиса, история хранит только изменения и растёт медленно
"""

from dataclasses import dataclass
from datetime import timedelta

from loguru import logger
from sqlalchemy import Select, delete, exists, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.crawler.models import CrawlState, RawPage, Run, RunStatus
from app.jobs.models import ACTIVE_STATUSES, Job

CHUNK = 5000


@dataclass
class CleanupReport:
    jobs: int = 0
    runs: int = 0
    crawl_states: int = 0
    raw_pages: int = 0
    sessions: int = 0
    login_attempts: int = 0
    login_challenges: int = 0


async def _delete_chunked(
    session_factory: async_sessionmaker[AsyncSession], model, ids: Select
) -> int:
    total = 0
    while True:
        async with session_factory() as session, session.begin():
            result = await session.execute(
                delete(model).where(model.id.in_(ids.limit(CHUNK).scalar_subquery()))
            )
        total += result.rowcount
        if result.rowcount < CHUNK:
            return total


def _stale_raw_pages(cutoff) -> Select:
    """Старые версии сырых ответов; последняя по каждой игре остаётся всегда, для reparse"""
    newer = aliased(RawPage)
    has_newer = exists().where(
        newer.game_id == RawPage.game_id,
        tuple_(newer.last_seen_at, newer.id) > tuple_(RawPage.last_seen_at, RawPage.id),
    )
    return select(RawPage.id).where(RawPage.last_seen_at < cutoff, has_newer)


async def cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int | None = None,
    raw_retention_days: int | None = None,
) -> CleanupReport:
    """0 дней: соответствующая очистка выключена"""
    days = settings.retention_days if retention_days is None else retention_days
    raw_days = settings.raw_retention_days if raw_retention_days is None else raw_retention_days
    now = func.now()
    report = CleanupReport()

    if days > 0:
        cutoff = now - timedelta(days=days)
        report.jobs = await _delete_chunked(
            session_factory,
            Job,
            select(Job.id).where(
                Job.status.not_in(ACTIVE_STATUSES),
                func.coalesce(Job.finished_at, Job.created_at) < cutoff,
            ),
        )
        # run_items удаляются каскадом, у оставшихся задач run_id обнуляется
        report.runs = await _delete_chunked(
            session_factory,
            Run,
            select(Run.id).where(
                Run.status != RunStatus.RUNNING,
                func.coalesce(Run.finished_at, Run.started_at) < cutoff,
            ),
        )
        async with session_factory() as session, session.begin():
            result = await session.execute(
                delete(CrawlState).where(CrawlState.day < func.current_date() - days)
            )
            report.crawl_states = result.rowcount

    # Прав на sessions и login_attempts у воркера нет, только на эту функцию
    async with session_factory() as session, session.begin():
        purged = (await session.execute(select(text("* FROM purge_auth_records()")))).one()
    report.sessions, report.login_attempts, report.login_challenges = purged

    if raw_days > 0:
        report.raw_pages = await _delete_chunked(
            session_factory, RawPage, _stale_raw_pages(now - timedelta(days=raw_days))
        )

    logger.info("очистка: {}", report)
    return report
