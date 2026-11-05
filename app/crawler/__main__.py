import asyncio
import logging

from app.crawler.runner import run_crawl
from app.db import SessionLocal, engine
from app.embeddings import FastEmbedder
from app.llm import build_summarizer
from app.models import RunTrigger
from app.scraper.client import MetacriticClient


async def main() -> None:
    async with MetacriticClient() as client:
        run_id = await run_crawl(
            RunTrigger.MANUAL,
            client=client,
            engine=engine,
            session_factory=SessionLocal,
            summarizer=build_summarizer(),
            embedder=FastEmbedder(),
        )
    await engine.dispose()
    print("crawl already running" if run_id is None else f"run {run_id} finished")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
