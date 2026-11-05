"""Пользователи служебной части: python -m app users create-admin|set-password <login>

Пароль вводится только интерактивно через getpass, в аргументах и .env его не бывает
"""

import getpass

from sqlalchemy import select

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
