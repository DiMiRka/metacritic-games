from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.catalog import queries
from app.catalog.models import Game, GamePlatform, ReviewSummary
from app.core.db import get_session
from app.web.app import app

GAMES = [
    ("elden-ring", "Elden Ring", date(2022, 2, 25), ["Action RPG"],
     [("pc", 94, 7.6), ("playstation-5", 96, 8.4)]),
    ("elden-ring-nightreign", "Elden Ring Nightreign", date(2025, 5, 30), ["Action RPG"],
     [("pc", 77, 5.0)]),
    ("stardew-valley", "Stardew Valley", date(2016, 2, 26), ["Simulation"],
     [("pc", 89, 8.9), ("nintendo-switch", 84, None)]),
    ("fresh-game", "Fresh Game", date(2026, 11, 5), ["Puzzle"],
     [("pc", None, None)]),
]  # fmt: skip


@pytest.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    async with session_factory() as session, session.begin():
        games = {
            slug: Game(
                slug=slug,
                title=title,
                release_date=released,
                genres=genres,
                developer="FromSoftware" if slug.startswith("elden") else None,
                platforms=[
                    GamePlatform(platform=p, platform_name=p.upper(), metascore=m, userscore=u)
                    for p, m, u in platforms
                ],
            )
            for slug, title, released, genres, platforms in GAMES
        }
        session.add_all(games.values())
        await session.flush()
        session.add(
            ReviewSummary(
                game_id=games["elden-ring"].id,
                source="critic",
                likes=["Огромный открытый мир"],
                dislikes=["Камера в тесных местах"],
                reviews_count=30,
                reviews_hash="h",
                model="fake",
            )
        )

    async def override() -> AsyncIterator:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def slugs(client: AsyncClient, **params) -> list[str]:
    response = await client.get("/api/games", params=params)
    assert response.status_code == 200
    return [item["slug"] for item in response.json()["items"]]


async def test_default_sort_is_newest_first(client) -> None:
    assert await slugs(client) == [
        "fresh-game",
        "elden-ring-nightreign",
        "elden-ring",
        "stardew-valley",
    ]


async def test_sort_by_metascore_puts_missing_last(client) -> None:
    assert await slugs(client, sort="metascore") == [
        "elden-ring",
        "stardew-valley",
        "elden-ring-nightreign",
        "fresh-game",
    ]


async def test_sort_by_userscore(client) -> None:
    result = await slugs(client, sort="userscore")
    assert result[:3] == ["stardew-valley", "elden-ring", "elden-ring-nightreign"]


async def test_platform_filter_uses_platform_scores(client) -> None:
    response = await client.get("/api/games", params={"platform": "nintendo-switch"})
    items = response.json()["items"]
    assert [i["slug"] for i in items] == ["stardew-valley"]
    assert items[0]["metascore"] == 84
    assert items[0]["userscore"] is None


async def test_search_substring_and_relevance(client) -> None:
    assert await slugs(client, q="elden") == ["elden-ring", "elden-ring-nightreign"]


async def test_search_tolerates_typos(client) -> None:
    assert await slugs(client, q="stardew valey") == ["stardew-valley"]


async def test_search_escapes_like_wildcards(client) -> None:
    assert await slugs(client, q="%") == []
    assert await slugs(client, q="_") == []


async def test_pagination(client, monkeypatch) -> None:
    monkeypatch.setattr(queries, "PAGE_SIZE", 3)
    first = (await client.get("/api/games")).json()
    last = (await client.get("/api/games", params={"page": 2})).json()
    clamped = (await client.get("/api/games", params={"page": 99})).json()

    assert (first["total"], first["pages"], len(first["items"])) == (4, 2, 3)
    assert [i["slug"] for i in last["items"]] == ["stardew-valley"]
    assert clamped["page"] == 2


async def test_unknown_sort_falls_back(client) -> None:
    assert (await client.get("/api/games", params={"sort": "drop table"})).status_code == 200


async def test_api_game_detail(client) -> None:
    data = (await client.get("/api/games/elden-ring")).json()

    assert data["title"] == "Elden Ring"
    assert {p["platform"] for p in data["platforms"]} == {"pc", "playstation-5"}
    assert data["critic_summary"]["likes"] == ["Огромный открытый мир"]
    assert data["user_summary"] is None
    assert [g["slug"] for g in data["similar"]] == ["elden-ring-nightreign"]


async def test_api_game_not_found(client) -> None:
    assert (await client.get("/api/games/nope")).status_code == 404


async def test_index_page(client) -> None:
    response = await client.get("/", params={"platform": "pc", "sort": "metascore"})
    assert response.status_code == 200
    assert "<html" in response.text
    assert "Найдено игр: 4" in response.text
    assert 'value="pc" selected' in response.text


async def test_htmx_returns_fragment(client) -> None:
    response = await client.get("/", params={"q": "stardew"}, headers={"HX-Request": "true"})
    assert "<html" not in response.text
    assert "Stardew Valley" in response.text
    assert "Elden Ring" not in response.text


async def test_game_page(client) -> None:
    response = await client.get("/games/elden-ring")
    assert response.status_code == 200
    assert "Огромный открытый мир" in response.text
    assert "PLAYSTATION-5" in response.text
    assert "25 февраля 2022" in response.text
    assert 'href="/games/elden-ring-nightreign"' in response.text


async def test_game_page_without_scores(client) -> None:
    response = await client.get("/games/fresh-game")
    assert response.status_code == 200
    assert "Отзывов пока нет" in response.text


async def test_game_page_not_found(client) -> None:
    response = await client.get("/games/nope")
    assert response.status_code == 404
    assert "Игра не найдена" in response.text


async def test_html_is_escaped(client, session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(Game(slug="xss", title="<script>alert(1)</script>", genres=[]))
    response = await client.get("/", params={"q": "script"})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
