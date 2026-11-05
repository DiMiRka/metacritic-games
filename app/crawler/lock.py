"""Session-level advisory lock: не даёт двум проходам идти одновременно"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

CRAWL_LOCK_KEY = 815_001

# Advisory lock с ключом bigint лежит в pg_locks как старшие и младшие 32 бита
_LOCK_HELD = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = :hi AND objid = :lo AND objsubid = 1 AND granted)"
).bindparams(hi=CRAWL_LOCK_KEY >> 32, lo=CRAWL_LOCK_KEY & 0xFFFFFFFF)


async def crawl_lock_held(conn: AsyncSession | AsyncConnection) -> bool:
    """Идёт ли проход прямо сейчас, без попытки захватить блокировку"""
    return bool(await conn.scalar(_LOCK_HELD))


async def try_lock(conn: AsyncConnection) -> bool:
    """Захватывает блокировку на соединении, держится до unlock или закрытия соединения"""
    return bool(await conn.scalar(select(func.pg_try_advisory_lock(CRAWL_LOCK_KEY))))


async def unlock(conn: AsyncConnection) -> None:
    await conn.scalar(select(func.pg_advisory_unlock(CRAWL_LOCK_KEY)))
