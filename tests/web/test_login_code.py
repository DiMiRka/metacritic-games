"""Вход в два шага: пароль, затем код второго фактора только у администратора"""

from datetime import timedelta

from sqlalchemy import func, select, update

from app.auth import cli as users_cli
from app.auth import totp
from app.auth.models import LoginAttempt, LoginChallenge, User, UserSession
from app.auth.service import issue_recovery_codes, seal_totp_secret
from app.core.config import settings
from tests.conftest import PASSWORD, SESSION_COOKIE, create_user

CODE_COOKIE = "login-code"


async def with_totp(session_factory, login: str = "admin", role: str = "admin") -> tuple[int, str]:
    user_id = await create_user(session_factory, login, role=role)
    secret = totp.new_secret()
    async with session_factory() as db, db.begin():
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(totp_secret=seal_totp_secret(user_id, secret, config=settings))
        )
    return user_id, secret


def current_code(secret: str) -> str:
    return totp.code_at(secret, totp.current_step())


async def password_step(client, login: str = "admin", next: str = "/monitor"):
    return await client.post("/login", data={"login": login, "password": PASSWORD, "next": next})


async def count(session_factory, model) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def test_login_form_has_no_code_field(make_client) -> None:
    async with make_client() as client:
        page = (await client.get("/login")).text
    assert 'name="code"' not in page
    assert 'name="password"' in page


async def test_viewer_logs_in_without_code_even_with_secret(make_client, session_factory) -> None:
    await with_totp(session_factory, "reader", role="viewer")
    async with make_client() as client:
        response = await password_step(client, "reader", next="/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in response.cookies
    assert await count(session_factory, LoginChallenge) == 0


async def test_admin_two_step_login(make_client, session_factory) -> None:
    user_id, secret = await with_totp(session_factory)
    async with make_client() as client:
        first = await password_step(client)
        cookie = first.headers["set-cookie"]

        # Пароль верен, но сессии ещё нет: только ожидание кода
        assert first.status_code == 303
        assert first.headers["location"] == "/login/code"
        assert SESSION_COOKIE not in first.cookies
        assert f"{CODE_COOKIE}=" in cookie and "HttpOnly" in cookie
        assert "SameSite=strict" in cookie and "Max-Age=300" in cookie
        assert await count(session_factory, UserSession) == 0
        async with session_factory() as db:
            user = await db.get(User, user_id)
            challenge = await db.scalar(select(LoginChallenge))
        assert user.last_login_at is None
        assert challenge.token_hash != client.cookies[CODE_COOKIE]

        form = (await client.get("/login/code")).text
        assert 'autocomplete="one-time-code"' in form and "autofocus" in form
        assert "inputmode" not in form
        assert 'href="/login"' in form and 'name="next"' not in form

        done = await client.post("/login/code", data={"code": current_code(secret)})

    assert done.status_code == 303
    assert done.headers["location"] == "/monitor"
    assert SESSION_COOKIE in done.cookies
    assert f'{CODE_COOKIE}=""' in done.headers.get_list("set-cookie")[-1] or any(
        c.startswith(f"{CODE_COOKIE}=") and "Max-Age=0" in c
        for c in done.headers.get_list("set-cookie")
    )
    assert await count(session_factory, LoginChallenge) == 0
    async with session_factory() as db:
        assert (await db.get(User, user_id)).last_login_at is not None


async def test_same_totp_code_not_accepted_twice(make_client, session_factory) -> None:
    _, secret = await with_totp(session_factory)
    code = current_code(secret)
    async with make_client() as client:
        await password_step(client)
        assert (await client.post("/login/code", data={"code": code})).status_code == 303
    async with make_client(ip="10.5.0.1") as client:
        await password_step(client)
        replay = await client.post("/login/code", data={"code": code})
    assert replay.status_code == 401


async def test_recovery_code_on_second_step(make_client, session_factory) -> None:
    user_id, _ = await with_totp(session_factory)
    async with session_factory() as db, db.begin():
        codes = await issue_recovery_codes(db, user_id)
    async with make_client() as client:
        await password_step(client)
        ok = await client.post("/login/code", data={"code": codes[0]})
    assert ok.status_code == 303


async def test_wrong_codes_counted_and_challenge_dropped(
    make_client, session_factory, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 100)
    monkeypatch.setattr(settings, "login_max_attempts", 100)
    _, secret = await with_totp(session_factory)
    async with make_client() as client:
        await password_step(client)
        statuses = []
        for _ in range(4):
            response = await client.post("/login/code", data={"code": "000000"})
            statuses.append(response.status_code)
        assert "осталось попыток: 1" in response.text
        last = await client.post("/login/code", data={"code": "000000"})
        after = await client.get("/login/code")
        retry_right_code = await client.post("/login/code", data={"code": current_code(secret)})

    assert statuses == [401, 401, 401, 401]
    assert last.status_code == 303 and last.headers["location"] == "/login?reason=attempts"
    assert after.headers["location"] == "/login?reason=expired"
    # После исчерпания попыток даже верный код не помогает: нужен пароль заново
    assert retry_right_code.headers["location"] == "/login?reason=expired"
    assert await count(session_factory, LoginChallenge) == 0
    assert await count(session_factory, UserSession) == 0
    assert await count(session_factory, LoginAttempt) == 5
    assert "исчерпаны попытки кода для admin" in caplog.text


async def test_login_delay_applies_on_code_step(make_client, session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "login_delay_after", 2)
    monkeypatch.setattr(settings, "login_max_attempts", 100)
    await with_totp(session_factory)
    async with make_client() as client:
        await password_step(client)
        for _ in range(2):
            await client.post("/login/code", data={"code": "000000"})
        delayed = await client.post("/login/code", data={"code": "000000"})
    assert delayed.status_code == 429
    assert "повторите через" in delayed.text


async def test_missing_or_expired_challenge(make_client, session_factory) -> None:
    await with_totp(session_factory)
    async with make_client() as client:
        no_cookie = await client.get("/login/code")
        post_no_cookie = await client.post("/login/code", data={"code": "123456"})
        await password_step(client)
        async with session_factory() as db, db.begin():
            await db.execute(
                update(LoginChallenge).values(expires_at=func.now() - timedelta(seconds=1))
            )
        expired = await client.post("/login/code", data={"code": "123456"})
        message = (await client.get("/login?reason=expired")).text

    for response in (no_cookie, post_no_cookie, expired):
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=expired"
    assert "Время на ввод кода истекло, войдите заново" in message


async def test_new_password_login_replaces_challenge(make_client, session_factory) -> None:
    _, secret = await with_totp(session_factory)
    async with make_client() as first:
        await password_step(first)
        async with make_client(ip="10.6.0.1") as second:
            await password_step(second)
            assert await count(session_factory, LoginChallenge) == 1
            stale = await first.post("/login/code", data={"code": current_code(secret)})
            fresh = await second.post("/login/code", data={"code": current_code(secret)})
    assert stale.headers["location"] == "/login?reason=expired"
    assert fresh.status_code == 303


async def test_next_url_kept_in_challenge_and_sanitized(make_client, session_factory) -> None:
    _, secret = await with_totp(session_factory)
    async with make_client() as client:
        await password_step(client, next="//evil.example/x")
        done = await client.post(
            "/login/code", data={"code": current_code(secret), "next": "/admin"}
        )
    assert done.headers["location"] == "/"


async def test_code_step_rejects_foreign_origin(make_client, session_factory) -> None:
    _, secret = await with_totp(session_factory)
    async with make_client() as client:
        await password_step(client)
        foreign = await client.post(
            "/login/code",
            data={"code": current_code(secret)},
            headers={"Origin": "https://evil.example"},
        )
    assert foreign.status_code == 403
    assert await count(session_factory, UserSession) == 0


async def test_admin_without_totp_enters_directly_when_not_required(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "admin_totp_required", False)
    await create_user(session_factory, "admin")
    async with make_client() as client:
        response = await password_step(client)
    assert response.status_code == 303 and response.headers["location"] == "/monitor"
    assert await count(session_factory, LoginChallenge) == 0


async def test_admin_after_totp_off_enters_directly(
    make_client, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "admin_totp_required", False)
    await with_totp(session_factory)
    monkeypatch.setattr(users_cli, "open_session", session_factory)
    await users_cli.disable_totp("admin")
    async with make_client() as client:
        response = await password_step(client)
        monitor = await client.get("/monitor")
    assert response.status_code == 303 and response.headers["location"] == "/monitor"
    assert monitor.status_code == 200
    assert await count(session_factory, LoginChallenge) == 0
