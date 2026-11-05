from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import AuthContext, csrf_valid, load_session
from app.core.config import settings
from app.core.db import get_session, get_session_factory

# __Host- запрещает Domain и требует Secure, в dev на http такой cookie не сохранится
SESSION_COOKIE = "__Host-session" if settings.is_prod else "session"
CSRF_HEADER = "X-CSRF-Token"

Session = Annotated[AsyncSession, Depends(get_session)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class LoginRequiredError(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


async def load_auth(request: Request, db: Session) -> AuthContext | None:
    """Текущий пользователь, запоминается в request.state для шаблонов"""
    if not hasattr(request.state, "auth"):
        request.state.auth = await load_session(db, session_token(request))
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
    if not token or not csrf_valid(token, candidate):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")


async def require_csrf(request: Request, _: Annotated[AuthContext, Depends(require_admin)]) -> None:
    check_csrf(request, request.headers.get(CSRF_HEADER))


Admin = Annotated[AuthContext, Depends(require_admin)]
