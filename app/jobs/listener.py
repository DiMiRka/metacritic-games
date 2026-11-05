import asyncio
import contextlib
import logging
from collections.abc import Callable

import asyncpg

log = logging.getLogger(__name__)

RECONNECT_SECONDS = 5


async def listen(
    dsn: str, channel: str, on_notify: Callable[[str], None], stop: asyncio.Event
) -> None:
    """Держит LISTEN и переподключается, если соединение с базой оборвалось

    NOTIFY только ускоряет реакцию: пропущенное уведомление подхватит опрос очереди
    """
    while not stop.is_set():
        try:
            conn = await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("LISTEN %s: нет соединения (%s)", channel, exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), RECONNECT_SECONDS)
            continue

        lost = asyncio.Event()
        conn.add_termination_listener(lambda _conn, lost=lost: lost.set())
        await conn.add_listener(channel, lambda _conn, _pid, _ch, payload: on_notify(payload))
        log.info("LISTEN %s", channel)
        # После переподключения могли пропасть уведомления, будим всех сразу
        on_notify("")
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            if not conn.is_closed():
                await conn.close()
