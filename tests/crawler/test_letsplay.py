from datetime import timedelta

from sqlalchemy import func, select, update

from app.catalog.models import Game, Letsplay
from app.core.clock import now_tz
from app.core.config import settings
from app.crawler import stages as stages_module
from app.youtube.errors import NoSpeechError
from tests.conftest import FakeClient, FakeLetsplays, FakeSummarizer
from tests.crawler.test_pipeline import GAMES, items, run_pipeline


async def letsplay_row(session_factory) -> Letsplay:
    async with session_factory() as session:
        return await session.scalar(select(Letsplay))


async def test_letsplay_found_transcribed_and_concluded(db_engine, session_factory) -> None:
    letsplays = FakeLetsplays()
    run_id = await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=letsplays,
    )  # fmt: skip

    row = await letsplay_row(session_factory)
    assert row.status == "done"
    assert row.video_url == "https://www.youtube.com/watch?v=abc123def45"
    assert (row.views, row.duration_seconds, row.channel) == (1_000_000, 3600, "Канал")
    assert row.conclusion.startswith("Блогеру понравилась")
    assert (row.verdict, row.transcript_source, row.language) == ("positive", "captions", "ru")
    assert row.prompt_version == 1 and row.model == "fake-model"
    assert row.checked_at is not None and row.input_tokens
    assert ("elden-ring", "letsplay", "updated") in await items(session_factory, run_id)


async def test_same_video_not_transcribed_again(db_engine, session_factory, monkeypatch) -> None:
    letsplays = FakeLetsplays()
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)

    # Пора искать заново: тот же ролик, только просмотры обновятся
    async with session_factory() as session, session.begin():
        await session.execute(update(Letsplay).values(checked_at=func.now() - timedelta(days=31)))
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    run_id = await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays
    )

    assert letsplays.transcribed == ["abc123def45"]
    assert len(letsplays.searches) == 2
    assert ("elden-ring", "letsplay", "skipped") in await items(session_factory, run_id)


async def test_letsplay_not_due_before_refresh(db_engine, session_factory, monkeypatch) -> None:
    letsplays = FakeLetsplays()
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)
    assert len(letsplays.searches) == 1


async def test_not_found_recorded(db_engine, session_factory) -> None:
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=FakeLetsplays(video_id=None),
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert (row.status, row.video_url, row.conclusion) == ("not_found", None, None)


async def test_no_speech_marks_failed_and_keeps_game(
    db_engine, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    run_id = await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=FakeLetsplays(fail=NoSpeechError("тишина")),
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert row.status == "failed"
    assert "NoSpeechError" in row.error
    assert ("elden-ring", "letsplay", "failed") in await items(session_factory, run_id)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Game)) == 1


async def test_failure_keeps_previous_conclusion(db_engine, session_factory, monkeypatch) -> None:
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(), letsplays=FakeLetsplays()
    )
    async with session_factory() as session, session.begin():
        await session.execute(update(Letsplay).values(checked_at=func.now() - timedelta(days=31)))
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(),
        letsplays=FakeLetsplays(video_id="newvideo123", fail=NoSpeechError("тишина")),
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert row.status == "done" and row.video_id == "abc123def45"
    assert "NoSpeechError" in row.error


async def test_letsplay_stage_needs_llm(db_engine, session_factory) -> None:
    letsplays = FakeLetsplays()
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        letsplays=letsplays,
    )  # fmt: skip
    assert letsplays.searches == []
    assert await letsplay_row(session_factory) is None


def test_youtube_breaker_trips_on_bot_check() -> None:
    from app.crawler.breakers import judge_youtube
    from app.jobs.breaker import Verdict
    from app.youtube.errors import VideoUnavailableError, YouTubeBlockedError

    assert judge_youtube(YouTubeBlockedError("bot")) is Verdict.TRIP
    assert judge_youtube(VideoUnavailableError("private")) is Verdict.SUCCESS
    assert judge_youtube(None) is Verdict.SUCCESS
