"""Ручной запуск обхода: web шлёт NOTIFY, воркер слушает канал через LISTEN"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crawler.runner import CRAWL_LOCK_KEY

log = logging.getLogger(__name__)

CHANNEL = "crawl_requests"
RECONNECT_SECONDS = 5


async def crawl_running(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        free = await conn.scalar(select(func.pg_try_advisory_lock(CRAWL_LOCK_KEY)))
        if free:
            await conn.scalar(select(func.pg_advisory_unlock(CRAWL_LOCK_KEY)))
        await conn.commit()
    return not free


async def request_crawl(session: AsyncSession) -> None:
    await session.execute(select(func.pg_notify(CHANNEL, "manual")))
    await session.commit()


def asyncpg_dsn(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(False)


async def listen_for_requests(
    dsn: str, on_request: Callable[[], None], stop: asyncio.Event
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
        await conn.add_listener(CHANNEL, lambda *_: on_request())
        log.info("LISTEN %s", CHANNEL)
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            if not conn.is_closed():
                await conn.close()
