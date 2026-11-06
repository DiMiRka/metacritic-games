"""Контракт с настоящим API Metacritic: парсеры понимают то, что API отдаёт сейчас.

Фикстуры в tests/fixtures фиксируют формат на момент записи, а эти тесты ловят изменения
самого API. Нужна сеть, поэтому они запускаются отдельно: RUN_LIVE=1 pytest -m live
(в CI раз в неделю)
"""

import os

import pytest

from app.crawler.processor import fetch_bundle, parse_bundle
from app.scraper.client import MetacriticClient
from app.scraper.parsers import parse_listing, parse_user_score

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="нужен RUN_LIVE=1 и сеть"),
]

# Давно вышедшая игра с тысячами отзывов и несколькими платформами
SLUG = "elden-ring"


async def test_new_releases_listing() -> None:
    async with MetacriticClient() as client:
        slugs = parse_listing(await client.new_releases())
    assert len(slugs) >= 5
    assert all(isinstance(s, str) and s for s in slugs)


async def test_browse_page() -> None:
    async with MetacriticClient() as client:
        slugs = parse_listing(await client.browse(1))
    assert len(slugs) >= 10


async def test_full_game_bundle_parses() -> None:
    async with MetacriticClient() as client:
        bundle = await fetch_bundle(client, SLUG)
    data, reviews = parse_bundle(bundle, SLUG)

    assert data.slug == SLUG
    assert data.title.lower().startswith("elden ring")
    assert data.developer
    assert data.release_date is not None
    assert len(data.platforms) >= 2
    # У старых версий (Xbox One, PS4) Metacritic оценку критиков не показывает
    assert sum(p.metascore is not None for p in data.platforms) >= 2
    assert sum(p.userscore is not None for p in data.platforms) >= 2
    assert len(reviews["critic"]) >= 10
    assert len(reviews["user"]) >= 10
    assert all(r.text for r in reviews["critic"])


async def test_user_stats_for_second_platform() -> None:
    async with MetacriticClient() as client:
        bundle = await fetch_bundle(client, SLUG)
    assert bundle.user_stats, "у игры должна быть хотя бы одна платформа кроме основной"
    assert all(parse_user_score(stats) is not None for stats in bundle.user_stats.values())
