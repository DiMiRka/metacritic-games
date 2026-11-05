import asyncio
import logging
from datetime import date, datetime, time

from sqlalchemy import func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.crawler.cursor import Cursor, collect_batch
from app.crawler.heartbeat import beat
from app.crawler.processor import process_game
from app.embeddings import Embedder
from app.llm import Summarizer
from app.models import CrawlPhase, CrawlState, Game, Run, RunStatus, User
from app.scraper.client import MetacriticClient
from app.scraper.parsers import parse_listing

log = logging.getLogger(__name__)

CRAWL_LOCK_KEY = 815_001
WORKER_NAME = "crawler"
# После стольких ошибок за сутки игра больше не берётся в обход до следующего дня
MAX_FAILURES_PER_DAY = 2

# Advisory lock с ключом bigint лежит в pg_locks как старшие и младшие 32 бита
_LOCK_HELD = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = :hi AND objid = :lo AND objsubid = 1 AND granted)"
).bindparams(hi=CRAWL_LOCK_KEY >> 32, lo=CRAWL_LOCK_KEY & 0xFFFFFFFF)


async def crawl_lock_held(conn: AsyncSession | AsyncConnection) -> bool:
    """Идёт ли проход прямо сейчас, без попытки захватить блокировку"""
    return bool(await conn.scalar(_LOCK_HELD))


def day_bounds(now: datetime) -> tuple[date, datetime]:
    today = now.astimezone(settings.tz).date()
    return today, datetime.combine(today, time.min, tzinfo=settings.tz)


async def done_today(session: AsyncSession, slugs: list[str], day_start: datetime) -> set[str]:
    """Слаги, уже обработанные сегодня или упавшие сегодня MAX_FAILURES_PER_DAY раз"""
    processed = select(Game.slug).where(Game.slug.in_(slugs), Game.last_processed_at >= day_start)
    failed = text(
        "SELECT e->>'slug' FROM runs, jsonb_array_elements(errors) e "
        "WHERE started_at >= :day_start AND e->>'slug' = ANY(:slugs) "
        "GROUP BY 1 HAVING count(*) >= :max_failures"
    ).bindparams(day_start=day_start, slugs=slugs, max_failures=MAX_FAILURES_PER_DAY)
    rows = (await session.execute(processed)).scalars().all()
    rows += (await session.execute(failed)).scalars().all()
    return set(rows)


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    *,
    created: bool = False,
    error: dict | None = None,
) -> None:
    values = {"processed": Run.processed + 1}
    if error is not None:
        values["errors"] = Run.errors.op("||")(literal([error], JSONB))
    elif created:
        values["created"] = Run.created + 1
    else:
        values["updated"] = Run.updated + 1
    async with session_factory() as session, session.begin():
        await session.execute(update(Run).where(Run.id == run_id).values(**values))


async def _execute(
    run_id: int,
    client: MetacriticClient,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int,
    now: datetime,
    summarizer: Summarizer | None,
    embedder: Embedder | None,
) -> None:
    today, day_start = day_bounds(now)

    async def fetch(phase: CrawlPhase, page: int) -> list[str]:
        data = await (
            client.new_releases() if phase == CrawlPhase.NEW_RELEASES else client.browse(page)
        )
        return parse_listing(data)

    async with session_factory() as session, session.begin():
        state = await session.get(CrawlState, today, with_for_update=True)
        if state is None:
            state = CrawlState(day=today, phase=CrawlPhase.NEW_RELEASES, next_page=1)
            session.add(state)
            await session.flush()

        async def already_done(slugs: list[str]) -> set[str]:
            return await done_today(session, slugs, day_start)

        slugs, cursor = await collect_batch(
            Cursor(CrawlPhase(state.phase), state.next_page), fetch, already_done, batch_size
        )
        state.phase, state.next_page = cursor.phase, cursor.next_page

    log.info(
        "run %s: %d games, cursor -> %s/%s", run_id, len(slugs), cursor.phase, cursor.next_page
    )
    total = len(slugs)
    finished = 0
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def one(slug: str) -> None:
        nonlocal finished
        async with semaphore:
            try:
                created = await process_game(
                    session_factory, client, slug, now, summarizer, embedder
                )
                await _record(session_factory, run_id, created=created)
            except Exception as exc:
                log.warning("run %s: %s failed: %r", run_id, slug, exc)
                error = {"slug": slug, "error": f"{type(exc).__name__}: {exc}"[:500]}
                await _record(session_factory, run_id, error=error)
            finished += 1
            await beat(session_factory, WORKER_NAME, "running", f"{finished}/{total}: {slug}")

    await asyncio.gather(*(one(slug) for slug in slugs))


async def run_crawl(
    trigger: str,
    *,
    client: MetacriticClient,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int | None = None,
    now: datetime | None = None,
    summarizer: Summarizer | None = None,
    embedder: Embedder | None = None,
    requested_by: int | None = None,
) -> int | None:
    """Один проход обхода. Возвращает id прохода или None, если проход уже идёт."""
    async with engine.connect() as lock_conn:
        locked = await lock_conn.scalar(select(func.pg_try_advisory_lock(CRAWL_LOCK_KEY)))
        await lock_conn.commit()
        if not locked:
            log.info("crawl already running, skip")
            return None
        try:
            async with session_factory() as session, session.begin():
                # id пришёл через NOTIFY: несуществующий пользователь превращается в NULL
                requester = (
                    select(User.id).where(User.id == requested_by).scalar_subquery()
                    if requested_by is not None
                    else None
                )
                run = Run(trigger=trigger, status=RunStatus.RUNNING, requested_by=requester)
                session.add(run)
                await session.flush()
                run_id = run.id
            await beat(session_factory, WORKER_NAME, "running", "collecting candidates")

            status = RunStatus.SUCCESS
            try:
                await _execute(
                    run_id,
                    client,
                    session_factory,
                    batch_size or settings.batch_size,
                    now or datetime.now(settings.tz),
                    summarizer,
                    embedder,
                )
            except Exception as exc:
                log.exception("run %s failed", run_id)
                status = RunStatus.FAILED
                await _record_failure(session_factory, run_id, exc)

            async with session_factory() as session, session.begin():
                await session.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(status=status, finished_at=func.now())
                )
            await beat(session_factory, WORKER_NAME, "idle")
            return run_id
        finally:
            await lock_conn.scalar(select(func.pg_advisory_unlock(CRAWL_LOCK_KEY)))
            await lock_conn.commit()


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession], run_id: int, exc: Exception
) -> None:
    error = {"slug": None, "error": f"{type(exc).__name__}: {exc}"[:500]}
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(errors=Run.errors.op("||")(literal([error], JSONB)))
        )


async def fail_stale_runs(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """Помечает упавшими проходы, оставшиеся в running после падения воркера."""
    async with engine.connect() as conn:
        locked = await conn.scalar(select(func.pg_try_advisory_lock(CRAWL_LOCK_KEY)))
        if not locked:
            return 0
        try:
            async with session_factory() as session, session.begin():
                result = await session.execute(
                    update(Run)
                    .where(Run.status == RunStatus.RUNNING)
                    .values(status=RunStatus.FAILED, finished_at=func.now())
                )
                return result.rowcount
        finally:
            await conn.scalar(select(func.pg_advisory_unlock(CRAWL_LOCK_KEY)))
            await conn.commit()
