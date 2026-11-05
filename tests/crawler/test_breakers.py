import asyncio
from datetime import timedelta

import anthropic
import httpx
import openai
import pytest
from sqlalchemy import select

from app.core.clock import now_tz
from app.core.config import settings
from app.core.db import asyncpg_dsn
from app.crawler.breakers import judge_llm, judge_metacritic
from app.crawler.processor import GameNotFoundError, ScrapeFormatError
from app.crawler.stages import CrawlStages, enqueue_collect
from app.jobs.breaker import Verdict
from app.jobs.models import Breaker, Job
from app.worker.main import Worker
from tests.conftest import FakeClient, FakeEmbedder, FakeSummarizer

REQUEST = httpx.Request("GET", "https://x.test/")
GAMES = {"elden-ring": "composer_elden-ring.json"}


def http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "x", request=REQUEST, response=httpx.Response(status, request=REQUEST)
    )


def sdk_error(module, cls: str, status: int, message: str = "x"):
    return getattr(module, cls)(
        message, response=httpx.Response(status, request=REQUEST), body=None
    )


@pytest.mark.parametrize(
    ("error", "verdict"),
    [
        (None, Verdict.SUCCESS),
        (ScrapeFormatError("формат"), Verdict.FAILURE),
        (http_error(503), Verdict.FAILURE),
        (http_error(429), Verdict.FAILURE),
        (http_error(404), Verdict.SUCCESS),
        (GameNotFoundError("x"), Verdict.SUCCESS),
        (httpx.ConnectError("x", request=REQUEST), Verdict.NEUTRAL),
    ],
)
def test_judge_metacritic(error, verdict) -> None:
    assert judge_metacritic(error) == verdict


@pytest.mark.parametrize(
    ("error", "verdict"),
    [
        (None, Verdict.SUCCESS),
        (sdk_error(anthropic, "AuthenticationError", 401), Verdict.TRIP),
        (sdk_error(openai, "PermissionDeniedError", 403), Verdict.TRIP),
        (sdk_error(openai, "RateLimitError", 429, "You exceeded your current quota"), Verdict.TRIP),
        (sdk_error(anthropic, "RateLimitError", 429, "rate limited"), Verdict.FAILURE),
        (anthropic.APIConnectionError(request=REQUEST), Verdict.NEUTRAL),
        (ValueError("ответ не прошёл проверку"), Verdict.SUCCESS),
    ],
)
def test_judge_llm(error, verdict) -> None:
    assert judge_llm(error) == verdict


@pytest.fixture(autouse=True)
def fast_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings, "job_poll_seconds", 0.05)
    monkeypatch.setattr(settings, "scrape_concurrency", 1)
    monkeypatch.setattr(settings, "summary_concurrency", 1)


class BrokenFormatClient(FakeClient):
    async def game(self, slug: str):
        return {"components": [{"meta": {"componentName": "product"}, "data": {"item": {
            "slug": slug, "platforms": "не список"}}}]}  # fmt: skip


class RevokedKeySummarizer(FakeSummarizer):
    async def summarize(self, title, source, reviews):
        self.calls.append((title, source, len(reviews)))
        raise sdk_error(anthropic, "AuthenticationError", 401, "invalid x-api-key")


async def run_until(db_engine, session_factory, stages, predicate) -> None:
    async with session_factory() as session, session.begin():
        await enqueue_collect(session, "manual")
    dsn = asyncpg_dsn(db_engine.url.render_as_string(hide_password=False))
    worker = Worker(stages, session_factory, "test-worker", dsn=dsn)
    task = asyncio.create_task(worker.run(schedule=False))
    try:
        for _ in range(200):
            async with session_factory() as session:
                jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
                breakers = {b.name: b for b in (await session.scalars(select(Breaker))).all()}
            if predicate(jobs, breakers):
                return jobs, breakers
            await asyncio.sleep(0.05)
        raise AssertionError("не дождались нужного состояния")
    finally:
        worker.stop.set()
        await task


async def test_metacritic_breaker_defers_scrapes(db_engine, session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "breaker_metacritic_threshold", 2)
    client = BrokenFormatClient(new_releases=["a", "b", "c", "d"])
    stages = CrawlStages(
        client=client, engine=db_engine, session_factory=session_factory,
        summarizer=None, embedder=None,
    )  # fmt: skip

    later = now_tz() + timedelta(minutes=5)

    def settled(jobs, breakers):
        scrapes = [j for j in jobs if j.kind == "scrape_game"]
        deferred = [
            j for j in scrapes if j.status == "queued" and j.attempts == 0 and j.run_after > later
        ]
        dead = [j for j in scrapes if j.status == "dead"]
        return len(dead) == 2 and len(deferred) == 2

    jobs, breakers = await run_until(db_engine, session_factory, stages, settled)

    assert breakers["metacritic"].state == "open"
    assert all("ScrapeFormatError" in j.last_error for j in jobs if j.status == "dead")


async def test_llm_breaker_does_not_block_scrape_and_embed(db_engine, session_factory) -> None:
    summarizer = RevokedKeySummarizer()
    stages = CrawlStages(
        client=FakeClient(new_releases=["elden-ring"], games=GAMES),
        engine=db_engine,
        session_factory=session_factory,
        summarizer=summarizer,
        embedder=FakeEmbedder(),
    )

    def settled(jobs, breakers):
        by_kind = {}
        for job in jobs:
            by_kind.setdefault(job.kind, []).append(job.status)
        return (
            by_kind.get("scrape_game") == ["done"]
            and by_kind.get("embed") == ["done"]
            and sorted(by_kind.get("summarize", [])) == ["dead", "queued"]
        )

    jobs, breakers = await run_until(db_engine, session_factory, stages, settled)

    assert breakers["llm"].state == "open"
    assert len(summarizer.calls) == 1
    waiting = next(j for j in jobs if j.kind == "summarize" and j.status == "queued")
    assert waiting.attempts == 0
