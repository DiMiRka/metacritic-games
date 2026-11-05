import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


class OriginCheckMiddleware:
    """Небезопасные запросы принимаются только с собственного адреса сайта

    Сверяется Origin, а если его нет, то Referer. Свой адрес берётся из схемы и Host
    запроса: за Caddy схему передаёт X-Forwarded-Proto, которому uvicorn доверяет
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        own = f"{scope['scheme']}://{headers.get('host', '')}".lower()
        source = headers.get("origin") or headers.get("referer")
        if source and _origin_of(source) == own:
            await self.app(scope, receive, send)
            return

        body = json.dumps({"detail": "Запрос с чужого адреса отклонён"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
