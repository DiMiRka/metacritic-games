"""Пароли, серверные сессии, счётчик попыток входа и CSRF-токен"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import LoginAttempt, User, UserRole, UserSession
from app.core.clock import now_tz
from app.core.config import settings

MIN_PASSWORD_LENGTH = 12
LOGIN_MAX_LENGTH = 100
SESSION_TOUCH_SECONDS = 60

_hasher = PasswordHasher()
# Хеш-заглушка: если логина нет, всё равно считаем argon2, чтобы время ответа не выдавало его
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class AuthContext:
    user_id: int
    login: str
    role: str
    session_id: int
    csrf_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


def normalize_login(login: str) -> str:
    return login.strip().lower()[:LOGIN_MAX_LENGTH]


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль короче {MIN_PASSWORD_LENGTH} символов")
    return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for(session_token: str) -> str:
    """CSRF-токен выводится из токена сессии: без cookie его не получить, хранить не нужно"""
    return hashlib.sha256(b"csrf:" + session_token.encode()).hexdigest()


def csrf_valid(session_token: str, candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(csrf_token_for(session_token), candidate)


def safe_next(value: str | None, default: str = "/") -> str:
    """Только относительный путь внутри сайта, иначе получится открытый редирект"""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    if any(ch == "\\" or ord(ch) < 0x20 for ch in value):
        return default
    return value


async def authenticate(db: AsyncSession, login: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.login == normalize_login(login)))
    valid = _verify(user.password_hash if user else _DUMMY_HASH, password)
    if not (user and valid and user.is_active):
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return user


async def too_many_attempts(db: AsyncSession, ip: str, login: str) -> bool:
    since = now_tz() - timedelta(minutes=settings.login_window_minutes)
    count = await db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.ip == ip,
            LoginAttempt.login == normalize_login(login),
            LoginAttempt.attempted_at >= since,
        )
    )
    return count >= settings.login_max_attempts


async def record_failed_attempt(db: AsyncSession, ip: str, login: str) -> None:
    since = now_tz() - timedelta(minutes=settings.login_window_minutes)
    await db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < since))
    db.add(LoginAttempt(ip=ip, login=normalize_login(login)))


async def clear_attempts(db: AsyncSession, ip: str, login: str) -> None:
    await db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.ip == ip, LoginAttempt.login == normalize_login(login)
        )
    )


async def create_session(
    db: AsyncSession, user: User, ip: str | None, user_agent: str | None
) -> str:
    """Новый токен на каждый вход, в базу пишется только его хеш"""
    token = secrets.token_urlsafe(32)
    now = now_tz()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            last_seen_at=now,
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    user.last_login_at = now
    return token


async def revoke_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))


async def revoke_user_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(UserSession).where(UserSession.user_id == user_id))


async def load_session(db: AsyncSession, token: str | None) -> AuthContext | None:
    """Действующая сессия активного пользователя, иначе None"""
    if not token:
        return None
    now = now_tz()
    row = (
        await db.execute(
            select(UserSession.id, UserSession.last_seen_at, User.id, User.login, User.role)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash(token),
                UserSession.expires_at > now,
                User.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        return None
    session_id, last_seen, user_id, login, role = row
    if (now - last_seen).total_seconds() > SESSION_TOUCH_SECONDS:
        await db.execute(
            update(UserSession).where(UserSession.id == session_id).values(last_seen_at=now)
        )
        await db.commit()
    return AuthContext(user_id, login, role, session_id, csrf_token_for(token))
