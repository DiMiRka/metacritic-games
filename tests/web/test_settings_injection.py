"""create_app(settings) использует переданные настройки целиком, без примеси общих"""

import ast
from collections.abc import AsyncIterator
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.auth.service import csrf_token_for
from app.core.config import Settings, settings
from app.web.app import create_app
from app.web.deps import get_session, get_session_factory
from tests.conftest import ORIGIN, PASSWORD, create_user

ROOT = Path(__file__).parents[2]
# Модули, через которые идёт запрос к сайту: им настройки передаёт приложение
WEB_PATH = [
    *sorted((ROOT / "app/web").rglob("*.py")),
    ROOT / "app/auth/service.py",
    ROOT / "app/auth/crypto.py",
    ROOT / "app/monitoring/snapshot.py",
    ROOT / "app/monitoring/metrics.py",
    ROOT / "app/crawler/runs.py",
    ROOT / "app/crawler/control.py",
]
# Точка сборки: настройки по умолчанию для uvicorn --factory
COMPOSITION_ROOT = ROOT / "app/web/app.py"


def imports_global_settings(path: Path) -> bool:
    tree = ast.parse(path.read_text("utf-8"))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.core.config"
        and any(alias.name == "settings" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_web_path_does_not_import_global_settings() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in WEB_PATH
        if path != COMPOSITION_ROOT and imports_global_settings(path)
    ]
    assert not offenders, f"берут глобальные settings вместо настроек приложения: {offenders}"


def custom_settings() -> Settings:
    return Settings(
        secret_key="c" * 64,
        login_max_attempts=1,
        login_delay_after=100,
        rate_limit_login_per_minute=3,
        session_ttl_hours=1,
        timezone="UTC",
    )


async def client_for(app, session_factory, ip: str = "10.7.0.1") -> AsyncClient:
    async def override() -> AsyncIterator:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 50000)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )


async def post_login(client, password: str):
    data = {"login": "admin", "password": password, "next": "/monitor"}
    return await client.post("/login", data=data)


async def test_custom_settings_drive_login_limits_and_csrf(session_factory) -> None:
    config = custom_settings()
    assert settings.login_max_attempts != 1 and settings.secret_key != config.secret_key
    await create_user(session_factory, "admin")
    app = create_app(config)

    async with await client_for(app, session_factory) as client:
        # login_max_attempts=1 из своих настроек: вторая попытка с того же IP уже 429
        first = await post_login(client, "bad password!!")
        second = await post_login(client, PASSWORD)
    async with await client_for(app, session_factory, ip="10.7.0.2") as client:
        ok = await post_login(client, PASSWORD)
        token = ok.cookies.get("session")
        page = await client.get("/monitor")

    assert (first.status_code, second.status_code) == (401, 429)
    assert ok.status_code == 303
    assert "max-age=3600" in ok.headers["set-cookie"].lower()
    # CSRF-токен на странице выведен из SECRET_KEY этого приложения, а не общего
    assert csrf_token_for(token, config=config) in page.text
    assert csrf_token_for(token, config=settings) not in page.text


async def test_rate_limits_are_per_app(session_factory) -> None:
    strict, relaxed = create_app(custom_settings()), create_app(Settings(timezone="UTC"))
    async with await client_for(strict, session_factory) as client:
        strict_codes = [(await client.get("/login")).status_code for _ in range(4)]
    async with await client_for(relaxed, session_factory) as client:
        relaxed_codes = [(await client.get("/login")).status_code for _ in range(4)]

    assert strict_codes == [200, 200, 200, 429]
    assert relaxed_codes == [200, 200, 200, 200]
