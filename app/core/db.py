"""Подключение к базе. Engine создаётся явно: в lifespan приложения или при первом обращении
из команды CLI и воркера, но не при импорте модуля"""

from functools import cache

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()


@cache
def default_database() -> Database:
    """База для процессов без приложения FastAPI: воркер и команды CLI"""
    return Database(settings.database_url)


async def dispose_default_database() -> None:
    if default_database.cache_info().currsize:
        await default_database().dispose()
        default_database.cache_clear()


def asyncpg_dsn(database_url: str) -> str:
    """DSN для прямого asyncpg-соединения, например для LISTEN"""
    return make_url(database_url).set(drivername="postgresql").render_as_string(False)
