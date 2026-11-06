"""Процесс воркера: пулы задач по видам, расписание, LISTEN, сборщик аренд, heartbeat"""

import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from prometheus_client import start_http_server
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import asyncpg_dsn, default_database
from app.crawler.breakers import Breakers
from app.crawler.heartbeat import beat, cleanup_heartbeats, live_workers
from app.crawler.models import Run, RunStatus, RunTrigger
from app.crawler.reparse import ReparseReport, refresh_derived, reparse_games
from app.crawler.retention import CleanupReport, cleanup
from app.crawler.stages import CrawlStages, Derived, enqueue_collect
from app.enrichment.embeddings import FastEmbedder
from app.enrichment.llm import build_summarizer
from app.jobs.listener import listen
from app.jobs.models import ACTIVE_STATUSES, Job, JobKind
from app.jobs.pool import JobPool, JobType
from app.jobs.queue import CHANNEL, reap_expired
from app.scraper.client import MetacriticClient

HEARTBEAT_SECONDS = 15
REAPER_SECONDS = 60
COLLECT_SCHEDULE_JOB = "collect"
CLEANUP_JOB = "cleanup"

# Группы видов задач со своими лимитами: у Metacritic, LLM и эмбеддингов разные
# ограничения, цены и характер сбоев
POOL_GROUPS = (
    ("metacritic", (JobKind.COLLECT, JobKind.SCRAPE_GAME), "scrape_concurrency"),
    ("summarize", (JobKind.SUMMARIZE,), "summary_concurrency"),
    ("embed", (JobKind.EMBED,), "embed_concurrency"),
    ("letsplay", (JobKind.LETSPLAY,), "letsplay_concurrency"),
)


def build_pools(
    types: dict[str, JobType],
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    **pool_kwargs,
) -> list[JobPool]:
    """Пул на группу; группа без обработчиков (пока letsplay) не запускается"""
    pools = []
    for name, kinds, limit_setting in POOL_GROUPS:
        group = {kind: types[kind] for kind in kinds if kind in types}
        if group:
            pools.append(
                JobPool(
                    name,
                    group,
                    getattr(settings, limit_setting),
                    session_factory,
                    worker_id,
                    lease=settings.job_lease_seconds,
                    poll=settings.job_poll_seconds,
                    **pool_kwargs,
                )
            )
    return pools


class Worker:
    def __init__(
        self,
        stages: CrawlStages,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: str,
        pool_kwargs: dict | None = None,
        dsn: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.dsn = dsn or asyncpg_dsn(settings.database_url)
        self.worker_id = worker_id
        breakers = Breakers(session_factory)
        kwargs = {"gate": breakers.gate, "on_outcome": breakers.outcome, **(pool_kwargs or {})}
        self.pools = build_pools(stages.job_types(), session_factory, worker_id, **kwargs)
        self.stop = asyncio.Event()
        self.next_collect: datetime | None = None

    def wake_all(self, _payload: str = "") -> None:
        for pool in self.pools:
            pool.wake.set()

    def held(self) -> list[str]:
        return [job for pool in self.pools for job in pool.held()]

    async def heartbeat(self, status: str | None = None) -> None:
        held = self.held()
        if status is None:
            status = "running" if held else "idle"
        if held:
            task = ", ".join(held)
        elif self.next_collect and status != "stopped":
            task = f"следующий проход в {self.next_collect:%H:%M}"
        else:
            task = None
        await beat(self.session_factory, self.worker_id, status, task)
        if settings.worker_health_file:
            await asyncio.to_thread(Path(settings.worker_health_file).touch)

    async def schedule_collect(self) -> None:
        async with self.session_factory() as session, session.begin():
            job_id = await enqueue_collect(session, RunTrigger.SCHEDULE)
        logger.info("collect по расписанию: {}", job_id or "уже в очереди")

    async def cleanup(self) -> None:
        try:
            await cleanup(self.session_factory)
        except Exception:
            logger.exception("очистка старых данных")

    async def _reaper(self) -> None:
        while not self.stop.is_set():
            try:
                async with self.session_factory() as session, session.begin():
                    if returned := await reap_expired(session):
                        logger.warning("сборщик: вернул {} задач с истёкшей арендой", returned)
                        self.wake_all()
            except Exception:
                logger.exception("сборщик аренд")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), REAPER_SECONDS)

    async def run(self, *, schedule: bool = True) -> None:
        await cleanup_heartbeats(self.session_factory)
        await self.heartbeat("idle")
        scheduler = AsyncIOScheduler(timezone=settings.tz)
        if schedule:
            # next_run_time=None поставил бы задачу на паузу, передаём его только для старта сразу
            first = {"next_run_time": datetime.now(settings.tz)} if settings.crawl_on_start else {}
            scheduler.add_job(
                self.schedule_collect,
                "interval",
                id=COLLECT_SCHEDULE_JOB,
                minutes=settings.crawl_interval_minutes,
                coalesce=True,
                misfire_grace_time=600,
                **first,
            )
            scheduler.add_job(
                self.cleanup,
                "cron",
                id=CLEANUP_JOB,
                hour=settings.cleanup_hour,
                coalesce=True,
                misfire_grace_time=3600,
            )
        scheduler.add_job(self.heartbeat, "interval", seconds=HEARTBEAT_SECONDS)
        scheduler.start()
        if job := scheduler.get_job(COLLECT_SCHEDULE_JOB):
            self.next_collect = job.next_run_time

        helpers = [
            asyncio.create_task(listen(self.dsn, CHANNEL, self.wake_all, self.stop)),
            asyncio.create_task(self._reaper()),
        ]
        pool_tasks = [asyncio.create_task(pool.run(self.stop)) for pool in self.pools]
        logger.info("воркер {}: пулы {}", self.worker_id, [p.name for p in self.pools])
        try:
            await self.stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.gather(*pool_tasks, return_exceptions=True)
            grace = settings.worker_shutdown_grace_seconds
            await asyncio.gather(*(pool.drain(grace) for pool in self.pools))
            await asyncio.gather(*helpers, return_exceptions=True)
            await self.heartbeat("stopped")


def build_stages(client: MetacriticClient) -> CrawlStages:
    db = default_database()
    return CrawlStages(
        client=client,
        engine=db.engine,
        session_factory=db.session_factory,
        summarizer=build_summarizer(),
        embedder=FastEmbedder(),
    )


async def run_worker() -> None:
    """Обработка очереди и проход по расписанию до SIGTERM"""
    if settings.worker_metrics_port:
        # Отдельный поток: опрос Prometheus не зависит от занятости цикла событий
        start_http_server(settings.worker_metrics_port)
        logger.info("метрики воркера на порту {}", settings.worker_metrics_port)
    async with MetacriticClient() as client:
        worker = Worker(
            build_stages(client), default_database().session_factory, settings.worker_identity
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, worker.stop.set)
        await worker.run()


async def wait_for_run(
    session_factory: async_sessionmaker[AsyncSession], collect_id: int
) -> int | None:
    """Ждёт, пока collect завершится, а его проход закроется. Возвращает id прохода"""
    while True:
        async with session_factory() as session:
            job = await session.get(Job, collect_id)
            run_status = (
                await session.scalar(select(Run.status).where(Run.id == job.run_id))
                if job.run_id
                else None
            )
        if job.status not in ACTIVE_STATUSES and run_status != RunStatus.RUNNING:
            return job.run_id
        await asyncio.sleep(0.5)


@dataclass
class CrawlOutcome:
    run_id: int | None
    # Живые воркеры, которым отдан проход; пусто, если он шёл в этом процессе
    delegated_to: list[str] = field(default_factory=list)
    already_running: bool = False


async def crawl_once(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    client_factory=MetacriticClient,
) -> CrawlOutcome:
    """Один проход прямо сейчас. Если работает воркер, collect ставится ему и команда ждёт
    конца прохода: второй обработчик в этом процессе удвоил бы частоту запросов к Metacritic
    """
    session_factory = session_factory or default_database().session_factory
    async with session_factory() as session, session.begin():
        workers = await live_workers(session)
        collect_id = await enqueue_collect(session, RunTrigger.MANUAL)
    if collect_id is None:
        return CrawlOutcome(None, workers, already_running=True)
    if workers:
        return CrawlOutcome(await wait_for_run(session_factory, collect_id), workers)
    async with client_factory() as client:
        worker = Worker(build_stages(client), session_factory, f"{settings.worker_identity}:crawl")
        task = asyncio.create_task(worker.run(schedule=False))
        try:
            return CrawlOutcome(await wait_for_run(session_factory, collect_id))
        finally:
            worker.stop.set()
            await task


async def reparse(slug: str | None = None) -> ReparseReport:
    """Пересборка из сырых ответов; резюме и эмбеддинги ставятся в очередь воркеру"""
    summarizer = build_summarizer()
    return await reparse_games(
        default_database().session_factory,
        slug=slug,
        summary_model=summarizer.model if summarizer else None,
        embed_model=settings.embedding_model,
    )


async def cleanup_now() -> CleanupReport:
    return await cleanup(default_database().session_factory)


async def refresh() -> Derived:
    """Резюме и эмбеддинги, устаревшие после смены модели или промпта, ставятся воркеру"""
    summarizer = build_summarizer()
    return await refresh_derived(
        default_database().session_factory,
        summary_model=summarizer.model if summarizer else None,
        embed_model=settings.embedding_model,
    )
