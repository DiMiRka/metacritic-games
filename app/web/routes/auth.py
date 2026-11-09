"""Вход в два шага.

1. POST /login — логин и пароль. Viewer и администратор без обязательного второго фактора
   входят сразу. Администратору с TOTP сессия не создаётся: появляется одноразовое ожидание
   кода (login_challenges), его токен уходит в отдельной cookie, и редирект ведёт на /login/code.
2. POST /login/code — код TOTP или резервный код. Не больше LOGIN_CODE_MAX_ATTEMPTS неверных
   кодов на одно ожидание, после этого снова нужен пароль. Каждый неверный код считается
   неудачной попыткой входа, как неверный пароль: задержка по логину и блокировка по IP
   действуют и на этом шаге
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import LoginChallenge, User
from app.auth.service import (
    AuthContext,
    SecondFactor,
    authenticate,
    clear_attempts,
    create_login_challenge,
    create_session,
    delete_login_challenge,
    load_login_challenge,
    login_delay,
    normalize_login,
    record_failed_attempt,
    revoke_session,
    safe_next,
    second_factor_step,
    too_many_attempts,
    verify_second_factor,
)
from app.core.config import Settings
from app.web.deps import (
    Session,
    app_settings,
    check_csrf,
    load_auth,
    login_code_cookie,
    session_cookie,
    session_token,
)
from app.web.middleware.ratelimit import login_limit
from app.web.templating import templates_of

INVALID_CREDENTIALS = "Неверный логин или пароль"
INVALID_CODE = "Неверный код"
# Почему второй шаг закончился: ?reason= на странице входа
REASONS = {
    "expired": "Время на ввод кода истекло, войдите заново",
    "attempts": "Слишком много неверных кодов, войдите заново",
}

router = APIRouter()
limited = [Depends(login_limit)]


def client_ip(request: Request) -> str:
    # За Caddy uvicorn берёт адрес из X-Forwarded-For, потому что доверяет прокси
    return request.client.host if request.client else "unknown"


def render_login(request: Request, next_url: str, error: str = "", status: int = 200):
    return templates_of(request).TemplateResponse(
        request, "login.html", {"next": next_url, "error": error}, status_code=status
    )


def render_code(request: Request, error: str = "", status: int = 200):
    return templates_of(request).TemplateResponse(
        request, "login_code.html", {"error": error}, status_code=status
    )


def _set_code_cookie(response: Response, config: Settings, token: str) -> None:
    response.set_cookie(
        login_code_cookie(config),
        token,
        max_age=config.login_code_ttl_seconds,
        path="/",
        httponly=True,
        secure=config.is_prod,
        samesite="strict",
    )


def _drop_code_cookie(response: Response, config: Settings) -> None:
    response.delete_cookie(
        login_code_cookie(config), path="/", httponly=True, secure=config.is_prod, samesite="strict"
    )


def back_to_login(config: Settings, reason: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?reason={reason}", status_code=303)
    _drop_code_cookie(response, config)
    return response


async def too_fast(db: AsyncSession, ip: str, login: str, config: Settings) -> str | None:
    """Задержка по логину и блокировка по паре IP и логин: на обоих шагах одинаково.
    Задержка считается и для несуществующих логинов, по ней нельзя узнать, есть ли логин"""
    if wait := await login_delay(db, login, config=config):
        return f"Много неудачных попыток под этим логином, повторите через {wait} с"
    if await too_many_attempts(db, ip, login, config=config):
        return f"Слишком много попыток, попробуйте через {config.login_window_minutes} мин"
    return None


async def complete_login(
    request: Request, db: AsyncSession, user: User, target: str, config: Settings
) -> RedirectResponse:
    """Полный вход: только здесь сбрасываются попытки, отзывается старая сессия,
    обновляется last_login_at и ставится cookie сессии"""
    ip = client_ip(request)
    await clear_attempts(db, ip, user.login)
    if old_token := session_token(request):
        await revoke_session(db, old_token)
    token = await create_session(db, user, ip, request.headers.get("user-agent"), config=config)
    await db.commit()

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
    _drop_code_cookie(response, config)
    return response


@router.get("/login", response_class=HTMLResponse, dependencies=limited)
async def login_page(
    request: Request,
    auth: Annotated[AuthContext | None, Depends(load_auth)],
    next: str = "/",
    reason: str = "",
) -> Response:
    target = safe_next(next)
    if auth is not None:
        return RedirectResponse(target, status_code=303)
    return render_login(request, target, REASONS.get(reason, ""))


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
    config = app_settings(request)
    if message := await too_fast(db, ip, login, config):
        return render_login(request, target, message, status=429)

    user = await authenticate(db, login, password)
    if user is None:
        await record_failed_attempt(db, ip, login, config=config)
        await db.commit()
        return render_login(request, target, INVALID_CREDENTIALS, status=401)

    step = second_factor_step(user, config=config)
    if step is SecondFactor.SETUP_REQUIRED:
        await db.rollback()
        message = (
            "Администратору нужен второй фактор. Включите его на сервере: "
            f"python -m app users totp {normalize_login(login)}"
        )
        return render_login(request, target, message, status=403)
    if step is SecondFactor.OK:
        return await complete_login(request, db, user, target, config)

    # Пароль администратора верен, но сессии ещё нет: ждём код
    token = await create_login_challenge(db, user, target, ip, config=config)
    await db.commit()
    response = RedirectResponse("/login/code", status_code=303)
    _set_code_cookie(response, config, token)
    return response


async def _challenge(request: Request, db: AsyncSession) -> LoginChallenge | None:
    token = request.cookies.get(login_code_cookie(app_settings(request)))
    return await load_login_challenge(db, token)


@router.get("/login/code", response_class=HTMLResponse, dependencies=limited)
async def login_code_page(request: Request, db: Session) -> Response:
    challenge = await _challenge(request, db)
    await db.rollback()
    if challenge is None:
        return back_to_login(app_settings(request), "expired")
    return render_code(request)


@router.post("/login/code", response_class=HTMLResponse, dependencies=limited)
async def login_code(
    request: Request,
    db: Session,
    code: Annotated[str, Form(max_length=40)] = "",
) -> Response:
    config = app_settings(request)
    challenge = await _challenge(request, db)
    if challenge is None:
        await db.rollback()
        return back_to_login(config, "expired")
    user = await db.get(User, challenge.user_id)
    if user is None or not user.is_active:
        await delete_login_challenge(db, challenge)
        await db.commit()
        return back_to_login(config, "expired")

    ip = client_ip(request)
    if message := await too_fast(db, ip, user.login, config):
        await db.rollback()
        return render_code(request, message, status=429)

    if await verify_second_factor(db, user, code, config=config):
        target = challenge.next_url
        await delete_login_challenge(db, challenge)
        return await complete_login(request, db, user, target, config)

    await record_failed_attempt(db, ip, user.login, config=config)
    challenge.attempts += 1
    if challenge.attempts >= config.login_code_max_attempts:
        await delete_login_challenge(db, challenge)
        await db.commit()
        # Пароль был верным, а код подобрать не вышло: это тревожнее неверного пароля
        logger.error(
            "исчерпаны попытки кода для {}, пароль, вероятно, известен постороннему (IP {})",
            user.login,
            ip,
        )
        return back_to_login(config, "attempts")
    await db.commit()
    left = config.login_code_max_attempts - challenge.attempts
    return render_code(request, f"{INVALID_CODE}, осталось попыток: {left}", status=401)


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
