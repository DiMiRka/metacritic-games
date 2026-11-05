"""Ручной запуск обхода: web шлёт NOTIFY, воркер слушает канал через LISTEN"""

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crawler.models import Run

log = logging.getLogger(__name__)

CHANNEL = "crawl_requests"
RECONNECT_SECONDS = 5


async def seconds_until_manual_run(session: AsyncSession, now: datetime | None = None) -> int:
    """Сколько ждать до следующего ручного запуска, считая от старта последнего прохода"""
    last_started = await session.scalar(select(func.max(Run.started_at)))
    if last_started is None:
        return 0
    allowed_at = last_started + timedelta(minutes=settings.manual_run_cooldown_minutes)
    wait = (allowed_at - (now or datetime.now(settings.tz))).total_seconds()
    return max(0, math.ceil(wait))


async def request_crawl(session: AsyncSession, user_id: int) -> None:
    await session.execute(select(func.pg_notify(CHANNEL, str(user_id))))
    await session.commit()


def requester_from_payload(payload: str) -> int | None:
    """id пользователя из NOTIFY, мусор превращается в None"""
    return int(payload) if payload.isdigit() else None


def asyncpg_dsn(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(False)


async def listen_for_requests(
    dsn: str, on_request: Callable[[str], None], stop: asyncio.Event
) -> None:
    """Держит LISTEN и переподключается, если соединение с базой оборвалось"""
    while not stop.is_set():
        try:
            conn = await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning(
                "LISTEN %s: нет соединения (%s), повтор через %ss", CHANNEL, exc, RECONNECT_SECONDS
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), RECONNECT_SECONDS)
            continue

        lost = asyncio.Event()
        conn.add_termination_listener(lambda _conn, lost=lost: lost.set())
        await conn.add_listener(CHANNEL, lambda _conn, _pid, _channel, payload: on_request(payload))
        log.info("LISTEN %s", CHANNEL)
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            if not conn.is_closed():
                await conn.close()
