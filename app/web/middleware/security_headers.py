from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.urls import IMAGE_HOSTS, VIDEO_HOSTS


def _hosts(hosts: frozenset[str]) -> str:
    return " ".join(f"https://{h}" for h in sorted(hosts))


CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        f"img-src 'self' {_hosts(IMAGE_HOSTS)}",
        f"frame-src {_hosts(VIDEO_HOSTS)}",
        "connect-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ]
)

# Swagger UI и ReDoc грузят скрипты с CDN и держат инлайновый код, им нужна своя политика
DOCS_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
        "worker-src blob:",
        "connect-src 'self'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ]
)
DOCS_PATHS = ("/docs", "/redoc")

BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityHeadersMiddleware:
    """CSP и защитные заголовки на каждый HTTP-ответ, включая потоковые"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = DOCS_CSP if scope["path"].startswith(DOCS_PATHS) else CSP

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {b"content-security-policy", *dict(BASE_HEADERS)}
                ]
                headers.append((b"content-security-policy", csp.encode()))
                headers.extend(BASE_HEADERS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
