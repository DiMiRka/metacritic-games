from datetime import timedelta

from sqlalchemy import func, select, update

from app.catalog.models import Game
from app.crawler import retention
from app.crawler.models import CrawlState, RawPage, Run, RunItem
from app.crawler.retention import cleanup
from app.jobs.models import Job
from app.jobs.queue import enqueue

OLD = func.now() - timedelta(days=40)


async def seed(session_factory) -> dict[str, int]:
    async with session_factory() as session, session.begin():
        game = Game(slug="g", title="G", genres=[])
        old_run = Run(trigger="schedule", status="success")
        running = Run(trigger="manual", status="running")
        fresh_run = Run(trigger="schedule", status="success")
        session.add_all([game, old_run, running, fresh_run])
        await session.flush()
        session.add(RunItem(run_id=old_run.id, slug="g", stage="scrape", status="updated"))
        await session.execute(
            update(Run)
            .where(Run.id.in_([old_run.id, running.id]))
            .values(started_at=OLD, finished_at=OLD)
        )
        done = await enqueue(session, "embed", {"n": 1}, run_id=old_run.id)
        queued = await enqueue(session, "embed", {"n": 2})
        fresh = await enqueue(session, "embed", {"n": 3})
        await session.execute(
            update(Job).where(Job.id == done).values(status="done", finished_at=OLD)
        )
        await session.execute(update(Job).where(Job.id == queued).values(created_at=OLD))
        await session.execute(update(Job).where(Job.id == fresh).values(status="dead"))
        today = await session.scalar(select(func.current_date()))
        session.add_all(
            [
                CrawlState(day=today - timedelta(days=40), phase="browse", next_page=3),
                CrawlState(day=today, phase="browse", next_page=1),
            ]
        )
        for n, age in [(1, 200), (2, 100), (3, 0)]:
            page = RawPage(game_id=game.id, content_hash=str(n), body=b"x", size=1)
            session.add(page)
            await session.flush()
            await session.execute(
                update(RawPage)
                .where(RawPage.id == page.id)
                .values(last_seen_at=func.now() - timedelta(days=age))
            )
        return {"game": game.id, "running": running.id, "fresh_run": fresh_run.id}


async def test_cleanup_keeps_active_and_recent(session_factory, monkeypatch) -> None:
    ids = await seed(session_factory)
    monkeypatch.setattr(retention, "CHUNK", 1)

    report = await cleanup(session_factory, retention_days=30, raw_retention_days=90)

    assert (report.jobs, report.runs, report.crawl_states, report.raw_pages) == (1, 1, 1, 2)
    async with session_factory() as session:
        runs = set((await session.scalars(select(Run.id))).all())
        jobs = (await session.scalars(select(Job.status).order_by(Job.id))).all()
        items = await session.scalar(select(func.count()).select_from(RunItem))
        raw = (await session.scalars(select(RawPage.content_hash))).all()
        states = await session.scalar(select(func.count()).select_from(CrawlState))
    assert runs == {ids["running"], ids["fresh_run"]}
    assert jobs == ["queued", "dead"]
    assert (items, states) == (0, 1)
    assert raw == ["3"]


async def test_latest_raw_page_survives_any_age(session_factory) -> None:
    ids = await seed(session_factory)
    async with session_factory() as session, session.begin():
        await session.execute(update(RawPage).values(last_seen_at=func.now() - timedelta(days=365)))
        await session.execute(
            update(RawPage)
            .where(RawPage.content_hash == "3")
            .values(last_seen_at=func.now() - timedelta(days=300))
        )

    report = await cleanup(session_factory, retention_days=0, raw_retention_days=1)

    assert (report.jobs, report.runs, report.raw_pages) == (0, 0, 2)
    async with session_factory() as session:
        raw = (
            await session.scalars(
                select(RawPage.content_hash).where(RawPage.game_id == ids["game"])
            )
        ).all()
    assert raw == ["3"]
