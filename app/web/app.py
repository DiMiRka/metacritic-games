"""Фабрика веб-приложения: uvicorn app.web.app:create_app --factory"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from prometheus_client import start_http_server
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.service import rekey_totp_secrets
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.core.db import Database
from app.core.logs import configure_logging
from app.core.sentry import init_sentry
from app.monitoring.metrics import refresh_forever
from app.web.deps import LoginRequiredError, wants_html
from app.web.middleware.csrf import OriginCheckMiddleware
from app.web.middleware.request_id import RequestIdMiddleware
from app.web.middleware.security_headers import SecurityHeadersMiddleware
from app.web.routes import admin, auth, legacy, public
from app.web.templating import WEB_DIR, build_templates, templates_of

HTML_ERRORS = {
    403: ("Нет доступа", "Эта страница доступна только администраторам"),
    429: ("Слишком много запросов", "Подождите немного и обновите страницу"),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Пул соединений живёт ровно столько, сколько приложение"""
    config: Settings = app.state.settings
    # uvicorn настраивает свои логгеры до импорта приложения, перехватываем их здесь
    configure_logging()
    init_sentry("web")
    app.state.db = Database(config.database_url)
    await _rekey_secrets(app.state.db)
    refresher = None
    if config.web_metrics_port:
        # Отдельный порт во внутренней сети: через Caddy он недоступен
        start_http_server(config.web_metrics_port)
        refresher = asyncio.create_task(refresh_forever(app.state.db.session_factory))
        logger.info("метрики web на порту {}", config.web_metrics_port)
    try:
        yield
    finally:
        if refresher:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
        await app.state.db.dispose()


async def _rekey_secrets(db: Database) -> None:
    """Секреты TOTP в открытом виде или под прежним SECRET_KEY перешифровываются текущим"""
    try:
        async with db.session_factory() as session, session.begin():
            changed, failed = await rekey_totp_secrets(session)
        if changed:
            logger.info("секреты TOTP перешифрованы текущим ключом: {}", changed)
        if failed:
            logger.error("секреты TOTP не расшифровать: {}, нужен SECRET_KEY_PREVIOUS", failed)
    except Exception:
        logger.exception("перешифровка секретов TOTP")


async def login_required(request: Request, exc: LoginRequiredError) -> Response:
    return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code in HTML_ERRORS and wants_html(request):
        title, message = HTML_ERRORS[exc.status_code]
        return templates_of(request).TemplateResponse(
            request,
            "error.html",
            {"title": title, "message": message},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or default_settings
    # В проде документация API не публикуется
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if config.is_prod else {}
    app = FastAPI(title="Metacritic Games", version="1", lifespan=lifespan, **docs)
    app.state.settings = config
    app.state.templates = build_templates(config)

    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # Последним, значит внешним: request_id есть и в строках остальных middleware
    app.add_middleware(RequestIdMiddleware)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    # Последним: старые адреса /api/... без версии
    app.include_router(legacy.router)

    app.add_exception_handler(LoginRequiredError, login_required)
    app.add_exception_handler(StarletteHTTPException, http_error)
    return app
