from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.auth import service as auth_module
from app.auth.models import LoginAttempt, User, UserSession
from app.auth.service import csrf_token_for, token_hash
from app.core.clock import now_tz
from app.core.config import settings
from app.web.routes.admin import monitor_events
from tests.conftest import ORIGIN, PASSWORD, SESSION_COOKIE, app, create_user, login_as


async def post_login(client, login, password=PASSWORD, next="/monitor", code=""):
    data = {"login": login, "password": password, "next": next, "code": code}
    return await client.post("/login", data=data)


async def count(session_factory, model) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


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
    assert "Неверный логин, пароль или код" in wrong.text
    assert wrong.text == unknown.text
    assert len(verified) == 2
    assert verified[1] == auth_module._DUMMY_HASH


async def test_inactive_user_cannot_login(make_client, session_factory) -> None:
    await create_user(session_factory, "admin", active=False)
    async with make_client() as client:
        assert (await post_login(client, "admin")).status_code == 401


async def test_login_rate_limited_per_ip_and_login(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 100)
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
        after = await client.get("/api/v1/monitor")

    assert rejected.status_code == 403
    assert response.status_code == 303
    assert await count(session_factory, UserSession) == 0
    assert after.status_code == 401


async def test_expired_session_is_rejected(make_client, session_factory) -> None:
    async with make_client("admin") as client:
        async with session_factory() as db, db.begin():
            await db.execute(update(UserSession).values(expires_at=now_tz() - timedelta(minutes=1)))
        assert (await client.get("/api/v1/monitor")).status_code == 401


async def test_anonymous_html_redirects_to_login(make_client) -> None:
    async with make_client() as client:
        response = await client.get("/monitor?tab=1", headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/monitor%3Ftab%3D1"


@pytest.mark.parametrize("path", ["/api/v1/monitor", "/monitor/stream"])
async def test_anonymous_api_gets_401(make_client, path) -> None:
    async with make_client() as client:
        response = await client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Требуется вход"}


async def test_viewer_gets_403(make_client) -> None:
    async with make_client("viewer") as client:
        api = await client.get("/api/v1/monitor")
        page = await client.get("/monitor", headers={"Accept": "text/html"})
        run = await client.post("/api/v1/runs")
    assert api.status_code == page.status_code == run.status_code == 403
    assert "Нет доступа" in page.text


async def test_public_pages_stay_open(make_client) -> None:
    async with make_client() as client:
        for path in ("/", "/api/v1/games", "/health", "/login"):
            assert (await client.get(path)).status_code == 200


async def test_viewer_does_not_see_admin_links(make_client) -> None:
    async with make_client("viewer") as viewer:
        page = (await viewer.get("/")).text
    assert 'href="/monitor"' not in page
    assert 'href="/metrics"' not in page


async def test_nav_depends_on_role(make_client) -> None:
    async with make_client() as anonymous:
        anon = (await anonymous.get("/")).text
    async with make_client("admin") as admin:
        page = (await admin.get("/")).text
        csrf = admin.headers["X-CSRF-Token"]

    assert 'href="/monitor"' not in anon
    assert 'href="/metrics"' not in anon
    assert 'href="/login"' in anon
    assert 'name="csrf-token"' not in anon
    assert 'href="/monitor"' in page
    assert 'href="/metrics"' in page
    assert f'<meta name="csrf-token" content="{csrf}">' in page
    assert f'"X-CSRF-Token": "{csrf}"' in page


async def test_docs_available_in_dev(make_client) -> None:
    async with make_client() as client:
        assert (await client.get("/docs")).status_code == 200


async def test_post_requires_csrf_header(make_client) -> None:
    async with make_client("admin") as client:
        client.headers.pop("X-CSRF-Token")
        missing = await client.post("/api/v1/runs")
        forged = await client.post("/api/v1/runs", headers={"X-CSRF-Token": "0" * 64})
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
        run = await client.post("/api/v1/runs", headers=headers)
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


async def test_stream_closes_when_session_revoked(session_factory) -> None:
    allowed = iter([True, False])

    async def still_allowed() -> bool:
        return next(allowed)

    events = [
        e
        async for e in monitor_events(
            session_factory,
            app.state.templates,
            interval=0,
            limit=10,
            still_allowed=still_allowed,
            recheck_seconds=0,
        )
    ]

    assert [e.split("\n", 1)[0] for e in events] == ["event: update", "event: logout"]


# Задержка по логину, серии неудач, второй фактор


async def age_attempts(session_factory, seconds: int) -> None:
    async with session_factory() as db, db.begin():
        await db.execute(
            update(LoginAttempt).values(
                attempted_at=LoginAttempt.attempted_at - timedelta(seconds=seconds)
            )
        )


async def test_login_delay_grows_per_login_across_ips(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 3)
    await create_user(session_factory, "admin")
    for n in range(3):
        async with make_client(ip=f"10.1.0.{n}") as bot:
            assert (await post_login(bot, "admin", "bad password!!")).status_code == 401

    async with make_client(ip="10.9.9.9") as other:
        delayed = await post_login(other, "admin")
        await age_attempts(session_factory, 3)
        allowed = await post_login(other, "admin")

    assert delayed.status_code == 429
    assert "повторите через 2 с" in delayed.text
    assert allowed.status_code == 303
    # Отклонённая из-за задержки попытка не записана и задержку не растит
    assert await count(session_factory, LoginAttempt) == 3


async def test_login_delay_applies_to_unknown_login_too(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 2)
    async with make_client() as client:
        for _ in range(2):
            await post_login(client, "nobody", "bad password!!")
        assert (await post_login(client, "nobody")).status_code == 429


async def test_failed_series_logged_and_shown_in_monitor(
    make_client, session_factory, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 100)
    monkeypatch.setattr(settings, "login_alert_threshold", 3)
    await create_user(session_factory, "boss")
    for n in range(3):
        async with make_client(ip=f"10.2.0.{n}") as bot:
            await post_login(bot, "boss", "bad password!!")

    assert "логин 'boss', 3 попыток с 3 адресов" in caplog.text
    async with make_client("admin") as admin:
        page = await admin.get("/monitor")
    assert "Неудачные входы за сутки" in page.text
    assert "boss" in page.text


async def enable_totp(session_factory, user_id: int) -> str:
    from app.auth import totp
    from app.auth.service import seal_totp_secret

    secret = totp.new_secret()
    sealed = seal_totp_secret(user_id, secret)
    async with session_factory() as db, db.begin():
        await db.execute(update(User).where(User.id == user_id).values(totp_secret=sealed))
    return secret


async def test_totp_required_when_enabled_and_code_single_use(make_client, session_factory) -> None:
    from app.auth import totp

    user_id = await create_user(session_factory, "admin")
    secret = await enable_totp(session_factory, user_id)
    code = totp.code_at(secret, totp.current_step())

    async with make_client() as client:
        without = await post_login(client, "admin")
        wrong = await post_login(client, "admin", code="000000" if code != "000000" else "111111")
        ok = await post_login(client, "admin", code=code)
    async with make_client(ip="10.3.0.1") as replay:
        reused = await post_login(replay, "admin", code=code)

    assert (without.status_code, wrong.status_code, ok.status_code) == (401, 401, 303)
    assert reused.status_code == 401
    assert "Неверный логин, пароль или код" in without.text


async def test_admin_without_totp_blocked_when_required(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "admin_totp_required", True)
    await create_user(session_factory, "admin")
    await create_user(session_factory, "viewer", role="viewer")
    async with make_client() as client:
        admin = await post_login(client, "admin")
        bad = await post_login(client, "admin", "bad password!!")
        viewer = await post_login(client, "viewer")

    assert admin.status_code == 403
    assert "python -m app users totp admin" in admin.text
    assert bad.status_code == 401
    assert viewer.status_code == 303
    assert await count(session_factory, UserSession) == 1


def test_totp_required_in_prod_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_totp_required", None)
    monkeypatch.setattr(settings, "env", "prod")
    assert settings.totp_required_for_admins
    monkeypatch.setattr(settings, "env", "dev")
    assert not settings.totp_required_for_admins
