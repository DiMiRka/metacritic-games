"""Пользователи служебной части: python -m app users create-admin|set-password|totp|totp-off

Пароль вводится только интерактивно через getpass, в аргументах и .env его не бывает
"""

import getpass

from sqlalchemy import select

from app.auth import totp
from app.auth.models import User, UserRole
from app.auth.service import hash_password, normalize_login, revoke_user_sessions
from app.core.db import SessionLocal


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
    async with SessionLocal() as db, db.begin():
        if await db.scalar(select(User).where(User.login == login)):
            raise SystemExit(f"Пользователь {login} уже есть, для смены пароля: set-password")
        db.add(User(login=login, password_hash=ask_password(), role=UserRole.ADMIN))
    print(f"Создан администратор {login}")


async def set_password(login: str) -> None:
    login = normalize_login(login)
    async with SessionLocal() as db, db.begin():
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
    async with SessionLocal() as db:
        await _user(db, login)
    secret = totp.new_secret()
    print("Добавьте ключ в приложение-аутентификатор (Google Authenticator, Aegis, 1Password)")
    print(f"Ключ: {secret}")
    print(f"Или ссылка для QR-кода: {totp.provisioning_uri(secret, login)}")
    step = totp.match_step(secret, ask_code(), last_step=None)
    if step is None:
        raise SystemExit("Код не подошёл, второй фактор не включён")
    async with SessionLocal() as db, db.begin():
        user = await _user(db, login)
        user.totp_secret, user.totp_last_step = secret, step
    print(f"Второй фактор для {login} включён")


async def disable_totp(login: str) -> None:
    login = normalize_login(login)
    async with SessionLocal() as db, db.begin():
        user = await _user(db, login)
        user.totp_secret = user.totp_last_step = None
        await revoke_user_sessions(db, user.id)
    print(f"Второй фактор для {login} выключен, все его сессии завершены")
