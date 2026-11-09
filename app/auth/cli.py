"""Пользователи служебной части: python -m app users create-admin|set-password|totp|totp-off

Пароль вводится только интерактивно через getpass, в аргументах и .env его не бывает
"""

import getpass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import totp
from app.auth.models import TotpRecoveryCode, User, UserRole
from app.auth.service import (
    hash_password,
    issue_recovery_codes,
    normalize_login,
    rekey_totp_secrets,
    revoke_user_sessions,
    seal_totp_secret,
)
from app.core.config import settings
from app.core.db import default_database


def open_session() -> AsyncSession:
    return default_database().session_factory()


def ask_password() -> str:
    first = getpass.getpass("Пароль: ")
    if getpass.getpass("Повторите пароль: ") != first:
        raise SystemExit("Пароли не совпадают")
    try:
        return hash_password(first)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


async def create_admin(login: str) -> None:
    login = normalize_login(login)
    async with open_session() as db, db.begin():
        if await db.scalar(select(User).where(User.login == login)):
            raise SystemExit(f"Пользователь {login} уже есть, для смены пароля: set-password")
        db.add(User(login=login, password_hash=ask_password(), role=UserRole.ADMIN))
    print(f"Создан администратор {login}")


async def set_password(login: str) -> None:
    login = normalize_login(login)
    async with open_session() as db, db.begin():
        user = await db.scalar(select(User).where(User.login == login))
        if user is None:
            raise SystemExit(f"Пользователя {login} нет")
        user.password_hash = ask_password()
        await revoke_user_sessions(db, user.id)
    print(f"Пароль {login} обновлён, все его сессии завершены")


def ask_code() -> str:
    return input("Код из приложения: ")


async def _user(db, login: str) -> User:
    user = await db.scalar(select(User).where(User.login == login))
    if user is None:
        raise SystemExit(f"Пользователя {login} нет")
    return user


async def enable_totp(login: str) -> None:
    """Новый секрет TOTP; включается, только когда приложение выдало верный код"""
    login = normalize_login(login)
    async with open_session() as db:
        user = await _user(db, login)
        role = user.role
    if role != UserRole.ADMIN:
        raise SystemExit(
            f"Второй фактор нужен только администраторам, у {login} роль {role}: "
            "при входе код у него не спрашивается"
        )
    secret = totp.new_secret()
    print("Добавьте ключ в приложение-аутентификатор (Google Authenticator, Aegis, 1Password)")
    print(f"Ключ: {secret}")
    print(f"Или ссылка для QR-кода: {totp.provisioning_uri(secret, login)}")
    step = totp.match_step(secret, ask_code(), last_step=None)
    if step is None:
        raise SystemExit("Код не подошёл, второй фактор не включён")
    async with open_session() as db, db.begin():
        user = await _user(db, login)
        user.totp_secret = seal_totp_secret(user.id, secret, config=settings)
        user.totp_last_step = step
        codes = await issue_recovery_codes(db, user.id)
    print(f"Второй фактор для {login} включён")
    print_recovery_codes(codes)


def print_recovery_codes(codes: list[str]) -> None:
    print("Резервные коды на случай потери телефона, каждый действует один раз.")
    print("Сохраните их вне сервера, повторно они не показываются:")
    for code in codes:
        print(f"  {code}")


async def new_recovery_codes(login: str) -> None:
    """Новый набор резервных кодов, прежние перестают действовать"""
    login = normalize_login(login)
    async with open_session() as db, db.begin():
        user = await _user(db, login)
        if not user.totp_secret:
            raise SystemExit(
                f"У {login} второй фактор не включён: python -m app users totp {login}"
            )
        codes = await issue_recovery_codes(db, user.id)
    print_recovery_codes(codes)


async def rekey() -> None:
    """Перешифровать секреты TOTP текущим SECRET_KEY, обычно это делает web при старте"""
    async with open_session() as db, db.begin():
        changed, failed = await rekey_totp_secrets(db, config=settings)
    print(f"Перешифровано секретов: {changed}, не удалось расшифровать: {failed}")
    if failed:
        raise SystemExit(1)


async def disable_totp(login: str) -> None:
    login = normalize_login(login)
    async with open_session() as db, db.begin():
        user = await _user(db, login)
        user.totp_secret = user.totp_last_step = None
        await db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
        await revoke_user_sessions(db, user.id)
    print(f"Второй фактор для {login} выключен, все его сессии завершены")
