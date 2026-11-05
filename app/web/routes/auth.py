from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.service import (
    AuthContext,
    authenticate,
    clear_attempts,
    create_session,
    record_failed_attempt,
    revoke_session,
    safe_next,
    too_many_attempts,
)
from app.core.config import settings
from app.web.deps import SESSION_COOKIE, Session, check_csrf, load_auth, session_token
from app.web.middleware.ratelimit import login_limit
from app.web.templating import templates

INVALID_CREDENTIALS = "Неверный логин или пароль"

router = APIRouter()
limited = [Depends(login_limit)]


def client_ip(request: Request) -> str:
    # За Caddy uvicorn берёт адрес из X-Forwarded-For, потому что доверяет прокси
    return request.client.host if request.client else "unknown"


def render_login(request: Request, next_url: str, error: str = "", status: int = 200):
    return templates.TemplateResponse(
        request, "login.html", {"next": next_url, "error": error}, status_code=status
    )


@router.get("/login", response_class=HTMLResponse, dependencies=limited)
async def login_page(
    request: Request,
    auth: Annotated[AuthContext | None, Depends(load_auth)],
    next: str = "/",
) -> Response:
    target = safe_next(next)
    if auth is not None:
        return RedirectResponse(target, status_code=303)
    return render_login(request, target)


@router.post("/login", response_class=HTMLResponse, dependencies=limited)
async def login(
    request: Request,
    db: Session,
    login: Annotated[str, Form(max_length=200)] = "",
    password: Annotated[str, Form(max_length=1000)] = "",
    next: Annotated[str, Form(max_length=2000)] = "/",
) -> Response:
    target = safe_next(next)
    ip = client_ip(request)
    if await too_many_attempts(db, ip, login):
        message = f"Слишком много попыток, попробуйте через {settings.login_window_minutes} мин"
        return render_login(request, target, message, status=429)

    user = await authenticate(db, login, password)
    if user is None:
        await record_failed_attempt(db, ip, login)
        await db.commit()
        return render_login(request, target, INVALID_CREDENTIALS, status=401)

    await clear_attempts(db, ip, login)
    if old_token := session_token(request):
        await revoke_session(db, old_token)
    token = await create_session(db, user, ip, request.headers.get("user-agent"))
    await db.commit()

    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        httponly=True,
        secure=settings.is_prod,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    db: Session,
    csrf_token: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    if token := session_token(request):
        check_csrf(request, csrf_token or request.headers.get("X-CSRF-Token"))
        await revoke_session(db, token)
        await db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=settings.is_prod, samesite="lax"
    )
    return response
