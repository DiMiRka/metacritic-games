"""Проходы обхода: создание, учёт попыток в run_items и завершение"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import User
from app.core.clock import now_tz
from app.core.config import settings
from app.crawler.models import Run, RunItem, RunItemStatus, RunStatus
from app.jobs.pool import Finish
from app.jobs.queue import ClaimedJob


async def create_run(
    session_factory: async_sessionmaker[AsyncSession], trigger: str, requested_by: int | None
) -> int:
    """Проход создаётся отдельной транзакцией, чтобы сбой сбора можно было записать в него"""
    # id пришёл из задачи: несуществующий пользователь превращается в NULL
    requester = (
        select(User.id).where(User.id == requested_by).scalar_subquery()
        if requested_by is not None
        else None
    )
    async with session_factory() as session, session.begin():
        run = Run(trigger=trigger, status=RunStatus.RUNNING, requested_by=requester)
        session.add(run)
        await session.flush()
        return run.id


async def fail_run(session_factory: async_sessionmaker[AsyncSession], run_id: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.RUNNING)
            .values(status=RunStatus.FAILED, finished_at=func.now())
        )


async def done_today(session: AsyncSession, slugs: list[str], day_start: datetime) -> set[str]:
    """Слаги, по которым сегодня уже есть задача scrape_game

    queued и running ещё в работе, done обработаны, dead кончились попытки. Повторы
    внутри дня делает сама очередь
    """
    rows = await session.execute(
        text(
            "SELECT DISTINCT payload->>'slug' FROM jobs "
            "WHERE kind = 'scrape_game' AND created_at >= :day_start "
            "AND status IN ('queued', 'running', 'done', 'dead') "
            "AND payload->>'slug' = ANY(:slugs)"
        ),
        {"day_start": day_start, "slugs": slugs},
    )
    return set(rows.scalars().all())


_FINALIZE = text(
    """
    UPDATE runs SET finished_at = now(), status = CASE
        WHEN EXISTS (SELECT 1 FROM jobs WHERE run_id = :run_id AND status = 'dead')
          OR EXISTS (SELECT 1 FROM run_items WHERE run_id = :run_id AND status = 'failed')
        THEN 'partial' ELSE 'success' END
    WHERE id = :run_id AND status = 'running'
      AND NOT EXISTS (
          SELECT 1 FROM jobs WHERE run_id = :run_id AND status IN ('queued', 'running')
      )
    """
)


async def lock_run(session: AsyncSession, run_id: int) -> None:
    """Выстраивает завершения задач одного прохода в очередь

    Без блокировки две последние задачи, завершаясь одновременно, увидели бы друг друга
    ещё running, и проход не закрылся бы никогда. FOR NO KEY UPDATE не конфликтует с
    KEY SHARE, который берут вставки в run_items и jobs по внешнему ключу, поэтому брать
    блокировку надо до этих вставок, иначе две транзакции ждут друг друга
    """
    await session.execute(select(Run.id).where(Run.id == run_id).with_for_update(key_share=True))


async def finalize_run(session: AsyncSession, run_id: int) -> None:
    """Закрывает проход, если у него не осталось задач в работе"""
    await lock_run(session, run_id)
    await session.execute(_FINALIZE, {"run_id": run_id})


def _failure_status(finish: Finish) -> str:
    return RunItemStatus.RETRYING if finish.status == "queued" else RunItemStatus.FAILED


@dataclass(frozen=True)
class ItemInfo:
    stage: str
    slug: str | None
    game_id: int | None
    success_status: str


async def record_item(
    session: AsyncSession, job: ClaimedJob, finish: Finish, info: ItemInfo
) -> None:
    """Одна строка run_items на попытку, в транзакции завершения задачи"""
    if job.run_id is None:
        return
    await lock_run(session, job.run_id)
    slug = info.slug
    if slug is None and info.game_id is not None:
        slug = await session.scalar(
            text("SELECT slug FROM games WHERE id = :id"), {"id": info.game_id}
        )
    session.add(
        RunItem(
            run_id=job.run_id,
            job_id=job.id,
            slug=slug,
            game_id=info.game_id,
            stage=info.stage,
            status=info.success_status if finish.error is None else _failure_status(finish),
            error=finish.error_text,
            attempt=job.attempts,
            duration_ms=finish.duration_ms,
        )
    )
    await session.flush()
    await finalize_run(session, job.run_id)


@dataclass(frozen=True)
class RunStats:
    processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0


async def run_stats(session: AsyncSession, run_ids: list[int]) -> dict[int, RunStats]:
    """Счётчики прохода из run_items: игра с упавшим резюме всё равно created или updated"""
    if not run_ids:
        return {}
    scrape = RunItem.stage == "scrape"
    terminal = RunItem.status.in_(["created", "updated", "skipped", "failed"])
    rows = await session.execute(
        select(
            RunItem.run_id,
            func.count(func.distinct(RunItem.slug)).filter(scrape, terminal),
            func.count().filter(scrape, RunItem.status == "created"),
            func.count().filter(scrape, RunItem.status == "updated"),
            func.count().filter(scrape, RunItem.status == "skipped"),
            func.count().filter(RunItem.status == "failed"),
        )
        .where(RunItem.run_id.in_(run_ids))
        .group_by(RunItem.run_id)
    )
    return {row[0]: RunStats(*row[1:]) for row in rows}


async def seconds_until_manual_run(session: AsyncSession, now: datetime | None = None) -> int:
    """Сколько ждать до следующего ручного запуска, считая от старта последнего прохода"""
    last_started = await session.scalar(select(func.max(Run.started_at)))
    if last_started is None:
        return 0
    allowed_at = last_started + timedelta(minutes=settings.manual_run_cooldown_minutes)
    return max(0, math.ceil((allowed_at - (now or now_tz())).total_seconds()))
