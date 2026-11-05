import asyncio
import contextlib
import logging
import signal
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.crawler.heartbeat import beat
from app.crawler.runner import WORKER_NAME, fail_stale_runs, run_crawl
from app.db import SessionLocal, engine
from app.embeddings import FastEmbedder
from app.llm import build_summarizer
from app.models import RunTrigger
from app.scraper.client import MetacriticClient

log = logging.getLogger("worker")

HEARTBEAT_SECONDS = 15


async def main() -> None:
    stale = await fail_stale_runs(engine, SessionLocal)
    if stale:
        log.warning("marked %d stale runs as failed", stale)
    await beat(SessionLocal, WORKER_NAME, "idle")

    summarizer = build_summarizer()
    embedder = FastEmbedder()

    async with MetacriticClient() as client:

        async def crawl() -> None:
            await run_crawl(
                RunTrigger.SCHEDULE,
                client=client,
                engine=engine,
                session_factory=SessionLocal,
                summarizer=summarizer,
                embedder=embedder,
            )

        async def keepalive() -> None:
            await beat(SessionLocal, WORKER_NAME)

        scheduler = AsyncIOScheduler(timezone=settings.tz)
        scheduler.add_job(
            crawl,
            "interval",
            minutes=settings.crawl_interval_minutes,
            next_run_time=datetime.now(settings.tz) if settings.crawl_on_start else None,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
        scheduler.add_job(keepalive, "interval", seconds=HEARTBEAT_SECONDS)
        scheduler.start()
        log.info("worker started, crawl every %d min", settings.crawl_interval_minutes)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            await beat(SessionLocal, WORKER_NAME, "stopped")
            await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
