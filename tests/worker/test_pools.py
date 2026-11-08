from app.core.config import settings
from app.jobs.models import JobKind
from app.jobs.pool import JobType
from app.worker.main import build_pools


async def handler(job):
    return None


def pools(session_factory) -> list[str]:
    types = {kind: JobType(handler) for kind in JobKind}
    return [p.name for p in build_pools(types, session_factory, "w")]


def test_worker_pools_setting(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_pools", "")
    assert pools(session_factory) == ["metacritic", "summarize", "embed", "letsplay"]
    monkeypatch.setattr(settings, "worker_pools", "letsplay")
    assert pools(session_factory) == ["letsplay"]
    monkeypatch.setattr(settings, "worker_pools", "metacritic, summarize,embed")
    assert pools(session_factory) == ["metacritic", "summarize", "embed"]


def test_conclusion_runs_in_summarize_pool(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_pools", "summarize")
    types = {kind: JobType(handler) for kind in JobKind}
    (pool,) = build_pools(types, session_factory, "w")
    assert set(pool.kinds) == {JobKind.SUMMARIZE, JobKind.LETSPLAY_CONCLUDE}
