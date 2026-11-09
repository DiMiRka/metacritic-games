from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.catalog import queries
from app.catalog.models import Game, GamePlatform, ReviewSummary
from app.web.deps import get_session, get_session_factory
from tests.conftest import app

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
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def slugs(client: AsyncClient, **params) -> list[str]:
    response = await client.get("/api/v1/games", params=params)
    assert response.status_code == 200
    return [item["slug"] for item in response.json()["items"]]


async def test_default_sort_is_metascore_desc(client) -> None:
    assert await slugs(client) == [
        "elden-ring",
        "stardew-valley",
        "elden-ring-nightreign",
        "fresh-game",
    ]


async def test_sort_by_date_newest_first(client) -> None:
    assert await slugs(client, sort="date") == [
        "fresh-game",
        "elden-ring-nightreign",
        "elden-ring",
        "stardew-valley",
    ]


async def test_relevance_without_query_falls_back_to_metascore(client) -> None:
    assert await slugs(client, sort="relevance") == await slugs(client)


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
    response = await client.get("/api/v1/games", params={"platform": "nintendo-switch"})
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
    first = (await client.get("/api/v1/games")).json()
    last = (await client.get("/api/v1/games", params={"page": 2})).json()
    clamped = (await client.get("/api/v1/games", params={"page": 99})).json()

    assert (first["total"], first["pages"], len(first["items"])) == (4, 2, 3)
    assert [i["slug"] for i in last["items"]] == ["fresh-game"]
    assert clamped["page"] == 2


async def test_unknown_sort_falls_back(client) -> None:
    assert (await client.get("/api/v1/games", params={"sort": "drop table"})).status_code == 200


async def test_api_game_detail(client) -> None:
    data = (await client.get("/api/v1/games/elden-ring")).json()

    assert data["title"] == "Elden Ring"
    assert {p["platform"] for p in data["platforms"]} == {"pc", "playstation-5"}
    assert data["critic_summary"]["likes"] == ["Огромный открытый мир"]
    assert data["user_summary"] is None
    assert [g["slug"] for g in data["similar"]] == ["elden-ring-nightreign"]
    assert data["similar"][0]["reasons"] == [
        {"kind": "developer", "values": ["FromSoftware"]},
        {"kind": "genres", "values": ["Action RPG"]},
        {"kind": "platforms", "values": ["PC"]},
    ]


async def test_api_game_not_found(client) -> None:
    assert (await client.get("/api/v1/games/nope")).status_code == 404


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
    assert '<p class="similar__why">тоже от FromSoftware · Action RPG</p>' in response.text


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


async def test_game_page_and_api_show_letsplay(client, session_factory) -> None:
    from app.catalog.models import Letsplay

    async with session_factory() as session, session.begin():
        game = Game(slug="hades", title="Hades", genres=[])
        session.add(game)
        await session.flush()
        session.add(
            Letsplay(
                game_id=game.id, status="done", video_id="abc", channel="Канал",
                video_url="https://www.youtube.com/watch?v=abc", title="Hades #1",
                views=1234567, duration_seconds=5400, conclusion="Блогеру понравилось.",
                verdict="mixed", transcript_source="whisper", transcript="секрет расшифровки",
            )
        )  # fmt: skip

    page = (await client.get("/games/hades")).text
    data = (await client.get("/api/v1/games/hades")).json()["letsplay"]

    assert "Мнение летсплейщика" in page and "Блогеру понравилось." in page
    assert 'href="https://www.youtube.com/watch?v=abc"' in page
    assert "1 234 567 просмотров" in page and "1 ч 30 мин" in page
    assert "распознанной Whisper" in page and "двоякое" in page
    assert (data["verdict"], data["transcript_source"], data["channel"]) == (
        "mixed",
        "whisper",
        "Канал",
    )
    assert "transcript" not in data


async def test_letsplay_link_only_to_youtube(client, session_factory) -> None:
    from app.catalog.models import Letsplay

    async with session_factory() as session, session.begin():
        game = Game(slug="evil", title="Evil", genres=[])
        session.add(game)
        await session.flush()
        session.add(
            Letsplay(game_id=game.id, status="done", conclusion="ok",
                     video_url="https://evil.example/watch?v=1", title="Ролик")
        )  # fmt: skip
    page = (await client.get("/games/evil")).text
    assert "evil.example" not in page


async def test_game_page_shows_translation_with_original(client, session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            Game(slug="t", title="T", genres=[], description="Dark fantasy <b>world</b>",
                 description_ru="Мрачное фэнтези")
        )  # fmt: skip
        session.add(Game(slug="u", title="U", genres=[], description="Only English"))

    translated = (await client.get("/games/t")).text
    untranslated = (await client.get("/games/u")).text
    data = (await client.get("/api/v1/games/t")).json()

    assert '<p class="description">Мрачное фэнтези</p>' in translated
    assert "Оригинал с Metacritic" in translated
    assert "Dark fantasy &lt;b&gt;world&lt;/b&gt;" in translated
    assert '<p class="description">Only English</p>' in untranslated
    assert "Оригинал с Metacritic" not in untranslated
    assert (data["description"], data["description_ru"]) == (
        "Dark fantasy <b>world</b>",
        "Мрачное фэнтези",
    )
