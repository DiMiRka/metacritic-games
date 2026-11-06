import time
from datetime import UTC, datetime

import httpx
import pytest

from app.scraper.client import MetacriticClient, parse_retry_after


def make_client(handler, **kwargs) -> MetacriticClient:
    kwargs.setdefault("delay", 0)
    kwargs.setdefault("backoff", 0)
    return MetacriticClient(transport=httpx.MockTransport(handler), **kwargs)


def sequence(*responses: httpx.Response):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]

    return handler, calls


async def test_retry_on_429_with_retry_after() -> None:
    handler, calls = sequence(
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": True}),
    )
    async with make_client(handler) as client:
        assert await client.get_json("/x") == {"ok": True}
    assert len(calls) == 2


async def test_retry_on_5xx() -> None:
    handler, calls = sequence(
        httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})
    )
    async with make_client(handler) as client:
        assert await client.get_json("/x") == {"ok": 1}
    assert len(calls) == 3


async def test_gives_up_after_max_retries() -> None:
    handler, calls = sequence(httpx.Response(500))
    async with make_client(handler, max_retries=2) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("/x")
    assert len(calls) == 3


async def test_no_retry_on_404() -> None:
    handler, calls = sequence(httpx.Response(404))
    async with make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("/x")
    assert len(calls) == 1


async def test_retry_on_transport_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={})

    async with make_client(handler) as client:
        assert await client.get_json("/x") == {}
    assert calls == 2


async def test_throttle_spaces_requests() -> None:
    handler, _ = sequence(httpx.Response(200, json={}))
    async with make_client(handler, delay=0.05) as client:
        started = time.monotonic()
        for _ in range(3):
            await client.get_json("/x")
    assert time.monotonic() - started >= 0.1


async def test_critic_reviews_paginate_by_ten() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        items = [{"quote": f"r{offset + i}"} for i in range(min(10, 25 - offset))]
        return httpx.Response(200, json={"data": {"totalResults": 25, "items": items}})

    async with make_client(handler) as client:
        items = await client.critic_reviews("game", limit=30)
    assert offsets == [0, 10, 20]
    assert len(items) == 25


async def test_browse_offsets() -> None:
    handler, calls = sequence(httpx.Response(200, json={}))
    async with make_client(handler) as client:
        await client.browse(3)
    params = calls[0].url.params
    assert params["offset"] == "48"
    assert params["limit"] == "24"
    assert params["mcoTypeId"] == "13"
    assert params["sortBy"] == "-releaseDate"


def test_parse_retry_after() -> None:
    now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("5") == 5
    assert parse_retry_after("9999") == 120
    assert parse_retry_after("Thu, 05 Nov 2026 12:00:30 GMT", now) == 30
    assert parse_retry_after("Thu, 05 Nov 2026 11:00:00 GMT", now) == 0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


async def test_requests_counted_by_status() -> None:
    from prometheus_client import REGISTRY

    def value(status: str) -> float:
        return REGISTRY.get_sample_value("metacritic_requests_total", {"status": status}) or 0

    before = {s: value(s) for s in ("429", "200")}
    handler, _ = sequence(
        httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={})
    )
    async with make_client(handler) as client:
        await client.get_json("/x")

    assert value("429") == before["429"] + 1
    assert value("200") == before["200"] + 1
