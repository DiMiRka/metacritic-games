import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logs import configure_logging, log_context
from app.core.sentry import init_sentry
from app.web.deps import LoginRequiredError, wants_html
from app.web.middleware.csrf import OriginCheckMiddleware
from app.web.middleware.security_headers import SecurityHeadersMiddleware
from app.web.routes import admin, auth, metrics, public
from app.web.templating import WEB_DIR, templates

# В проде документация API не публикуется
docs = {} if not settings.is_prod else {"docs_url": None, "redoc_url": None, "openapi_url": None}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # uvicorn настраивает свои логгеры до импорта приложения, перехватываем их здесь
    configure_logging()
    init_sentry("web")
    yield


app = FastAPI(title="Metacritic Games", lifespan=lifespan, **docs)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(metrics.router)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """request_id в каждой строке лога запроса и в заголовке ответа"""
    request_id = secrets.token_hex(6)
    with log_context(request_id=request_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(LoginRequiredError)
async def login_required(request: Request, exc: LoginRequiredError) -> Response:
    return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)


HTML_ERRORS = {
    403: ("Нет доступа", "Эта страница доступна только администраторам"),
    429: ("Слишком много запросов", "Подождите немного и обновите страницу"),
}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code in HTML_ERRORS and wants_html(request):
        title, message = HTML_ERRORS[exc.status_code]
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": title, "message": message},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)
