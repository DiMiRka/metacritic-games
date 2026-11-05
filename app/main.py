from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.security_headers import SecurityHeadersMiddleware
from app.web import admin, auth_routes, public
from app.web.csrf import OriginCheckMiddleware
from app.web.deps import LoginRequiredError, wants_html
from app.web.templating import APP_DIR, templates

# В проде документация API не публикуется
docs = {} if not settings.is_prod else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Metacritic Games", **docs)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

app.include_router(public.router)
app.include_router(auth_routes.router)
app.include_router(admin.router)


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
