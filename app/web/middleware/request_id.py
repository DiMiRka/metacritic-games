"""request_id в каждой строке лога запроса и в заголовке ответа X-Request-ID.

Чистый ASGI, а не BaseHTTPMiddleware: контекст держится, пока отправляется всё тело ответа,
включая потоковый SSE монитора, и не теряется в фоновых задачах. Значение из входящего
X-Request-ID принимается: его ставит Caddy, и тот же id есть в журнале Caddy
"""

import re
import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logs import log_context

HEADER = "x-request-id"
# Только безопасные символы и разумная длина: значение попадает в логи
VALID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def _incoming(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == HEADER.encode():
            text = value.decode("latin-1")
            return text if VALID.fullmatch(text) else None
    return None


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _incoming(scope) or secrets.token_hex(6)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        with log_context(request_id=request_id):
            await self.app(scope, receive, send_with_id)
