import httpx
import pytest

from app.youtube import search as search_module
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
                        "contentDetails": {"duration": "PT30M"},
                    },
                ]
            },
        )

    return httpx.MockTransport(handler)


async def test_search_api_reads_views_and_durations() -> None:
    async with httpx.AsyncClient(transport=api_transport()) as client:
        found = await search(client, "key", "Hades let's play", {})
    assert [(c.video_id, c.views, c.duration_seconds, c.channel) for c in found] == [
        ("v1", 1200, 3600, "A"),
        ("v2", 90, 1800, "B"),
    ]


async def test_quota_falls_back_to_ytdlp(monkeypatch) -> None:
    async def ytdlp(query, options):
        return [video("y1", "Hades", 5, 60)]

    monkeypatch.setattr(search_module, "search_ytdlp", ytdlp)
    async with httpx.AsyncClient(transport=api_transport(quota=True)) as client:
        found = await search(client, "key", "Hades let's play", {})
    assert [c.video_id for c in found] == ["y1"]


async def test_no_key_uses_ytdlp(monkeypatch) -> None:
    calls = []

    async def ytdlp(query, options):
        calls.append(query)
        return []

    monkeypatch.setattr(search_module, "search_ytdlp", ytdlp)
    async with httpx.AsyncClient(transport=api_transport()) as client:
        assert await search(client, "", "Hades let's play", {}) == []
    assert calls == ["Hades let's play"]


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Sign in to confirm you're not a bot", "YouTubeBlockedError"),
        ("Video unavailable. This video is private", "VideoUnavailableError"),
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
