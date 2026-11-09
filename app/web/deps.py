from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import AuthContext, csrf_valid, load_session, touch_session
from app.core.config import Settings

CSRF_HEADER = "X-CSRF-Token"


def session_cookie(settings: Settings) -> str:
    # __Host- запрещает Domain и требует Secure, в dev на http такой cookie не сохранится
    return "__Host-session" if settings.is_prod else "session"


def login_code_cookie(settings: Settings) -> str:
    # Ожидание кода второго фактора: тот же префикс __Host-, что у сессии
    return "__Host-login-code" if settings.is_prod else "login-code"


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.db.session_factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_session_factory(request)() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class LoginRequiredError(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def session_token(request: Request) -> str | None:
    return request.cookies.get(session_cookie(app_settings(request)))


async def load_auth(
    request: Request, db: Session, factory: SessionFactory, background: BackgroundTasks
) -> AuthContext | None:
    """Текущий пользователь, запоминается в request.state для шаблонов.
    Сессия запроса только читает, отметку активности пишет фоновая задача после ответа
    """
    if not hasattr(request.state, "auth"):
        auth = await load_session(db, session_token(request), config=app_settings(request))
        if auth is not None and auth.touch_due:
            background.add_task(touch_session, factory, auth.session_id)
        request.state.auth = auth
    return request.state.auth


async def require_admin(
    request: Request, auth: Annotated[AuthContext | None, Depends(load_auth)]
) -> AuthContext:
    if auth is None:
        if wants_html(request):
            query = f"?{request.url.query}" if request.url.query else ""
            raise LoginRequiredError(request.url.path + query)
        raise HTTPException(status_code=401, detail="Требуется вход")
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return auth


def check_csrf(request: Request, candidate: str | None) -> None:
    token = session_token(request)
    if not token or not csrf_valid(token, candidate, config=app_settings(request)):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")


async def require_csrf(request: Request, _: Annotated[AuthContext, Depends(require_admin)]) -> None:
    check_csrf(request, request.headers.get(CSRF_HEADER))


Admin = Annotated[AuthContext, Depends(require_admin)]
