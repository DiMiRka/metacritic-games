from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.service import (
    AuthContext,
    SecondFactor,
    authenticate,
    check_second_factor,
    clear_attempts,
    create_session,
    login_delay,
    normalize_login,
    record_failed_attempt,
    revoke_session,
    safe_next,
    too_many_attempts,
)
from app.core.config import settings
from app.web.deps import (
    Session,
    app_settings,
    check_csrf,
    load_auth,
    session_cookie,
    session_token,
)
from app.web.middleware.ratelimit import login_limit
from app.web.templating import templates_of

INVALID_CREDENTIALS = "Неверный логин, пароль или код"

router = APIRouter()
limited = [Depends(login_limit)]


def client_ip(request: Request) -> str:
    # За Caddy uvicorn берёт адрес из X-Forwarded-For, потому что доверяет прокси
    return request.client.host if request.client else "unknown"


def render_login(request: Request, next_url: str, error: str = "", status: int = 200):
    return templates_of(request).TemplateResponse(
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
    code: Annotated[str, Form(max_length=20)] = "",
    next: Annotated[str, Form(max_length=2000)] = "/",
) -> Response:
    target = safe_next(next)
    ip = client_ip(request)
    # Задержка считается и для несуществующих логинов: по ней нельзя узнать, есть ли логин
    if wait := await login_delay(db, login):
        message = f"Много неудачных попыток под этим логином, повторите через {wait} с"
        return render_login(request, target, message, status=429)
    if await too_many_attempts(db, ip, login):
        message = f"Слишком много попыток, попробуйте через {settings.login_window_minutes} мин"
        return render_login(request, target, message, status=429)

    user = await authenticate(db, login, password)
    factor = await check_second_factor(db, user, code) if user else SecondFactor.INVALID
    if factor is SecondFactor.INVALID:
        await record_failed_attempt(db, ip, login)
        await db.commit()
        return render_login(request, target, INVALID_CREDENTIALS, status=401)
    if factor is SecondFactor.SETUP_REQUIRED:
        await db.rollback()
        message = (
            "Администратору нужен второй фактор. Включите его на сервере: "
            f"python -m app users totp {normalize_login(login)}"
        )
        return render_login(request, target, message, status=403)

    await clear_attempts(db, ip, login)
    if old_token := session_token(request):
        await revoke_session(db, old_token)
    token = await create_session(db, user, ip, request.headers.get("user-agent"))
    await db.commit()

    config = app_settings(request)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        session_cookie(config),
        token,
        max_age=config.session_ttl_hours * 3600,
        path="/",
        httponly=True,
        secure=config.is_prod,
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
    config = app_settings(request)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        session_cookie(config), path="/", httponly=True, secure=config.is_prod, samesite="lax"
    )
    return response
