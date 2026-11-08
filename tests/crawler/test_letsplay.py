from datetime import timedelta

from sqlalchemy import func, select, update

from app.catalog.models import Game, Letsplay
from app.core.clock import now_tz
from app.core.config import settings
from app.crawler import stages as stages_module
from app.youtube.errors import NoSpeechError
from tests.conftest import FakeClient, FakeLetsplays, FakeSummarizer
from tests.crawler.test_pipeline import GAMES, items, run_pipeline


async def job_statuses(session_factory) -> set[tuple[str, str]]:
    from app.jobs.models import Job

    async with session_factory() as session:
        rows = await session.execute(select(Job.kind, Job.status).where(Job.kind.like("letsplay%")))
    return {tuple(row) for row in rows}


async def letsplay_row(session_factory) -> Letsplay:
    async with session_factory() as session:
        return await session.scalar(select(Letsplay))


async def test_letsplay_found_transcribed_and_concluded(db_engine, session_factory) -> None:
    letsplays = FakeLetsplays()
    run_id = await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=letsplays,
    )  # fmt: skip
    # Проход закрылся без летсплеев: они идут в фоне
    assert not any(
        stage.startswith("letsplay") for _, stage, _ in await items(session_factory, run_id)
    )

    row = await letsplay_row(session_factory)
    assert row.status == "done"
    assert row.video_url == "https://www.youtube.com/watch?v=abc123def45"
    assert (row.views, row.duration_seconds, row.channel) == (1_000_000, 3600, "Канал")
    assert row.conclusion.startswith("Блогеру понравилась")
    assert (row.verdict, row.transcript_source, row.language) == ("positive", "captions", "ru")
    assert row.prompt_version == 1 and row.model == "fake-model"
    assert row.checked_at is not None and row.input_tokens
    assert await job_statuses(session_factory) == {
        ("letsplay", "done"),
        ("letsplay_conclude", "done"),
    }


async def test_same_video_not_transcribed_again(db_engine, session_factory, monkeypatch) -> None:
    letsplays = FakeLetsplays()
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)

    # Пора искать заново: тот же ролик, только просмотры обновятся
    async with session_factory() as session, session.begin():
        await session.execute(update(Letsplay).values(checked_at=func.now() - timedelta(days=31)))
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)

    assert letsplays.transcribed == ["abc123def45"]
    assert len(letsplays.searches) == 2
    assert await letsplay_row(session_factory) is not None


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
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=FakeLetsplays(fail=NoSpeechError("тишина")),
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert row.status == "failed"
    assert "NoSpeechError" in row.error
    assert ("letsplay", "dead") in await job_statuses(session_factory)
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


def test_transcribe_and_conclude_registered_independently(db_engine, session_factory) -> None:
    """worker-letsplay без LLM только расшифровывает, основной воркер без YouTube только
    пишет заключения"""
    from app.crawler.stages import CrawlStages
    from app.jobs.models import JobKind

    def kinds(**kw):
        stages = CrawlStages(
            client=FakeClient(), engine=db_engine, session_factory=session_factory,
            embedder=None, **kw,
        )  # fmt: skip
        return set(stages.job_types())

    letsplay_only = kinds(summarizer=None, letsplays=FakeLetsplays())
    llm_only = kinds(summarizer=FakeSummarizer(), letsplays=None)
    assert JobKind.LETSPLAY in letsplay_only and JobKind.LETSPLAY_CONCLUDE not in letsplay_only
    assert JobKind.LETSPLAY_CONCLUDE in llm_only and JobKind.LETSPLAY not in llm_only


async def test_llm_failure_does_not_transcribe_again(
    db_engine, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    letsplays = FakeLetsplays()
    client = FakeClient(new_releases=["elden-ring"], games=GAMES)
    await run_pipeline(
        db_engine, session_factory, client, FakeSummarizer(fail=True), letsplays=letsplays
    )
    row = await letsplay_row(session_factory)
    assert row.status == "failed" and row.transcript and row.conclusion is None
    statuses = await job_statuses(session_factory)
    assert ("letsplay", "done") in statuses and ("letsplay_conclude", "dead") in statuses

    # LLM снова доступна: заключение по сохранённой расшифровке, без скачивания и Whisper
    async with session_factory() as session, session.begin():
        await session.execute(update(Letsplay).values(updated_at=func.now() - timedelta(days=2)))
    later = now_tz() + timedelta(days=1)
    monkeypatch.setattr(stages_module, "now_tz", lambda: later)
    await run_pipeline(db_engine, session_factory, client, FakeSummarizer(), letsplays=letsplays)

    row = await letsplay_row(session_factory)
    assert row.status == "done" and row.conclusion
    assert letsplays.transcribed == ["abc123def45"]


def test_conclusion_waits_for_llm_breaker() -> None:
    from app.crawler.breakers import LLM, POLICY_BY_KIND, YOUTUBE
    from app.jobs.models import JobKind

    assert POLICY_BY_KIND[JobKind.LETSPLAY_CONCLUDE] is LLM
    assert POLICY_BY_KIND[JobKind.LETSPLAY] is YOUTUBE


def test_youtube_breaker_trips_on_bot_check() -> None:
    from app.crawler.breakers import judge_youtube
    from app.jobs.breaker import Verdict
    from app.youtube.errors import (
        VideoUnavailableError,
        YouTubeApiConfigError,
        YouTubeApiError,
        YouTubeBlockedError,
    )

    assert judge_youtube(YouTubeBlockedError("bot")) is Verdict.TRIP
    assert judge_youtube(YouTubeApiConfigError("keyInvalid")) is Verdict.TRIP

    from app.youtube.search import QuotaExceededError

    assert judge_youtube(QuotaExceededError("quota")) is Verdict.TRIP
    assert judge_youtube(YouTubeApiError(503, "x")) is Verdict.FAILURE
    assert judge_youtube(YouTubeApiError(404, "x")) is Verdict.SUCCESS
    assert judge_youtube(VideoUnavailableError("private")) is Verdict.SUCCESS
    assert judge_youtube(None) is Verdict.SUCCESS


async def test_next_video_when_most_popular_has_no_speech(db_engine, session_factory) -> None:
    letsplays = FakeLetsplays(
        videos=["silent01", "silent02", "talking3", "unused04"], silent={"silent01", "silent02"}
    )
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=letsplays,
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert (row.status, row.video_id) == ("done", "talking3")
    assert letsplays.transcribed == ["silent01", "silent02", "talking3"]


async def test_gives_up_after_max_candidates(db_engine, session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    monkeypatch.setattr(settings, "letsplay_max_candidates", 2)
    letsplays = FakeLetsplays(videos=["s1", "s2", "talking"], silent={"s1", "s2"})
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=letsplays,
    )  # fmt: skip
    row = await letsplay_row(session_factory)
    assert row.status == "failed" and "ни в одном из 2 роликов" in row.error
    assert letsplays.transcribed == ["s1", "s2"]


async def test_blocked_youtube_stops_without_trying_next(
    db_engine, session_factory, monkeypatch
) -> None:
    from app.youtube.errors import YouTubeBlockedError

    monkeypatch.setattr(settings, "job_max_attempts", 1)
    letsplays = FakeLetsplays(videos=["v1", "v2"], fail=YouTubeBlockedError("bot"))
    await run_pipeline(
        db_engine, session_factory, FakeClient(new_releases=["elden-ring"], games=GAMES),
        FakeSummarizer(), letsplays=letsplays,
    )  # fmt: skip
    assert letsplays.transcribed == ["v1"]
