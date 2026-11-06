import asyncio

from httpx import ASGITransport, AsyncClient
from loguru import logger
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.web.middleware.request_id import RequestIdMiddleware


async def stream(request):
    async def body():
        for n in range(3):
            # Пишется во время отправки тела: так ведёт себя SSE монитора
            logger.info("кусок {}", n)
            await asyncio.sleep(0)
            yield f"data: {n}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def client() -> AsyncClient:
    app = RequestIdMiddleware(Starlette(routes=[Route("/stream", stream)]))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_request_id_kept_while_streaming(log_records) -> None:
    async with client() as http:
        response = await http.get("/stream")

    request_id = response.headers["X-Request-ID"]
    chunks = [r for r in log_records if r["message"].startswith("кусок")]
    assert len(chunks) == 3
    assert {r["extra"].get("request_id") for r in chunks} == {request_id}


async def test_incoming_request_id_accepted_when_valid() -> None:
    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    async with client() as http:
        from_caddy = await http.get("/stream", headers={"X-Request-ID": uuid})
        injected = await http.get("/stream", headers={"X-Request-ID": "bad id\nFAKE LOG"})
        short = await http.get("/stream", headers={"X-Request-ID": "x"})

    assert from_caddy.headers["X-Request-ID"] == uuid
    for response in (injected, short):
        generated = response.headers["X-Request-ID"]
        assert len(generated) == 12 and generated.isalnum()


async def test_app_sets_request_id_on_every_response(make_client) -> None:
    async with make_client() as http:
        page = await http.get("/")
        missing = await http.get("/nope")
    assert len(page.headers["X-Request-ID"]) == 12
    assert len(missing.headers["X-Request-ID"]) == 12
