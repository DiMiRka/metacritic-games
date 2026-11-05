"""Роли базы с минимальными правами: python -m app db-roles

Выполняется владельцем схемы после миграций (сервис migrate) и идемпотентна: создаёт роли
или обновляет их пароли из .env, сбрасывает и заново выдаёт права на таблицы и задаёт
права по умолчанию для таблиц будущих миграций
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings
from app.core.db import engine

WEB_ROLE = "app_web"
WORKER_ROLE = "app_worker"

WORKER_TABLES = (
    "games",
    "game_platforms",
    "review_summaries",
    "letsplays",
    "crawl_state",
    "runs",
    "worker_heartbeats",
)
WEB_READ_TABLES = (
    "games",
    "game_platforms",
    "review_summaries",
    "letsplays",
    "runs",
    "crawl_state",
    "worker_heartbeats",
)
WEB_WRITE_TABLES = ("users", "sessions", "login_attempts")
DML = "SELECT, INSERT, UPDATE, DELETE"


async def _ensure_role(conn: AsyncConnection, role: str, password: str) -> None:
    exists = await conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
    verb = "ALTER ROLE %I WITH" if exists else "CREATE ROLE %I WITH"
    # format() экранирует имя и пароль на стороне Postgres
    options = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L"
    statement = await conn.scalar(
        text(f"SELECT format('{verb} {options}', CAST(:r AS text), CAST(:p AS text))"),
        {"r": role, "p": password},
    )
    await conn.execute(text(statement))


async def _sequences(conn: AsyncConnection, tables: tuple[str, ...]) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT pg_get_serial_sequence(table_name, 'id') FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'id' "
            "AND table_name = ANY(CAST(:tables AS text[]))"
        ),
        {"tables": list(tables)},
    )
    return [seq for (seq,) in rows if seq]


async def apply_roles(
    conn: AsyncConnection,
    web_password: str,
    worker_password: str,
    web_role: str = WEB_ROLE,
    worker_role: str = WORKER_ROLE,
) -> None:
    await _ensure_role(conn, web_role, web_password)
    await _ensure_role(conn, worker_role, worker_password)
    owner = await conn.scalar(text("SELECT current_user"))
    database = await conn.scalar(text("SELECT current_database()"))
    roles = f'"{web_role}", "{worker_role}"'

    statements = [
        f'GRANT CONNECT ON DATABASE "{database}" TO {roles}',
        f"GRANT USAGE ON SCHEMA public TO {roles}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}",
        f'GRANT {DML} ON {", ".join(WORKER_TABLES)} TO "{worker_role}"',
        # Воркер проверяет, что пользователь из NOTIFY существует, хеши паролей ему не видны
        f'GRANT SELECT (id) ON users TO "{worker_role}"',
        f'GRANT SELECT ON {", ".join(WEB_READ_TABLES)} TO "{web_role}"',
        f'GRANT {DML} ON {", ".join(WEB_WRITE_TABLES)} TO "{web_role}"',
    ]
    for seq in await _sequences(conn, WORKER_TABLES):
        statements.append(f'GRANT USAGE ON SEQUENCE {seq} TO "{worker_role}"')
    for seq in await _sequences(conn, WEB_WRITE_TABLES):
        statements.append(f'GRANT USAGE ON SEQUENCE {seq} TO "{web_role}"')

    # Таблицы будущих миграций: воркеру рабочие права, сайту только чтение,
    # запись для сайта новая миграция выдаёт явно и вносит таблицу в списки выше
    default = f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public'
    statements += [
        f'{default} GRANT {DML} ON TABLES TO "{worker_role}"',
        f'{default} GRANT SELECT ON TABLES TO "{web_role}"',
        f'{default} GRANT USAGE ON SEQUENCES TO "{worker_role}"',
    ]
    for statement in statements:
        await conn.execute(text(statement))


async def setup_roles() -> None:
    if not settings.app_web_db_password or not settings.app_worker_db_password:
        raise SystemExit("Задайте APP_WEB_DB_PASSWORD и APP_WORKER_DB_PASSWORD в .env")
    async with engine.begin() as conn:
        await apply_roles(conn, settings.app_web_db_password, settings.app_worker_db_password)
    print(f"Роли {WEB_ROLE} и {WORKER_ROLE} настроены")
