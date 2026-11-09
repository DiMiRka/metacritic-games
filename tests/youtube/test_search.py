import httpx
import pytest

from app.youtube.search import (
    Candidate,
    mentions_game,
    parse_iso_duration,
    pick_most_popular,
    search,
)


def video(video_id, title, views, minutes):
    return Candidate(video_id, title, "ch", views, minutes * 60)


def test_mentions_game_ignores_case_punctuation_and_stop_words() -> None:
    assert mentions_game("ELDEN RING - Let's Play Part 1", "Elden Ring")
    assert (
        mentions_game(
            "Zelda: Tears of the Kingdom walkthrough", "The Legend of Zelda: Tears of the Kingdom"
        )
        is False
    )
    assert mentions_game(
        "Legend of Zelda Tears of the Kingdom #1", "The Legend of Zelda: Tears of the Kingdom"
    )
    assert mentions_game("Baldur's Gate 3 letsplay", "Baldur's Gate 3")
    assert not mentions_game("Dark Souls 3 let's play", "Elden Ring")


def test_pick_most_popular_long_relevant_video() -> None:
    candidates = [
        video("trailer", "Elden Ring trailer", 50_000_000, 3),
        video("other", "Dark Souls let's play", 9_000_000, 60),
        video("top", "Elden Ring let's play #1", 2_000_000, 60),
        video("second", "Elden Ring blind playthrough", 300_000, 120),
    ]
    assert pick_most_popular(candidates, "Elden Ring", 600).video_id == "top"
    assert pick_most_popular(candidates[:2], "Elden Ring", 600) is None


def test_parse_iso_duration() -> None:
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT45M") == 2700
    assert parse_iso_duration("P1DT1H") == 90000
    assert parse_iso_duration("bad") is None


def api_transport(quota: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            if quota:
                return httpx.Response(
                    403, text='{"error": {"errors": [{"reason": "quotaExceeded"}]}}'
                )
            assert request.url.params["order"] == "relevance"
            return httpx.Response(
                200, json={"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]}
            )
        assert request.url.params["id"] == "v1,v2"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "v1",
                        "snippet": {"title": "Hades let's play", "channelTitle": "A"},
                        "statistics": {"viewCount": "1200"},
                        "contentDetails": {"duration": "PT1H"},
                    },
                    {
                        "id": "v2",
                        "snippet": {"title": "Hades #2", "channelTitle": "B"},
                        "statistics": {"viewCount": "90"},
                        "contentDetails": {
                            "duration": "PT30M",
                            "contentRating": {"ytRating": "ytAgeRestricted"},
                        },
                    },
                ]
            },
        )

    return httpx.MockTransport(handler)


async def test_search_api_reads_views_and_durations() -> None:
    async with httpx.AsyncClient(transport=api_transport()) as client:
        found = await search(client, "key", "Hades let's play")
    assert [
        (c.video_id, c.views, c.duration_seconds, c.channel, c.age_restricted) for c in found
    ] == [
        ("v1", 1200, 3600, "A", False),
        ("v2", 90, 1800, "B", True),
    ]


def test_age_restricted_videos_are_skipped() -> None:
    """Без входа в аккаунт YouTube не отдаёт ни субтитры, ни звук роликов 18+"""
    from dataclasses import replace

    from app.youtube.search import rank_candidates

    popular = replace(video("adult", "Hades let's play", 10_000, 60), age_restricted=True)
    regular = video("ok", "Hades let's play", 100, 60)
    assert [c.video_id for c in rank_candidates([popular, regular], "Hades", 600)] == ["ok"]


def test_age_restriction_does_not_trip_youtube_breaker() -> None:
    from app.crawler.breakers import judge_youtube
    from app.jobs.breaker import Verdict
    from app.youtube.errors import translate_ytdlp_error

    error = translate_ytdlp_error(Exception("Sign in to confirm your age. This video may be"))
    assert judge_youtube(error) is not Verdict.TRIP


async def test_quota_raises_and_is_remembered_without_calling_api() -> None:
    from app.youtube.search import QuotaExceededError, SearchQuota

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return api_transport(quota=True).handler(request)

    quota = SearchQuota()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(QuotaExceededError):
                await search(client, "key", "Hades let's play", quota)
    assert calls == ["/youtube/v3/search"]
    assert not quota.available()


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Sign in to confirm you're not a bot", "YouTubeBlockedError"),
        ("Sign in to confirm you’re not a bot. This helps protect", "YouTubeBlockedError"),
        ("Video unavailable. This video is private", "VideoUnavailableError"),
        # Возрастное ограничение одного ролика — не блокировка адреса, автомат не открывается
        (
            "ERROR: [youtube] MM1qVVpUWYw: Sign in to confirm your age. This video may be "
            "inappropriate for some users. Use --cookies-from-browser or --cookies",
            "VideoUnavailableError",
        ),
        ("HTTP Error 503", "RetryableError"),
    ],
)
def test_ytdlp_errors_translated(message, kind) -> None:
    from app.youtube.errors import translate_ytdlp_error

    assert type(translate_ytdlp_error(Exception(message))).__name__ == kind


def test_prefers_letsplay_titles_and_skips_parodies_in_other_games() -> None:
    candidates = [
        video("mc", "Playing as the Goddess HADES In Minecraft!", 10_000_000, 20),
        video("review", "Hades is a masterpiece", 3_000_000, 15),
        video("lp", "Hades - Let's Play Part 1", 900_000, 60),
    ]
    assert pick_most_popular(candidates, "Hades", 600).video_id == "lp"
    # Без помеченных летсплеев берётся самый популярный подходящий ролик
    assert pick_most_popular(candidates[:2], "Hades", 600).video_id == "review"
    # Слово из названия самой игры другим ролик не делает
    mc = [video("m", "Minecraft Dungeons let's play", 5, 60)]
    assert pick_most_popular(mc, "Minecraft Dungeons", 600).video_id == "m"


KEY = "AIzaSy-secret-test-key"


def error_response(status: int, reason: str) -> httpx.Response:
    body = {"error": {"code": status, "errors": [{"reason": reason}], "status": "X"}}
    return httpx.Response(status, json=body)


async def run_api(handler, quota=None):
    from app.youtube.search import SearchQuota

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await search(client, KEY, "Hades let's play", quota or SearchQuota())


async def test_key_sent_in_header_never_in_url_or_errors() -> None:
    from app.youtube.errors import YouTubeApiError

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="backend error")

    with pytest.raises(YouTubeApiError) as caught:
        await run_api(handler)
    assert seen[0].headers["X-Goog-Api-Key"] == KEY
    assert KEY not in str(seen[0].url)
    assert KEY not in str(caught.value)
    assert caught.value.status == 500


@pytest.mark.parametrize(
    "reason", ["keyInvalid", "accessNotConfigured", "forbidden", "ipRefererBlocked"]
)
async def test_misconfigured_key_raises_config_error(reason) -> None:
    from app.youtube.errors import YouTubeApiConfigError

    with pytest.raises(YouTubeApiConfigError) as caught:
        await run_api(lambda request: error_response(403, reason))
    assert reason in str(caught.value) and KEY not in str(caught.value)


async def test_plain_403_is_config_error_too() -> None:
    from app.youtube.errors import YouTubeApiConfigError

    with pytest.raises(YouTubeApiConfigError):
        await run_api(lambda request: httpx.Response(403, text="Forbidden"))


async def test_quota_on_videos_list_is_detected_too() -> None:
    from app.youtube.search import QuotaExceededError, SearchQuota

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "v1"}}]})
        return error_response(403, "quotaExceeded")

    quota = SearchQuota()
    with pytest.raises(QuotaExceededError):
        await run_api(handler, quota)
    assert not quota.available()


def test_quota_resets_at_pacific_midnight() -> None:
    from datetime import UTC, datetime

    from app.youtube.search import SearchQuota, next_quota_reset

    # 06.11 20:00 UTC — это 12:00 в Лос-Анджелесе, сброс в 00:00 по Тихоокеанскому = 08:00 UTC
    now = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    assert next_quota_reset(now) == datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
    quota = SearchQuota()
    quota.exhausted(now)
    assert not quota.available(datetime(2026, 11, 7, 7, 59, tzinfo=UTC))
    assert quota.available(datetime(2026, 11, 7, 8, 0, tzinfo=UTC))


def test_rank_candidates_letsplays_first_then_by_views() -> None:
    from app.youtube.search import rank_candidates

    candidates = [
        video("review", "Hades review", 3_000_000, 15),
        video("lp2", "Hades #2", 500_000, 40),
        video("lp1", "Hades let's play part 1", 900_000, 60),
        video("short", "Hades let's play", 9_000_000, 2),
    ]
    assert [c.video_id for c in rank_candidates(candidates, "Hades", 600)] == [
        "lp1",
        "lp2",
        "review",
    ]
