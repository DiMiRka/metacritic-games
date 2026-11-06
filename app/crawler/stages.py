"""Этапы обхода как задачи очереди: collect → scrape_game → summarize и embed"""

from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.catalog.models import Game, ReviewSource
from app.core.clock import day_bounds, now_tz
from app.core.config import settings
from app.core.errors import PermanentError
from app.core.logs import log_context
from app.crawler.cursor import Cursor, collect_batch
from app.crawler.lock import try_lock, unlock
from app.crawler.models import CrawlPhase, CrawlState, RunItemStage, RunItemStatus
from app.crawler.processor import (
    fetch_bundle,
    load_reviews,
    parse_bundle,
    record_scores,
    replace_reviews,
    save_embedding,
    upsert_game,
)
from app.crawler.raw import RawBundle, latest_hash, save_raw
from app.crawler.runs import ItemInfo, create_run, done_today, fail_run, finalize_run, record_item
from app.enrichment.embeddings import Embedder, embedding_outdated, embedding_text, text_hash
from app.enrichment.llm import Summarizer
from app.enrichment.summaries import summary_outdated, update_summary
from app.jobs.models import ACTIVE_STATUSES, Job, JobKind
from app.jobs.pool import Finish, JobType
from app.jobs.queue import ClaimedJob, enqueue
from app.scraper.client import MetacriticClient
from app.scraper.parsers import Review, parse_listing

COLLECT_DEDUPE_KEY = "collect"
MANUAL_COLLECT_PRIORITY = 100
SCHEDULED_COLLECT_PRIORITY = 50


@dataclass(frozen=True)
class ScrapeResult:
    game_id: int
    created: bool
    changed: bool = True


@dataclass(frozen=True)
class StageResult:
    game_id: int
    changed: bool


async def enqueue_collect(
    session: AsyncSession, trigger: str, requested_by: int | None = None
) -> int | None:
    """Ставит сбор кандидатов; None, если collect уже в очереди или выполняется"""
    priority = MANUAL_COLLECT_PRIORITY if trigger == "manual" else SCHEDULED_COLLECT_PRIORITY
    return await enqueue(
        session,
        JobKind.COLLECT,
        {"trigger": trigger},
        priority=priority,
        dedupe_key=COLLECT_DEDUPE_KEY,
        requested_by=requested_by,
    )


async def request_manual_collect(session: AsyncSession, user_id: int) -> int | None:
    """Ручной запуск с сайта через функцию request_manual_collect: прав на запись в jobs
    у сайта нет. None, если collect уже в очереди или не прошла пауза между запусками
    """
    return await session.scalar(select(func.request_manual_collect(user_id)))


async def collect_active(session: AsyncSession) -> bool:
    """collect уже в очереди или выполняется"""
    count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == JobKind.COLLECT, Job.status.in_(ACTIVE_STATUSES))
    )
    return bool(count)


async def store_bundle(
    session: AsyncSession,
    bundle: RawBundle,
    slug: str,
    processed_at: datetime | None,
    *,
    save: bool = True,
) -> tuple[int, bool, dict[ReviewSource, list[Review]]]:
    """Разбирает сырой ответ и сохраняет игру, платформы, отзывы и сам ответ"""
    data, reviews = parse_bundle(bundle, slug)
    game_id, created = await upsert_game(session, data, processed_at)
    await replace_reviews(session, game_id, reviews)
    # При пересборке новых наблюдений нет: ни сырой ответ, ни история оценок не пишутся
    if save:
        await save_raw(session, game_id, bundle)
        await record_scores(session, game_id, data.platforms)
    return game_id, created, reviews


@dataclass
class Derived:
    """Сколько задач резюме и эмбеддингов поставлено в очередь"""

    summaries: int = 0
    embeddings: int = 0


async def enqueue_derived(
    session: AsyncSession,
    game_id: int,
    reviews: dict[ReviewSource, list[Review]],
    *,
    run_id: int | None,
    summary_model: str | None,
    embed_model: str | None,
) -> Derived:
    """Резюме и эмбеддинг, только если их входные данные, модель или промпт поменялись"""
    derived = Derived()
    if summary_model:
        for source, items in reviews.items():
            if await summary_outdated(session, game_id, source, items, summary_model):
                job_id = await enqueue(
                    session,
                    JobKind.SUMMARIZE,
                    {"game_id": game_id, "source": source},
                    dedupe_key=f"summarize:{game_id}:{source}",
                    run_id=run_id,
                )
                derived.summaries += job_id is not None
    if embed_model and await embedding_outdated(session, game_id, embed_model):
        job_id = await enqueue(
            session,
            JobKind.EMBED,
            {"game_id": game_id},
            dedupe_key=f"embed:{game_id}",
            run_id=run_id,
        )
        derived.embeddings += job_id is not None
    return derived


async def stored_reviews(session: AsyncSession, game_id: int) -> dict[ReviewSource, list[Review]]:
    return {source: await load_reviews(session, game_id, source) for source in ReviewSource}


class CrawlStages:
    def __init__(
        self,
        *,
        client: MetacriticClient,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        summarizer: Summarizer | None,
        embedder: Embedder | None,
        batch_size: int | None = None,
    ) -> None:
        self.client = client
        self.engine = engine
        self.session_factory = session_factory
        self.summarizer = summarizer
        self.embedder = embedder
        self.batch_size = batch_size or settings.batch_size

    def job_types(self) -> dict[str, JobType]:
        types = {
            JobKind.COLLECT: JobType(self.collect, self._collect_finished),
            JobKind.SCRAPE_GAME: JobType(self.scrape_game, self._scrape_finished),
        }
        if self.summarizer:
            types[JobKind.SUMMARIZE] = JobType(self.summarize, self._summarize_finished)
        if self.embedder:
            types[JobKind.EMBED] = JobType(self.embed, self._embed_finished)
        return types

    # collect

    async def collect(self, job: ClaimedJob) -> int | None:
        """Набирает кандидатов и ставит по задаче scrape_game на игру. Возвращает id прохода"""
        async with self.engine.connect() as lock_conn:
            locked = await try_lock(lock_conn)
            await lock_conn.commit()
            if not locked:
                logger.info("collect: блокировка занята, пропускаю")
                return None
            try:
                run_id = await create_run(
                    self.session_factory, job.payload.get("trigger", "schedule"), job.requested_by
                )
                try:
                    with log_context(run_id=run_id):
                        await self._collect(job, run_id)
                except Exception:
                    await fail_run(self.session_factory, run_id)
                    raise
                return run_id
            finally:
                await unlock(lock_conn)
                await lock_conn.commit()

    async def _collect(self, job: ClaimedJob, run_id: int) -> None:
        today, day_start = day_bounds(now_tz())

        async def fetch(phase: CrawlPhase, page: int) -> list[str]:
            data = await (
                self.client.new_releases()
                if phase == CrawlPhase.NEW_RELEASES
                else self.client.browse(page)
            )
            return parse_listing(data)

        async with self.session_factory() as session, session.begin():
            # Проход привязывается к самой задаче collect: так её хук его закроет
            await session.execute(update(Job).where(Job.id == job.id).values(run_id=run_id))
            state = await session.get(CrawlState, today, with_for_update=True)
            if state is None:
                state = CrawlState(day=today, phase=CrawlPhase.NEW_RELEASES, next_page=1)
                session.add(state)
                await session.flush()

            async def already_done(slugs: list[str]) -> set[str]:
                return await done_today(session, slugs, day_start)

            slugs, cursor = await collect_batch(
                Cursor(CrawlPhase(state.phase), state.next_page),
                fetch,
                already_done,
                self.batch_size,
            )
            state.phase, state.next_page = cursor.phase, cursor.next_page
            for slug in slugs:
                await enqueue(
                    session,
                    JobKind.SCRAPE_GAME,
                    {"slug": slug},
                    dedupe_key=f"scrape:{slug}:{today.isoformat()}",
                    run_id=run_id,
                )
        logger.info(
            "run {}: {} игр, курсор {}/{}", run_id, len(slugs), cursor.phase, cursor.next_page
        )

    async def _collect_finished(self, session: AsyncSession, job: ClaimedJob, finish: Finish):
        if finish.error is None and finish.result is not None:
            await finalize_run(session, finish.result)

    # scrape_game

    async def scrape_game(self, job: ClaimedJob) -> ScrapeResult:
        slug = job.payload["slug"]
        bundle = await fetch_bundle(self.client, slug)
        async with self.session_factory() as session, session.begin():
            game_id = await session.scalar(select(Game.id).where(Game.slug == slug))
            if game_id is not None and await latest_hash(session, game_id) == bundle.digest:
                # Ответ тот же: игру не перезаписываем, отмечаем только, что сегодня её видели.
                # Резюме и эмбеддинг всё равно сверяются: могли смениться модель или промпт
                await save_raw(session, game_id, bundle)
                await session.execute(
                    update(Game).where(Game.id == game_id).values(last_processed_at=now_tz())
                )
                reviews = await stored_reviews(session, game_id)
                await self.enqueue_derived(session, game_id, reviews, job.run_id)
                return ScrapeResult(game_id, created=False, changed=False)
            game_id, created, reviews = await store_bundle(session, bundle, slug, now_tz())
            await self.enqueue_derived(session, game_id, reviews, job.run_id)
        return ScrapeResult(game_id, created)

    async def enqueue_derived(
        self,
        session: AsyncSession,
        game_id: int,
        reviews: dict[ReviewSource, list[Review]],
        run_id: int | None,
    ) -> Derived:
        return await enqueue_derived(
            session,
            game_id,
            reviews,
            run_id=run_id,
            summary_model=self.summarizer.model if self.summarizer else None,
            embed_model=self.embedder.model if self.embedder else None,
        )

    async def _scrape_finished(self, session: AsyncSession, job: ClaimedJob, finish: Finish):
        result: ScrapeResult | None = finish.result
        if result and not result.changed:
            status = RunItemStatus.SKIPPED
        elif result and result.created:
            status = RunItemStatus.CREATED
        else:
            status = RunItemStatus.UPDATED
        info = ItemInfo(
            RunItemStage.SCRAPE, job.payload.get("slug"), result and result.game_id, status
        )
        await record_item(session, job, finish, info)

    # summarize

    async def summarize(self, job: ClaimedJob) -> StageResult:
        game_id, source = job.payload["game_id"], ReviewSource(job.payload["source"])
        async with self.session_factory() as session:
            title = await session.scalar(select(Game.title).where(Game.id == game_id))
            if title is None:
                raise PermanentError(f"игры {game_id} больше нет")
            reviews = await load_reviews(session, game_id, source)
        changed = await update_summary(
            self.session_factory, self.summarizer, game_id, title, source, reviews
        )
        return StageResult(game_id, changed)

    async def _summarize_finished(self, session: AsyncSession, job: ClaimedJob, finish: Finish):
        stage = f"summarize_{job.payload.get('source')}"
        changed = finish.result.changed if finish.result else False
        status = RunItemStatus.UPDATED if changed else RunItemStatus.SKIPPED
        info = ItemInfo(stage, None, job.payload.get("game_id"), status)
        await record_item(session, job, finish, info)

    # embed

    async def embed(self, job: ClaimedJob) -> StageResult:
        game_id = job.payload["game_id"]
        async with self.session_factory() as session:
            game = await session.get(Game, game_id)
            if game is None:
                raise PermanentError(f"игры {game_id} больше нет")
            if not await embedding_outdated(session, game_id, self.embedder.model):
                return StageResult(game_id, False)
            text = embedding_text(game)
        vector = await self.embedder.embed(text)
        async with self.session_factory() as session, session.begin():
            await save_embedding(session, game_id, vector, self.embedder.model, text_hash(text))
        return StageResult(game_id, True)

    async def _embed_finished(self, session: AsyncSession, job: ClaimedJob, finish: Finish):
        changed = finish.result.changed if finish.result else False
        status = RunItemStatus.UPDATED if changed else RunItemStatus.SKIPPED
        info = ItemInfo(RunItemStage.EMBED, None, job.payload.get("game_id"), status)
        await record_item(session, job, finish, info)
