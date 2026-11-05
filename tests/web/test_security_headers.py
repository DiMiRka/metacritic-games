import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.catalog.models import Game
from app.core.db import get_session
from app.web.app import app

TEMPLATES = Path(__file__).parents[2] / "app" / "web" / "templates"


@pytest.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def add_game(session_factory, slug: str, video_url: str | None, cover_url: str | None):
    async with session_factory() as session, session.begin():
        session.add(
            Game(slug=slug, title=slug, genres=[], video_url=video_url, cover_url=cover_url)
        )


async def test_security_headers_on_pages_and_api(client) -> None:
    for path in ("/", "/api/games", "/health", "/static/style.css"):
        response = await client.get(path)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self';" in csp
        assert "style-src 'self';" in csp
        assert "img-src 'self' https://www.metacritic.com" in csp
        assert "frame-src https://cdn.jwplayer.com" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "unsafe-inline" not in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in response.headers["permissions-policy"]


async def test_htmx_served_locally(client) -> None:
    page = (await client.get("/")).text
    assert 'src="http://test/static/htmx.min.js"' in page
    assert "unpkg.com" not in page
    assert '"includeIndicatorStyles": false' in page
    assert (await client.get("/static/htmx.min.js")).status_code == 200


async def test_bad_stored_urls_are_not_rendered(client, session_factory) -> None:
    await add_game(session_factory, "evil", "javascript:alert(1)", "http://evil.com/x.jpg")
    await add_game(session_factory, "evil-frame", "https://evil.com/players/x.html", None)

    for slug in ("evil", "evil-frame"):
        page = (await client.get(f"/games/{slug}")).text
        assert "<iframe" not in page
        assert "evil.com" not in page
        assert "javascript:" not in page
    assert "evil.com" not in (await client.get("/")).text


async def test_video_iframe_is_sandboxed(client, session_factory) -> None:
    await add_game(
        session_factory,
        "good",
        "https://cdn.jwplayer.com/players/OXExoEAD.html",
        "https://www.metacritic.com/a/img/catalog/provider/6/3/x.jpg",
    )
    page = (await client.get("/games/good")).text
    iframe = re.search(r"<iframe[^>]*>", page, re.S).group(0)
    assert 'sandbox="allow-scripts allow-same-origin allow-presentation"' in iframe
    assert 'referrerpolicy="strict-origin-when-cross-origin"' in iframe
    assert 'allow="fullscreen"' in iframe
    assert 'src="https://www.metacritic.com/a/img/catalog/provider/6/3/x.jpg"' in page


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.html")))
def test_templates_have_no_inline_code(template: str) -> None:
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", source)
    assert "<style" not in source
    assert not re.search(r"\sstyle=", source)
    assert not re.search(r"\son[a-z]+=", source)
    assert "hx-on" not in source
