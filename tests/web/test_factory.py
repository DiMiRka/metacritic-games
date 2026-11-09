import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.auth import service as auth_service
from app.auth.models import UserSession
from app.core.config import MissingSecretKeyError, Settings, settings
from app.web.app import create_app
from tests.conftest import create_user, login_as


def test_create_app_uses_given_settings_without_touching_database() -> None:
    config = Settings(env="prod", secret_key="s" * 64, timezone="UTC")
    app = create_app(config)
    assert app.state.settings is config
    assert app.docs_url is None
    assert not hasattr(app.state, "db")


def test_prod_web_requires_secret_key() -> None:
    config = Settings(env="prod", secret_key="")
    with pytest.raises(MissingSecretKeyError, match="SECRET_KEY"):
        create_app(config)
    with pytest.raises(MissingSecretKeyError):
        _ = config.secret_keys
    assert len(Settings(env="dev", secret_key="").secret_key) == 64


def test_prod_settings_load_without_secret_key_outside_web() -> None:
    """migrate и воркеры получают ENV=prod без SECRET_KEY: настройки должны загружаться"""
    config = Settings(env="prod", secret_key="")
    assert config.is_prod and config.secret_key == ""


def test_worker_and_migrate_start_in_prod_without_secret_key() -> None:
    """Как в compose: ENV=prod без SECRET_KEY, импорт воркера и миграций не падает"""
    code = "import app.core.config, app.worker.main, app.core.db_roles"
    # Переменные окружения важнее .env: какие бы значения ни лежали в локальном .env
    env = {**os.environ, "ENV": "prod", "SECRET_KEY": ""}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_csrf_token_depends_on_server_secret(monkeypatch) -> None:
    token = "session-token"
    monkeypatch.setattr(settings, "secret_key", "a" * 64)
    first = auth_service.csrf_token_for(token, config=settings)
    monkeypatch.setattr(settings, "secret_key", "b" * 64)
    assert auth_service.csrf_token_for(token, config=settings) != first
    assert auth_service.csrf_valid(
        token, auth_service.csrf_token_for(token, config=settings), config=settings
    )


async def test_request_session_only_reads_and_touch_runs_after_response(
    make_client, session_factory
) -> None:
    user_id = await create_user(session_factory, "admin")
    token = await login_as(session_factory, user_id)
    async with session_factory() as db, db.begin():
        await db.execute(
            update(UserSession).values(
                last_seen_at=UserSession.last_seen_at - timedelta(minutes=10)
            )
        )
        before = await db.scalar(select(UserSession.last_seen_at))

    async with session_factory() as db:
        auth = await auth_service.load_session(db, token, config=settings)
        assert auth.touch_due
        assert not db.in_transaction() or not db.dirty

    async with make_client() as client:
        from tests.conftest import SESSION_COOKIE

        client.cookies.set(SESSION_COOKIE, token)
        assert (await client.get("/monitor")).status_code == 200

    async with session_factory() as db:
        after = await db.scalar(select(UserSession.last_seen_at))
    assert after > before


async def test_unversioned_api_redirects_to_v1(make_client) -> None:
    async with make_client() as client:
        get = await client.get("/api/games?q=ring")
        post = await client.post("/api/runs")
        missing = await client.get("/api/v1/nope")

    assert get.status_code == 308
    assert get.headers["location"] == "/api/v1/games?q=ring"
    assert get.headers["deprecation"] == "true"
    assert post.status_code == 308
    assert post.headers["location"] == "/api/v1/runs"
    assert missing.status_code == 404


def _imported_modules(code: str) -> set[str]:
    out = subprocess.run(
        [sys.executable, "-c", code + "\nimport sys\nprint(' '.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name.split(".")[0] for name in out.stdout.split()}


def test_web_does_not_import_worker_libraries() -> None:
    """Образ web собирается без зависимостей воркера: импорт приложения их не трогает"""
    modules = _imported_modules("from app.web.app import create_app\ncreate_app()")
    assert not modules & {"anthropic", "openai", "fastembed", "apscheduler", "onnxruntime"}


def test_worker_does_not_import_web_libraries() -> None:
    modules = _imported_modules("import app.worker.main")
    assert not modules & {"fastapi", "starlette", "jinja2", "uvicorn"}
