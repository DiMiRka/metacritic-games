from datetime import timedelta

from sqlalchemy import func, update

from app.jobs.breaker import BreakerPolicy, Verdict, gate, record
from app.jobs.models import Breaker


def policy(threshold: int = 3) -> BreakerPolicy:
    return BreakerPolicy(
        "svc",
        threshold=lambda: threshold,
        open_seconds=lambda: 600,
        probe_seconds=lambda: 60,
        judge=lambda error: Verdict.FAILURE if error else Verdict.SUCCESS,
    )


async def state(session_factory) -> tuple[str, int]:
    async with session_factory() as session:
        breaker = await session.get(Breaker, "svc")
        return (breaker.state, breaker.failures) if breaker else ("closed", 0)


async def expire(session_factory) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Breaker).values(opened_until=func.now() - timedelta(seconds=1))
        )


async def test_opens_after_consecutive_failures(session_factory) -> None:
    p = policy(threshold=3)
    for verdict in (Verdict.FAILURE, Verdict.FAILURE, Verdict.SUCCESS):
        await record(session_factory, p, verdict)
    assert await state(session_factory) == ("closed", 0)
    assert await gate(session_factory, p) is None

    for _ in range(3):
        await record(session_factory, p, Verdict.FAILURE)
    assert (await state(session_factory))[0] == "open"
    assert await gate(session_factory, p) is not None


async def test_half_open_probe_closes_on_success(session_factory) -> None:
    p = policy(threshold=1)
    await record(session_factory, p, Verdict.FAILURE)
    await expire(session_factory)

    assert await gate(session_factory, p) is None
    assert (await state(session_factory))[0] == "half_open"
    # Пробная задача в работе, остальные ждут
    assert await gate(session_factory, p) is not None

    await record(session_factory, p, Verdict.SUCCESS)
    assert await state(session_factory) == ("closed", 0)
    assert await gate(session_factory, p) is None


async def test_failed_probe_reopens(session_factory) -> None:
    p = policy(threshold=5)
    await record(session_factory, p, Verdict.TRIP)
    assert (await state(session_factory))[0] == "open"
    await expire(session_factory)
    assert await gate(session_factory, p) is None

    await record(session_factory, p, Verdict.FAILURE)
    assert (await state(session_factory))[0] == "open"


async def test_lost_probe_lets_next_job_probe(session_factory) -> None:
    p = policy(threshold=1)
    await record(session_factory, p, Verdict.FAILURE)
    await expire(session_factory)
    assert await gate(session_factory, p) is None
    await expire(session_factory)
    assert await gate(session_factory, p) is None


async def test_neutral_changes_nothing(session_factory) -> None:
    p = policy(threshold=2)
    await record(session_factory, p, Verdict.FAILURE)
    await record(session_factory, p, Verdict.NEUTRAL)
    assert await state(session_factory) == ("closed", 1)


async def test_trip_with_until_opens_until_that_moment(session_factory) -> None:
    """Квота сбрасывается раз в сутки: автомат открыт до сброса, а не на open_seconds"""
    from datetime import UTC, datetime

    from app.jobs.models import Breaker

    reset = datetime.now(UTC) + timedelta(hours=5)
    await record(session_factory, policy(), Verdict.TRIP, until=reset)
    async with session_factory() as session:
        breaker = await session.get(Breaker, "svc")
    assert breaker.state == "open"
    assert abs((breaker.opened_until - reset).total_seconds()) < 1
    assert await gate(session_factory, policy()) == breaker.opened_until

    # Короткий until не сокращает обычное время открытия
    soon = datetime.now(UTC) + timedelta(seconds=5)
    await record(session_factory, policy(), Verdict.TRIP, until=soon)
    async with session_factory() as session:
        breaker = await session.get(Breaker, "svc")
    assert breaker.opened_until > soon + timedelta(seconds=500)
