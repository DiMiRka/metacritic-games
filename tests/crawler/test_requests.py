import asyncio

import asyncpg
import pytest

from app.crawler.requests import CHANNEL, asyncpg_dsn, listen_for_requests, requester_from_payload


@pytest.fixture
def dsn(db_engine) -> str:
    return asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))


async def test_worker_listener_receives_requests(dsn) -> None:
    payloads: list[str] = []
    stop = asyncio.Event()

    def on_request(payload: str) -> None:
        payloads.append(payload)

    listener = asyncio.create_task(listen_for_requests(dsn, on_request, stop))
    conn = await asyncpg.connect(dsn)
    try:
        for _ in range(50):
            await conn.execute(f"NOTIFY {CHANNEL}, '42'")
            await asyncio.sleep(0.1)
            if payloads:
                break
    finally:
        await conn.close()
        stop.set()
        await asyncio.wait_for(listener, 5)
    assert payloads[0] == "42"


@pytest.mark.parametrize(
    ("payload", "expected"), [("7", 7), ("manual", None), ("-1", None), ("", None)]
)
def test_requester_from_payload(payload, expected) -> None:
    assert requester_from_payload(payload) == expected
