from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, update

from app.catalog.models import Game, ReviewSummary
from app.core.clock import now_tz
from app.core.config import settings
from app.crawler.models import Run, RunItem, WorkerHeartbeat
from app.jobs.models import Job
from app.jobs.queue import enqueue
from app.monitoring.snapshot import snapshot, worker_alive
from tests.conftest import create_user


async def seed(session_factory) -> int:
    admin_id = await create_user(session_factory, "boss")
    now = now_tz()
    async with session_factory() as session, session.begin():
        old = Run(
            trigger="schedule",
            status="partial",
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=28),
        )
        current = Run(trigger="manual", status="running", requested_by=admin_id)
        session.add_all([old, current])
        await session.flush()
        session.add_all(
            [
                RunItem(run_id=old.id, slug="a", stage="scrape", status="created"),
                RunItem(run_id=old.id, slug="b", stage="scrape", status="updated"),
                RunItem(run_id=old.id, slug="c", stage="scrape", status="retrying", error="503"),
                RunItem(run_id=old.id, slug="c", stage="scrape", status="failed", error="dead"),
                RunItem(
                    run_id=old.id, slug="a", stage="summarize_user", status="failed", error="llm"
                ),
            ]
        )
        for kind, status in [
            ("scrape_game", "queued"),
            ("scrape_game", "queued"),
            ("summarize", "running"),
            ("embed", "dead"),
        ]:
            job_id = await enqueue(session, kind, {"n": 1})
            await session.execute(update(Job).where(Job.id == job_id).values(status=status))
        expired = await enqueue(session, "embed", {"game_id": 1})
        await session.execute(
            update(Job)
            .where(Job.id == expired)
            .values(
                status="running",
                locked_by="dead-worker",
                locked_until=func.now() - timedelta(minutes=1),
            )
        )
        game = Game(slug="g", title="G", genres=[])
        session.add(game)
        await session.flush()
        session.add(
            ReviewSummary(
                game_id=game.id,
                source="user",
                reviews_hash="h",
                model="m",
                input_tokens=1_000_000,
                output_tokens=200_000,
            )
        )
        session.add(WorkerHeartbeat(worker="w1", status="running", current_task="embed#3"))
        session.add(
            WorkerHeartbeat(worker="w2", status="idle", updated_at=now - timedelta(hours=1))
        )
        return current.id


async def test_snapshot(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.1)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.4)
    current_id = await seed(session_factory)

    async with session_factory() as session:
        data = await snapshot(session, config=settings)
        alive = await worker_alive(session)

    assert alive is True
    # w2 в старом формате имени, без роли, и давно молчит: прежняя сборка, в списке не нужна
    assert [(w.worker, w.alive) for w in data.workers] == [("w1", True)]
    assert data.workers_hidden == 1
    assert data.current_run.id == current_id
    assert data.current_run.requested_by == "boss"
    old = data.runs[1]
    assert (old.processed, old.created, old.updated, old.errors_count) == (3, 1, 1, 2)
    assert [e.error for e in data.errors] == ["llm", "dead", "503"]
    queue = {q.kind: (q.queued, q.running, q.dead) for q in data.queue}
    assert queue["scrape_game"] == (2, 0, 0)
    assert queue["summarize"] == (0, 1, 0)
    assert queue["embed"] == (0, 1, 1)
    assert [j.locked_by for j in data.expired] == ["dead-worker"]
    assert [j.kind for j in data.dead] == ["embed"]
    assert data.collect_active is False
    assert data.counters.errors_today == 2
    assert data.counters.llm_input_tokens_today == 1_000_000
    assert data.counters.llm_cost_today == pytest.approx(0.18)


async def test_worker_alive_ignores_stopped(session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(WorkerHeartbeat(worker="w1", status="stopped"))
    async with session_factory() as session:
        assert await worker_alive(session) is False


def test_old_containers_of_same_role_hidden() -> None:
    """После деплоя у контейнеров новый hostname: прежние экземпляры той же роли прячутся,
    а роль, у которой живых нет, показывается последним замолчавшим экземпляром"""
    from types import SimpleNamespace

    from app.monitoring.snapshot import visible_workers

    now = now_tz()
    fresh = now - timedelta(seconds=45)

    def beat(worker, age_minutes, status="idle"):
        return SimpleNamespace(
            worker=worker,
            status=status,
            current_task=None,
            next_collect_at=None,
            updated_at=now - timedelta(minutes=age_minutes),
        )

    rows = [
        beat("metacritic,summarize,embed@aaa111:1", 900),
        beat("metacritic,summarize,embed@bbb222:1", 0),
        beat("letsplay@ccc333:1", 900),
        beat("letsplay@ddd444:1", 30),
        beat("0febe0c38c98:1", 900),
    ]
    shown, hidden = visible_workers(rows, fresh)

    assert [(w.title, w.host, w.alive) for w in shown] == [
        ("Основной воркер", "bbb222", True),
        ("Воркер летсплеев", "ddd444", False),
    ]
    assert shown[0].duties == "обход Metacritic, резюме, переводы и заключения LLM, эмбеддинги"
    assert hidden == 3


def test_worker_identity_names_its_pools(monkeypatch) -> None:
    from app.monitoring.labels import describe_worker

    monkeypatch.setattr(settings, "worker_id", "")
    monkeypatch.setattr(settings, "worker_pools", "letsplay")
    identity = settings.worker_identity
    assert identity.startswith("letsplay@") and ":" in identity
    assert describe_worker(identity).title == "Воркер летсплеев"

    monkeypatch.setattr(settings, "worker_pools", "")
    assert settings.worker_identity.startswith("all@")
    assert describe_worker("all@host:1").duties == "все виды задач"


def test_breaker_kinds_match_policies() -> None:
    """Подписи монитора повторяют POLICY_BY_KIND: в web его не импортировать"""
    from app.crawler.breakers import POLICY_BY_KIND
    from app.monitoring.labels import BREAKER_KINDS, BREAKER_LABELS

    expected: dict[str, set[str]] = {}
    for kind, policy in POLICY_BY_KIND.items():
        expected.setdefault(policy.name, set()).add(str(kind))
    assert {name: set(kinds) for name, kinds in BREAKER_KINDS.items()} == expected
    assert set(BREAKER_LABELS) == set(expected)


async def test_breakers_explain_state(session_factory) -> None:
    from datetime import UTC, datetime

    from app.jobs.breaker import Verdict, record
    from app.jobs.queue import claim, complete
    from app.monitoring.snapshot import snapshot

    async with session_factory() as session, session.begin():
        await enqueue(session, "translate", {"game_id": 1})
    async with session_factory() as session, session.begin():
        [job] = await claim(session, "w", ["translate"], 1, 60)
    async with session_factory() as session, session.begin():
        await complete(session, job.id, "w")

    youtube = SimpleNamespace(
        name="youtube",
        threshold=lambda: 5,
        open_seconds=lambda: 1800,
        probe_seconds=lambda: 60,
    )
    reset = datetime.now(UTC) + timedelta(hours=2)
    reason = "QuotaExceededError: квота YouTube Data API исчерпана"
    await record(session_factory, youtube, Verdict.TRIP, reset, reason)

    async with session_factory() as session:
        data = await snapshot(session, config=settings)
    breakers = {b.name: b for b in data.breakers}

    # Все сервисы видны, даже если сбоев ещё не было
    assert list(breakers) == ["metacritic", "llm", "youtube"]
    assert (breakers["llm"].state, breakers["llm"].title) == ("closed", "LLM")
    assert breakers["llm"].last_done_at is not None
    assert breakers["metacritic"].last_done_at is None
    assert breakers["youtube"].state == "open"
    assert breakers["youtube"].last_error == reason


def test_all_silent_old_format_still_shows_last() -> None:
    """Пустой список выглядел бы как «воркер ни разу не запускался»"""
    from app.monitoring.snapshot import visible_workers

    now = now_tz()
    rows = [
        SimpleNamespace(
            worker=name, status="idle", current_task=None, next_collect_at=None,
            updated_at=now - timedelta(minutes=age),
        )
        for name, age in (("old-a:1", 90), ("old-b:1", 30))
    ]  # fmt: skip
    shown, hidden = visible_workers(rows, now - timedelta(seconds=45))
    assert [(w.worker, w.alive) for w in shown] == [("old-b:1", False)]
    assert hidden == 1
