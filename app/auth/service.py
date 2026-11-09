"""Пароли, серверные сессии, счётчик попыток входа и CSRF-токен"""

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from loguru import logger
from sqlalchemy import delete, distinct, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import crypto, totp
from app.auth.models import (
    LoginAttempt,
    LoginChallenge,
    TotpRecoveryCode,
    User,
    UserRole,
    UserSession,
)
from app.core.clock import now_tz
from app.core.config import Settings

MIN_PASSWORD_LENGTH = 12
LOGIN_MAX_LENGTH = 100
SESSION_TOUCH_SECONDS = 60
# Попытки хранятся не меньше суток: по ним монитор показывает серии неудачных входов
ATTEMPTS_KEEP = timedelta(days=1)
DELAY_BASE_SECONDS = 2

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
    # Пора обновить last_seen_at: это делает фоновая задача после ответа, не сам запрос
    touch_due: bool = False

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


def csrf_token_for(session_token: str, *, config: Settings) -> str:
    """CSRF-токен — HMAC от токена сессии на серверном секрете: без cookie и без SECRET_KEY
    его не получить, хранить не нужно"""
    return _csrf_token(config.secret_key, session_token)


def _csrf_token(secret: str, session_token: str) -> str:
    return hmac.new(secret.encode(), b"csrf:" + session_token.encode(), hashlib.sha256).hexdigest()


def csrf_valid(session_token: str, candidate: str | None, *, config: Settings) -> bool:
    """Принимаются и токены прежних ключей: страницы, открытые до смены SECRET_KEY, работают"""
    if not candidate:
        return False
    return any(
        hmac.compare_digest(_csrf_token(secret, session_token), candidate)
        for secret in config.secret_keys
    )


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


async def too_many_attempts(db: AsyncSession, ip: str, login: str, *, config: Settings) -> bool:
    since = now_tz(config.tz) - timedelta(minutes=config.login_window_minutes)
    count = await db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.ip == ip,
            LoginAttempt.login == normalize_login(login),
            LoginAttempt.attempted_at >= since,
        )
    )
    return count >= config.login_max_attempts


def _window(config: Settings) -> timedelta:
    return timedelta(minutes=config.login_window_minutes)


async def login_delay(db: AsyncSession, login: str, *, config: Settings) -> int:
    """Сколько секунд ждать до следующей попытки под этим логином, с какого адреса ни войди.
    Отклонённые из-за задержки попытки не записываются: растянуть её дальше нельзя
    """
    count, last, now = (
        await db.execute(
            select(func.count(), func.max(LoginAttempt.attempted_at), func.now()).where(
                LoginAttempt.login == normalize_login(login),
                LoginAttempt.attempted_at >= func.now() - _window(config),
            )
        )
    ).one()
    if count < config.login_delay_after:
        return 0
    power = min(count - config.login_delay_after, 20)
    delay = min(DELAY_BASE_SECONDS * 2**power, config.login_delay_max_seconds)
    return max(0, math.ceil((last + timedelta(seconds=delay) - now).total_seconds()))


async def record_failed_attempt(db: AsyncSession, ip: str, login: str, *, config: Settings) -> None:
    login = normalize_login(login)
    keep = max(_window(config), ATTEMPTS_KEEP)
    await db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < func.now() - keep))
    db.add(LoginAttempt(ip=ip, login=login))
    await db.flush()
    count, ips = (
        await db.execute(
            select(func.count(), func.count(distinct(LoginAttempt.ip))).where(
                LoginAttempt.login == login,
                LoginAttempt.attempted_at >= func.now() - _window(config),
            )
        )
    ).one()
    if count % config.login_alert_threshold == 0:
        logger.warning(
            "серия неудачных входов: логин {!r}, {} попыток с {} адресов за {} мин",
            login,
            count,
            ips,
            config.login_window_minutes,
        )


@dataclass(frozen=True)
class FailedLogins:
    login: str
    attempts: int
    ips: int
    last_at: datetime


async def failed_login_series(db: AsyncSession, limit: int = 10) -> list[FailedLogins]:
    """Логины с неудачными попытками за сутки, больше всего попыток сверху"""
    rows = await db.execute(
        select(
            LoginAttempt.login,
            func.count(),
            func.count(distinct(LoginAttempt.ip)),
            func.max(LoginAttempt.attempted_at),
        )
        .where(LoginAttempt.attempted_at >= func.now() - ATTEMPTS_KEEP)
        .group_by(LoginAttempt.login)
        .order_by(func.count().desc(), LoginAttempt.login)
        .limit(limit)
    )
    return [FailedLogins(*row) for row in rows]


class SecondFactor(StrEnum):
    # Входить сразу
    OK = "ok"
    # Нужен второй шаг: код TOTP или резервный код
    CODE_REQUIRED = "code_required"
    # Администратору без TOTP вход закрыт, пока второй фактор обязателен
    SETUP_REQUIRED = "setup_required"


TOTP_PURPOSE = "totp"
RECOVERY_CODES = 10
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_LOOKUP_LENGTH = 4
RECOVERY_SECRET_LENGTH = 8


def seal_totp_secret(user_id: int, secret: str, *, config: Settings) -> str:
    return crypto.encrypt(
        secret, purpose=TOTP_PURPOSE, context=f"user:{user_id}", secrets=config.secret_keys
    )


def open_totp_secret(user: User, *, config: Settings) -> str:
    """Секрет из базы; открытый текст бывает только до первого старта web после обновления"""
    if not crypto.is_encrypted(user.totp_secret):
        return user.totp_secret
    return crypto.decrypt(
        user.totp_secret,
        purpose=TOTP_PURPOSE,
        context=f"user:{user.id}",
        secrets=config.secret_keys,
    )


async def rekey_totp_secrets(db: AsyncSession, *, config: Settings) -> tuple[int, int]:
    """Шифрует текущим ключом открытые секреты и секреты под прежними ключами.
    Возвращает (перешифровано, не удалось расшифровать)
    """
    users = (await db.scalars(select(User).where(User.totp_secret.is_not(None)))).all()
    changed = failed = 0
    for user in users:
        if not crypto.needs_rekey(
            user.totp_secret, purpose=TOTP_PURPOSE, secrets=config.secret_keys
        ):
            continue
        try:
            secret = open_totp_secret(user, config=config)
            user.totp_secret = seal_totp_secret(user.id, secret, config=config)
            changed += 1
        except crypto.SecretDecryptError as exc:
            failed += 1
            logger.error("секрет TOTP пользователя {} не расшифровать: {}", user.login, exc)
    return changed, failed


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def _new_recovery_code() -> str:
    length = RECOVERY_LOOKUP_LENGTH + RECOVERY_SECRET_LENGTH
    return "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(length))


def format_recovery_code(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


async def issue_recovery_codes(db: AsyncSession, user_id: int) -> list[str]:
    """Новый набор резервных кодов, прежние перестают действовать. Коды показываются один раз"""
    await db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user_id))
    codes: list[str] = []
    lookups: set[str] = set()
    while len(codes) < RECOVERY_CODES:
        code = _new_recovery_code()
        lookup = code[:RECOVERY_LOOKUP_LENGTH]
        if lookup in lookups:
            continue
        lookups.add(lookup)
        codes.append(code)
        db.add(
            TotpRecoveryCode(
                user_id=user_id,
                lookup=lookup,
                code_hash=_hasher.hash(code[RECOVERY_LOOKUP_LENGTH:]),
            )
        )
    return [format_recovery_code(code) for code in codes]


async def use_recovery_code(db: AsyncSession, user: User, code: str) -> bool:
    code = _normalize_recovery(code)
    if len(code) != RECOVERY_LOOKUP_LENGTH + RECOVERY_SECRET_LENGTH:
        return False
    row = await db.scalar(
        select(TotpRecoveryCode).where(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.lookup == code[:RECOVERY_LOOKUP_LENGTH],
            TotpRecoveryCode.used_at.is_(None),
        )
    )
    if row is None or not _verify(row.code_hash, code[RECOVERY_LOOKUP_LENGTH:]):
        return False
    # Условие в UPDATE: один код не пройдёт в двух одновременных входах
    used = await db.execute(
        update(TotpRecoveryCode)
        .where(TotpRecoveryCode.id == row.id, TotpRecoveryCode.used_at.is_(None))
        .values(used_at=func.now())
        .execution_options(synchronize_session=False)
    )
    if used.rowcount != 1:
        return False
    left = await db.scalar(
        select(func.count())
        .select_from(TotpRecoveryCode)
        .where(TotpRecoveryCode.user_id == user.id, TotpRecoveryCode.used_at.is_(None))
    )
    logger.warning("вход {} по резервному коду, осталось кодов: {}", user.login, left - 1)
    return True


def _looks_like_totp(code: str) -> bool:
    digits = "".join(code.split())
    return len(digits) == totp.DIGITS and digits.isdigit()


def second_factor_step(user: User, *, config: Settings) -> SecondFactor:
    """Что нужно после верного пароля. Код спрашивается только у администратора с TOTP:
    у viewer второго фактора нет, даже если секрет в базе вдруг остался"""
    if user.role != UserRole.ADMIN:
        return SecondFactor.OK
    if user.totp_secret:
        return SecondFactor.CODE_REQUIRED
    if config.totp_required_for_admins:
        return SecondFactor.SETUP_REQUIRED
    return SecondFactor.OK


async def verify_totp(db: AsyncSession, user: User, code: str, *, config: Settings) -> bool:
    try:
        secret = open_totp_secret(user, config=config)
    except crypto.SecretDecryptError as exc:
        logger.error("секрет TOTP {} не расшифровать: {}", user.login, exc)
        return False
    step = totp.match_step(secret, code, last_step=user.totp_last_step)
    if step is None:
        return False
    # Условие в UPDATE: из двух одновременных входов с одним кодом пройдёт только один
    result = await db.execute(
        update(User)
        .where(
            User.id == user.id,
            or_(User.totp_last_step.is_(None), User.totp_last_step < step),
        )
        .values(totp_last_step=step)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    user.totp_last_step = step
    return True


async def verify_second_factor(
    db: AsyncSession, user: User, code: str, *, config: Settings
) -> bool:
    """6 цифр — код TOTP, иначе резервный код"""
    if not user.totp_secret or not code.strip():
        return False
    if _looks_like_totp(code):
        return await verify_totp(db, user, code, config=config)
    return await use_recovery_code(db, user, code)


async def create_login_challenge(
    db: AsyncSession, user: User, next_url: str, ip: str | None, *, config: Settings
) -> str:
    """Ожидание кода после верного пароля. Прежнее ожидание пользователя удаляется:
    одновременно действует только одно. Возвращает токен для cookie, в базе только хеш"""
    await db.execute(delete(LoginChallenge).where(LoginChallenge.user_id == user.id))
    token = secrets.token_urlsafe(32)
    db.add(
        LoginChallenge(
            user_id=user.id,
            token_hash=token_hash(token),
            next_url=safe_next(next_url),
            ip=(ip or "")[:64] or None,
            expires_at=func.now() + timedelta(seconds=config.login_code_ttl_seconds),
        )
    )
    await db.flush()
    return token


async def load_login_challenge(db: AsyncSession, token: str | None) -> LoginChallenge | None:
    """Действующее ожидание по токену из cookie. Строка блокируется до конца транзакции:
    два одновременных ввода кода не обойдут счётчик попыток"""
    if not token:
        return None
    return await db.scalar(
        select(LoginChallenge)
        .where(
            LoginChallenge.token_hash == token_hash(token),
            LoginChallenge.expires_at > func.now(),
        )
        .with_for_update()
    )


async def delete_login_challenge(db: AsyncSession, challenge: LoginChallenge) -> None:
    await db.execute(delete(LoginChallenge).where(LoginChallenge.id == challenge.id))


async def clear_attempts(db: AsyncSession, ip: str, login: str) -> None:
    await db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.ip == ip, LoginAttempt.login == normalize_login(login)
        )
    )


async def create_session(
    db: AsyncSession, user: User, ip: str | None, user_agent: str | None, *, config: Settings
) -> str:
    """Новый токен на каждый вход, в базу пишется только его хеш"""
    token = secrets.token_urlsafe(32)
    now = now_tz(config.tz)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=now + timedelta(hours=config.session_ttl_hours),
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


def idle_condition(now: datetime, config: Settings) -> list:
    """Сессия без активности дольше SESSION_IDLE_MINUTES больше не действует.
    Активность отмечается не чаще раза в SESSION_TOUCH_SECONDS, это и есть точность таймаута
    """
    if not config.session_idle_minutes:
        return []
    return [UserSession.last_seen_at > now - timedelta(minutes=config.session_idle_minutes)]


async def load_session(
    db: AsyncSession, token: str | None, *, config: Settings
) -> AuthContext | None:
    """Действующая сессия активного пользователя, иначе None. Только читает базу"""
    if not token:
        return None
    now = now_tz(config.tz)
    row = (
        await db.execute(
            select(UserSession.id, UserSession.last_seen_at, User.id, User.login, User.role)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash(token),
                UserSession.expires_at > now,
                *idle_condition(now, config),
                User.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        return None
    session_id, last_seen, user_id, login, role = row
    touch_due = (now - last_seen).total_seconds() > SESSION_TOUCH_SECONDS
    return AuthContext(
        user_id, login, role, session_id, csrf_token_for(token, config=config), touch_due
    )


async def touch_session(session_factory: async_sessionmaker[AsyncSession], session_id: int) -> None:
    """Отметка активности в своей транзакции, вне сессии запроса"""
    async with session_factory() as db, db.begin():
        await db.execute(
            update(UserSession).where(UserSession.id == session_id).values(last_seen_at=func.now())
        )
