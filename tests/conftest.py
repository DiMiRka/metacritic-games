import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Таблицы всех доменов должны попасть в Base.metadata
import app.auth.models
import app.catalog.models
import app.crawler.models
import app.jobs.models
from app.auth.models import User
from app.auth.service import create_session, csrf_token_for, hash_password
from app.catalog.models import EMBEDDING_DIM, ReviewSource
from app.core.config import settings
from app.core.db import get_session, get_session_factory
from app.core.db_roles import create_functions
from app.core.models import Base
from app.enrichment.llm import ReviewDigest, Summary
from app.web.app import app
from app.web.deps import SESSION_COOKIE
from app.web.middleware.ratelimit import LIMITERS

PASSWORD = "correct horse battery staple"
ORIGIN = "http://test"

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _test_db_url() -> URL:
    if explicit := os.environ.get("TEST_DATABASE_URL"):
        return make_url(explicit)
    url = make_url(settings.database_url)
    return url.set(database=f"{url.database}_test")


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    url = _test_db_url()
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:
        if os.environ.get("REQUIRE_DB"):
            raise
        pytest.skip(f"Postgres недоступен: {exc}")
    finally:
        await admin.dispose()

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await create_functions(conn)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return async_sessionmaker(db_engine, expire_on_commit=False)


class FakeClient:
    """Подменяет MetacriticClient: отдаёт сохранённые ответы вместо сети."""

    def __init__(
        self,
        new_releases: list[str] | None = None,
        browse: dict[int, list[str]] | None = None,
        games: dict[str, str] | None = None,
    ) -> None:
        self._new_releases = new_releases or []
        self._browse = browse or {}
        self._games = games or {}
        self.user_stats_calls: list[tuple[str, str]] = []

    @staticmethod
    def _listing(slugs: list[str]) -> dict[str, Any]:
        return {"data": {"items": [{"slug": s} for s in slugs]}}

    async def new_releases(self) -> dict[str, Any]:
        return self._listing(self._new_releases)

    async def browse(self, page: int) -> dict[str, Any]:
        return self._listing(self._browse.get(page, []))

    async def game(self, slug: str) -> dict[str, Any]:
        return load_fixture(self._games.get(slug, "composer_not-found.json"))

    async def user_stats(self, slug: str, platform: str) -> dict[str, Any]:
        self.user_stats_calls.append((slug, platform))
        return load_fixture("user_stats_elden-ring_pc.json")

    async def critic_reviews(self, slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return load_fixture("critic_reviews_elden-ring.json")["data"]["items"][:limit]

    async def user_reviews(self, slug: str, limit: int = 30) -> list[dict[str, Any]]:
        return load_fixture("user_reviews_elden-ring.json")["data"]["items"][:limit]


class FakeSummarizer:
    def __init__(self, model: str = "fake-model", fail: bool = False) -> None:
        self.model = model
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    async def summarize(self, title: str, source: ReviewSource, reviews: list) -> Summary:
        self.calls.append((title, source, len(reviews)))
        if self.fail:
            raise RuntimeError("LLM недоступна")
        digest = ReviewDigest(likes=[f"{source} плюс"], dislikes=[f"{source} минус"])
        return Summary(digest, input_tokens=100 * len(reviews), output_tokens=50)


class FakeEmbedder:
    """Детерминированный вектор: одинаковый текст даёт одинаковый эмбеддинг"""

    def __init__(self, model: str = "fake-embed") -> None:
        self.model = model
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        seed = hashlib.sha256(text.encode()).digest()
        return [seed[i % len(seed)] / 255 + 0.01 for i in range(EMBEDDING_DIM)]


async def create_user(
    session_factory, login: str, role: str = "admin", password: str = PASSWORD, active=True
) -> int:
    async with session_factory() as db, db.begin():
        user = User(login=login, password_hash=hash_password(password), role=role, is_active=active)
        db.add(user)
        await db.flush()
        return user.id


async def login_as(session_factory, user_id: int) -> str:
    async with session_factory() as db, db.begin():
        user = await db.get(User, user_id)
        return await create_session(db, user, "127.0.0.1", "pytest")


@pytest.fixture
def caplog(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Строки loguru тоже попадают в caplog"""
    handler_id = logger.add(caplog.handler, format="{message}", level=0)
    yield caplog
    logger.remove(handler_id)


@pytest.fixture
def log_records() -> list:
    """Записи loguru со всеми полями контекста"""
    records: list = []
    handler_id = logger.add(lambda message: records.append(message.record), level=0)
    yield records
    logger.remove(handler_id)


@pytest.fixture(autouse=True)
def reset_rate_limits() -> None:
    for limiter in LIMITERS:
        limiter.reset()


@pytest.fixture
def make_client(session_factory) -> Callable:
    """Клиент к приложению: анонимный или с готовой сессией нужной роли"""

    async def override() -> AsyncIterator:
        async with session_factory() as session:
            yield session

    @contextlib.asynccontextmanager
    async def factory(role: str | None = None, ip: str = "127.0.0.1"):
        app.dependency_overrides[get_session] = override
        app.dependency_overrides[get_session_factory] = lambda: session_factory
        headers = {"Origin": ORIGIN}
        cookies = {}
        if role:
            user_id = await create_user(session_factory, f"{role}-{ip}", role)
            token = await login_as(session_factory, user_id)
            cookies[SESSION_COOKIE] = token
            headers["X-CSRF-Token"] = csrf_token_for(token)
        transport = ASGITransport(app=app, client=(ip, 50000))
        async with AsyncClient(
            transport=transport, base_url=ORIGIN, headers=headers, cookies=cookies
        ) as http:
            yield http
        app.dependency_overrides.clear()

    return factory
