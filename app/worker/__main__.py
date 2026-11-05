import asyncio
import contextlib
import logging
import signal
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.crawl_requests import asyncpg_dsn, listen_for_requests, requester_from_payload
from app.crawler.heartbeat import beat
from app.crawler.runner import WORKER_NAME, fail_stale_runs, run_crawl
from app.db import SessionLocal, engine
from app.embeddings import FastEmbedder
from app.llm import build_summarizer
from app.models import RunTrigger
from app.scraper.client import MetacriticClient

log = logging.getLogger("worker")

HEARTBEAT_SECONDS = 15
CRAWL_JOB = "crawl"


async def main() -> None:
    stale = await fail_stale_runs(engine, SessionLocal)
    if stale:
        log.warning("marked %d stale runs as failed", stale)
    await beat(SessionLocal, WORKER_NAME, "idle")

    summarizer = build_summarizer()
    embedder = FastEmbedder()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    manual_tasks: set[asyncio.Task] = set()

    async def announce_next_run() -> None:
        job = scheduler.get_job(CRAWL_JOB)
        when = job.next_run_time if job else None
        task = f"следующий проход в {when:%H:%M}" if when else None
        await beat(SessionLocal, WORKER_NAME, "idle", task)

    async with MetacriticClient() as client:

        async def crawl(
            trigger: RunTrigger = RunTrigger.SCHEDULE, requested_by: int | None = None
        ) -> None:
            run_id = await run_crawl(
                trigger,
                client=client,
                engine=engine,
                session_factory=SessionLocal,
                summarizer=summarizer,
                embedder=embedder,
                requested_by=requested_by,
            )
            if run_id is not None:
                await announce_next_run()

        def on_manual_request(payload: str) -> None:
            requested_by = requester_from_payload(payload)
            log.info("manual crawl requested by user %s", requested_by)
            task = asyncio.create_task(crawl(RunTrigger.MANUAL, requested_by))
            manual_tasks.add(task)
            task.add_done_callback(manual_tasks.discard)

        async def keepalive() -> None:
            await beat(SessionLocal, WORKER_NAME)

        # next_run_time=None поставил бы задачу на паузу, передаём его только для старта сразу
        first_run = {"next_run_time": datetime.now(settings.tz)} if settings.crawl_on_start else {}
        scheduler.add_job(
            crawl,
            "interval",
            id=CRAWL_JOB,
            minutes=settings.crawl_interval_minutes,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            **first_run,
        )
        scheduler.add_job(keepalive, "interval", seconds=HEARTBEAT_SECONDS)
        scheduler.start()
        if not settings.crawl_on_start:
            await announce_next_run()
        log.info("worker started, crawl every %d min", settings.crawl_interval_minutes)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        listener = asyncio.create_task(
            listen_for_requests(asyncpg_dsn(settings.database_url), on_manual_request, stop)
        )
        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            await listener
            await beat(SessionLocal, WORKER_NAME, "stopped")
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
