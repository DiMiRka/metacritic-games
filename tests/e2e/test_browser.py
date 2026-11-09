import re
from datetime import date

import pytest

from app.catalog.models import Game, GamePlatform
from tests.conftest import PASSWORD, create_user

pytestmark = pytest.mark.e2e


async def seed_games(session_factory) -> None:
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                Game(
                    slug="elden-ring",
                    title="Elden Ring",
                    developer="FromSoftware",
                    genres=["Action RPG"],
                    release_date=date(2022, 2, 25),
                    platforms=[
                        GamePlatform(platform="pc", platform_name="PC", metascore=94, userscore=7.6)
                    ],
                ),
                Game(
                    slug="hades",
                    title="Hades",
                    developer="Supergiant Games",
                    genres=["Roguelike"],
                    release_date=date(2020, 9, 17),
                    platforms=[GamePlatform(platform="pc", platform_name="PC", metascore=93)],
                ),
            ]
        )


async def test_catalog_search_and_game_page(page, session_factory) -> None:
    await seed_games(session_factory)

    await page.goto("/")
    titles = page.locator(".card__title")
    assert sorted(await titles.all_inner_texts()) == ["Elden Ring", "Hades"]

    # Поиск htmx: список перерисовывается без перезагрузки, адрес обновляется
    await page.fill("input[name=q]", "elden")
    await page.wait_for_url(re.compile(r"[?&]q=elden"))
    await titles.first.wait_for()
    assert await titles.all_inner_texts() == ["Elden Ring"]

    await page.click("text=Elden Ring")
    await page.wait_for_url("**/games/elden-ring")
    assert await page.inner_text("h1") == "Elden Ring"
    assert "FromSoftware" in await page.inner_text("main")


async def test_admin_login_and_monitor(page, session_factory) -> None:
    await create_user(session_factory, "admin")

    await page.goto("/monitor")
    await page.wait_for_url("**/login?next=/monitor")
    await page.fill("input[name=login]", "admin")
    await page.fill("input[name=password]", PASSWORD)
    await page.click("button[type=submit]")

    await page.wait_for_url("**/monitor")
    # Снимок приходит через SSE и заменяет содержимое блока монитора
    await page.locator("#live:not(.live--off)").wait_for()
    # Заголовки панелей набраны заглавными через CSS, inner_text отдаёт их так же
    assert "последние проходы" in (await page.inner_text("#monitor")).lower()

    # Живого воркера нет: кнопка запуска отвечает понятной ошибкой, а не молчит
    await page.click("#run-now")
    await page.locator("#run-message", has_text="Воркер не отвечает").wait_for()


async def test_wrong_password_shows_error(page, session_factory) -> None:
    await create_user(session_factory, "admin")
    await page.goto("/login")
    await page.fill("input[name=login]", "admin")
    await page.fill("input[name=password]", "wrong password!!")
    await page.click("button[type=submit]")
    await page.locator("[role=alert]", has_text="Неверный логин или пароль").wait_for()
