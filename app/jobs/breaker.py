"""Circuit breaker в базе: при массовом сбое внешнего сервиса задачи откладываются

closed: всё работает. open: задачи откладываются до opened_until без траты попыток.
half_open: после открытия одна пробная задача; успех закрывает автомат, сбой открывает снова
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.models import Breaker, BreakerState


class Verdict(StrEnum):
    SUCCESS = "success"  # сервис ответил: сбрасывает счётчик подряд идущих ошибок
    FAILURE = "failure"  # сбой сервиса, считается к порогу
    TRIP = "trip"  # открыть сразу: кончилась квота, неверный ключ
    NEUTRAL = "neutral"  # ничего не говорит о сервисе


@dataclass(frozen=True)
class BreakerPolicy:
    name: str
    threshold: Callable[[], int]
    open_seconds: Callable[[], float]
    probe_seconds: Callable[[], float]
    judge: Callable[[BaseException | None], Verdict]


async def _locked(session: AsyncSession, name: str) -> tuple[Breaker, datetime]:
    await session.execute(insert(Breaker).values(name=name).on_conflict_do_nothing())
    row = (
        await session.execute(
            select(Breaker, func.now()).where(Breaker.name == name).with_for_update()
        )
    ).one()
    return row[0], row[1]


async def gate(
    session_factory: async_sessionmaker[AsyncSession], policy: BreakerPolicy
) -> datetime | None:
    """None, если задачу можно выполнять; иначе время, до которого её отложить"""
    async with session_factory() as session, session.begin():
        breaker, now = await _locked(session, policy.name)
        if breaker.state == BreakerState.CLOSED:
            return None
        if breaker.opened_until and breaker.opened_until > now:
            return breaker.opened_until
        # Открытие истекло или пробная задача не вернулась: эта задача становится пробной
        breaker.state = BreakerState.HALF_OPEN
        breaker.opened_until = now + timedelta(seconds=policy.probe_seconds())
        return None


async def record(
    session_factory: async_sessionmaker[AsyncSession],
    policy: BreakerPolicy,
    verdict: Verdict,
    until: datetime | None = None,
) -> None:
    """until: сбой известен до этого момента (сброс квоты), автомат открывается не короче"""
    if verdict == Verdict.NEUTRAL:
        return
    async with session_factory() as session, session.begin():
        if verdict == Verdict.SUCCESS:
            # Без блокировки и записи, если автомат и так закрыт с нулевым счётчиком
            await session.execute(
                update(Breaker)
                .where(
                    Breaker.name == policy.name,
                    (Breaker.state != BreakerState.CLOSED) | (Breaker.failures != 0),
                )
                .values(state=BreakerState.CLOSED, failures=0, opened_until=None)
            )
            return
        breaker, now = await _locked(session, policy.name)
        breaker.failures += 1
        should_open = (
            verdict == Verdict.TRIP
            or breaker.state == BreakerState.HALF_OPEN
            or breaker.failures >= policy.threshold()
        )
        if should_open:
            breaker.state = BreakerState.OPEN
            opened_until = now + timedelta(seconds=policy.open_seconds())
            # Квота сбрасывается раз в сутки: пробы каждые полчаса до сброса бессмысленны
            breaker.opened_until = max(opened_until, until) if until else opened_until
