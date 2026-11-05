import contextlib
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app import auth as auth_module
from app import users as users_cli
from app.auth import csrf_token_for, now_tz, safe_next, token_hash
from app.main import app
from app.models import LoginAttempt, User, UserSession
from app.web.admin import monitor_events
from app.web.deps import SESSION_COOKIE
from tests.conftest import ORIGIN, PASSWORD, create_user, login_as


async def post_login(client, login, password=PASSWORD, next="/monitor"):
    return await client.post("/login", data={"login": login, "password": password, "next": next})


async def count(session_factory, model) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


# Вход и сессии


async def test_login_sets_session_cookie_and_redirects(make_client, session_factory) -> None:
    await create_user(session_factory, "admin")
    async with make_client() as client:
        response = await post_login(client, "Admin ")

    assert response.status_code == 303
    assert response.headers["location"] == "/monitor"
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie

    token = response.cookies[SESSION_COOKIE]
    async with session_factory() as db:
        stored = await db.scalar(select(UserSession.token_hash))
        user = await db.scalar(select(User))
    assert stored == token_hash(token) != token
    assert user.last_login_at is not None


async def test_wrong_password_and_unknown_login_look_the_same(
    make_client, session_factory, monkeypatch
) -> None:
    await create_user(session_factory, "admin")
    verified: list[str] = []
    original = auth_module._verify

    def spy(password_hash: str, password: str) -> bool:
        verified.append(password_hash)
        return original(password_hash, password)

    monkeypatch.setattr(auth_module, "_verify", spy)
    async with make_client() as client:
        wrong = await post_login(client, "admin", "wrong password!!")
        unknown = await post_login(client, "nobody", "wrong password!!")

    assert wrong.status_code == unknown.status_code == 401
    assert "Неверный логин или пароль" in wrong.text
    assert wrong.text == unknown.text
    assert len(verified) == 2
    assert verified[1] == auth_module._DUMMY_HASH


async def test_inactive_user_cannot_login(make_client, session_factory) -> None:
    await create_user(session_factory, "admin", active=False)
    async with make_client() as client:
        assert (await post_login(client, "admin")).status_code == 401


async def test_login_rate_limited_per_ip_and_login(make_client, session_factory) -> None:
    await create_user(session_factory, "admin")
    async with make_client(ip="10.0.0.1") as client:
        for _ in range(5):
            assert (await post_login(client, "admin", "bad password!!")).status_code == 401
        blocked = await post_login(client, "admin")
    async with make_client(ip="10.0.0.2") as other_ip:
        allowed = await post_login(other_ip, "admin")

    assert blocked.status_code == 429
    assert allowed.status_code == 303
    assert await count(session_factory, LoginAttempt) == 5


async def test_successful_login_clears_attempts(make_client, session_factory) -> None:
    await create_user(session_factory, "admin")
    async with make_client() as client:
        await post_login(client, "admin", "bad password!!")
        await post_login(client, "admin")
    assert await count(session_factory, LoginAttempt) == 0


async def test_new_login_replaces_old_session(make_client, session_factory) -> None:
    user_id = await create_user(session_factory, "admin")
    old = await login_as(session_factory, user_id)
    async with make_client() as client:
        client.cookies.set(SESSION_COOKIE, old)
        response = await post_login(client, "admin")

    new = response.cookies[SESSION_COOKIE]
    assert new != old
    async with session_factory() as db:
        hashes = set((await db.scalars(select(UserSession.token_hash))).all())
    assert hashes == {token_hash(new)}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/monitor?x=1", "/monitor?x=1"),
        ("//evil.com", "/"),
        ("https://evil.com", "/"),
        ("/\\evil.com", "/"),
        ("/ok\r\nLocation: x", "/"),
        ("monitor", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next(value, expected) -> None:
    assert safe_next(value) == expected


async def test_login_next_cannot_redirect_away(make_client, session_factory) -> None:
    await create_user(session_factory, "admin")
    async with make_client() as client:
        response = await post_login(client, "admin", next="//evil.com/x")
    assert response.headers["location"] == "/"


async def test_logout_revokes_session(make_client, session_factory) -> None:
    async with make_client("admin") as client:
        csrf = client.headers["X-CSRF-Token"]
        client.headers.pop("X-CSRF-Token")
        rejected = await client.post("/logout", data={"csrf_token": "forged"})
        response = await client.post("/logout", data={"csrf_token": csrf})
        after = await client.get("/api/monitor")

    assert rejected.status_code == 403
    assert response.status_code == 303
    assert await count(session_factory, UserSession) == 0
    assert after.status_code == 401


async def test_expired_session_is_rejected(make_client, session_factory) -> None:
    async with make_client("admin") as client:
        async with session_factory() as db, db.begin():
            await db.execute(update(UserSession).values(expires_at=now_tz() - timedelta(minutes=1)))
        assert (await client.get("/api/monitor")).status_code == 401


# Доступ к служебной части


async def test_anonymous_html_redirects_to_login(make_client) -> None:
    async with make_client() as client:
        response = await client.get("/monitor?tab=1", headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/monitor%3Ftab%3D1"


@pytest.mark.parametrize("path", ["/api/monitor", "/monitor/stream"])
async def test_anonymous_api_gets_401(make_client, path) -> None:
    async with make_client() as client:
        response = await client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Требуется вход"}


async def test_viewer_gets_403(make_client) -> None:
    async with make_client("viewer") as client:
        api = await client.get("/api/monitor")
        page = await client.get("/monitor", headers={"Accept": "text/html"})
        run = await client.post("/api/runs")
    assert api.status_code == page.status_code == run.status_code == 403
    assert "Нет доступа" in page.text


async def test_public_pages_stay_open(make_client) -> None:
    async with make_client() as client:
        for path in ("/", "/api/games", "/health", "/login"):
            assert (await client.get(path)).status_code == 200


async def test_nav_depends_on_role(make_client) -> None:
    async with make_client() as anonymous:
        anon = (await anonymous.get("/")).text
    async with make_client("admin") as admin:
        page = (await admin.get("/")).text
        csrf = admin.headers["X-CSRF-Token"]

    assert 'href="/monitor"' not in anon
    assert 'href="/login"' in anon
    assert 'name="csrf-token"' not in anon
    assert 'href="/monitor"' in page
    assert f'<meta name="csrf-token" content="{csrf}">' in page
    assert f'"X-CSRF-Token": "{csrf}"' in page


async def test_docs_available_in_dev(make_client) -> None:
    async with make_client() as client:
        assert (await client.get("/docs")).status_code == 200


# CSRF


async def test_post_requires_csrf_header(make_client) -> None:
    async with make_client("admin") as client:
        client.headers.pop("X-CSRF-Token")
        missing = await client.post("/api/runs")
        forged = await client.post("/api/runs", headers={"X-CSRF-Token": "0" * 64})
    assert missing.status_code == forged.status_code == 403
    assert missing.json()["detail"] == "Неверный CSRF-токен"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.com"},
        {"Origin": "null"},
        {"Origin": "http://test.evil.com"},
        {"Referer": "https://evil.com/page"},
        {},
    ],
)
async def test_foreign_origin_rejected(session_factory, headers) -> None:
    user_id = await create_user(session_factory, "admin")
    token = await login_as(session_factory, user_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=ORIGIN) as client:
        client.cookies.set(SESSION_COOKIE, token)
        headers = {"X-CSRF-Token": csrf_token_for(token), **headers}
        run = await client.post("/api/runs", headers=headers)
        login = await client.post("/login", data={"login": "admin"}, headers=headers)
    assert run.status_code == login.status_code == 403
    assert run.json()["detail"] == "Запрос с чужого адреса отклонён"


async def test_same_origin_referer_accepted(make_client, session_factory) -> None:
    await create_user(session_factory, "admin")
    async with make_client() as client:
        client.headers.pop("Origin")
        response = await post_login(client, "admin")
        client.headers["Referer"] = f"{ORIGIN}/login"
        with_referer = await post_login(client, "admin")
    assert response.status_code == 403
    assert with_referer.status_code == 303


# Поток мониторинга


async def test_stream_closes_when_session_revoked(session_factory) -> None:
    allowed = iter([True, False])

    async def still_allowed() -> bool:
        return next(allowed)

    events = [
        e
        async for e in monitor_events(
            session_factory, interval=0, limit=10, still_allowed=still_allowed, recheck_seconds=0
        )
    ]

    assert [e.split("\n", 1)[0] for e in events] == ["event: update", "event: logout"]


# CLI


@contextlib.contextmanager
def patched_cli(monkeypatch, session_factory, *passwords: str):
    answers = iter(passwords)
    monkeypatch.setattr(users_cli.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(users_cli, "SessionLocal", session_factory)
    yield


async def test_cli_create_admin_and_set_password(monkeypatch, session_factory) -> None:
    new_password = "another long password"
    with patched_cli(monkeypatch, session_factory, PASSWORD, PASSWORD):
        await users_cli.create_admin("Root")
    async with session_factory() as db:
        user_id = await db.scalar(select(User.id))
    await login_as(session_factory, user_id)

    with patched_cli(monkeypatch, session_factory, new_password, new_password):
        await users_cli.set_password("root")

    async with session_factory() as db:
        user = await db.scalar(select(User))
        assert user.login == "root"
        assert user.role == "admin"
        assert user.password_hash.startswith("$argon2")
        assert await auth_module.authenticate(db, "root", new_password) is not None
    assert await count(session_factory, UserSession) == 0


async def test_cli_rejects_short_or_mismatched_password(monkeypatch, session_factory) -> None:
    with patched_cli(monkeypatch, session_factory, "short", "short"), pytest.raises(SystemExit):
        await users_cli.create_admin("root")
    with (
        patched_cli(monkeypatch, session_factory, PASSWORD, PASSWORD + "!"),
        pytest.raises(SystemExit),
    ):
        await users_cli.create_admin("root")
    assert await count(session_factory, User) == 0
