from datetime import date

import pytest

from app.scraper.parsers import (
    VIDEO_HOSTS,
    Review,
    parse_game,
    parse_listing,
    parse_reviews,
    parse_user_score,
    safe_url,
)
from tests.conftest import load_fixture


def test_listing_new_releases() -> None:
    slugs = parse_listing(load_fixture("finder_new_releases.json"))
    assert len(slugs) == 20
    assert slugs[0] == "cozy-game-restoration"


def test_listing_browse_page() -> None:
    slugs = parse_listing(load_fixture("finder_browse_page2.json"))
    assert len(slugs) == 24
    assert len(set(slugs)) == 24


def test_listing_empty() -> None:
    assert parse_listing({"data": {"items": []}}) == []
    assert parse_listing({}) == []


def test_game_full() -> None:
    game = parse_game(load_fixture("composer_elden-ring.json"))
    assert game is not None
    assert game.slug == "elden-ring"
    assert game.title == "Elden Ring"
    assert game.developer == "From Software"
    assert game.genres == ["Action RPG"]
    assert game.release_date == date(2022, 2, 25)
    assert game.cover_url == (
        "https://www.metacritic.com/a/img/catalog/provider/6/3/6-1-824862-13.jpg"
    )
    assert game.video_url == "https://cdn.jwplayer.com/players/OXExoEAD.html"
    assert game.description and game.description.startswith("A New World")

    scores = {p.platform: p.metascore for p in game.platforms}
    assert scores == {
        "xbox-one": None,
        "pc": 94,
        "playstation-4": scores["playstation-4"],
        "xbox-series-x": 96,
        "playstation-5": 96,
    }
    assert game.lead_platform == "playstation-5"
    assert game.lead_userscore == 8.4


def test_game_without_reviews() -> None:
    game = parse_game(load_fixture("composer_starshapes.json"))
    assert game is not None
    assert game.developer == "Entire Games"
    assert [(p.platform, p.metascore) for p in game.platforms] == [("pc", None)]
    assert game.lead_userscore is None
    assert game.video_url is None


def test_game_not_found_returns_none() -> None:
    assert parse_game(load_fixture("composer_not-found.json")) is None


def test_user_score() -> None:
    assert parse_user_score(load_fixture("user_stats_elden-ring_pc.json")) == 7.6


def test_user_score_zero_reviews_is_none() -> None:
    assert parse_user_score({"data": {"item": {"score": 0, "reviewCount": 0}}}) is None
    assert parse_user_score({"data": {"item": {"score": None, "reviewCount": None}}}) is None


def test_user_score_tbd_is_none() -> None:
    tbd = {"score": 0, "reviewCount": 2, "positiveCount": 2, "sentiment": None}
    assert parse_user_score({"data": {"item": tbd}}) is None


def test_critic_reviews() -> None:
    reviews = parse_reviews(load_fixture("critic_reviews_elden-ring.json")["data"]["items"])
    assert len(reviews) == 10
    assert reviews[0].author == "LEVEL (Czech Republic)"
    assert reviews[0].score == 100
    assert all(r.text for r in reviews)


def test_user_reviews() -> None:
    reviews = parse_reviews(load_fixture("user_reviews_elden-ring.json")["data"]["items"])
    assert len(reviews) == 30
    assert all(r.text and r.score is not None for r in reviews)


def test_reviews_skip_empty_quotes() -> None:
    reviews = parse_reviews([{"quote": "  ", "score": 5}, {"quote": "ok", "score": None}])
    assert reviews == [Review(text="ok", score=None, author=None)]


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "http://cdn.jwplayer.com/players/x.html",
        "https://evil.com/players/x.html",
        "https://cdn.jwplayer.com.evil.com/x.html",
        "//evil.com/x.html",
        "https://user:pass@cdn.jwplayer.com/x.html",
        "https://cdn.jwplayer.com:8443/x.html",
        r"https://cdn.jwplayer.com\@evil.com/x.html",
        "https://cdn.jwplayer.com/x.html\njavascript:alert(1)",
        "",
        None,
        42,
    ],
)
def test_safe_url_rejects(value) -> None:
    assert safe_url(value, VIDEO_HOSTS) is None


def test_safe_url_accepts_allowed_https_host() -> None:
    url = "https://cdn.jwplayer.com/players/OXExoEAD.html"
    assert safe_url(url, VIDEO_HOSTS) == url
    assert safe_url("https://CDN.jwplayer.com/x.html", VIDEO_HOSTS) is not None


def test_game_with_untrusted_urls_drops_them() -> None:
    data = load_fixture("composer_elden-ring.json")
    item = data["components"][0]["data"]["item"]
    item["video"]["embedUrl"] = "javascript:alert(1)"
    item["video"]["manifestUrl"] = "https://evil.com/x.m3u8"
    for image in item["images"]:
        image["bucketPath"] = "@evil.com/x.jpg"

    game = parse_game(data)

    assert game.video_url is None
    assert game.cover_url is None
