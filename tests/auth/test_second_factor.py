from datetime import timedelta

from sqlalchemy import func, select, update

from app.auth import totp
from app.auth.models import TotpRecoveryCode, User, UserSession
from app.auth.service import (
    issue_recovery_codes,
    load_session,
    rekey_totp_secrets,
    seal_totp_secret,
)
from app.core.config import settings
from tests.conftest import PASSWORD, create_user, login_as

OLD, NEW = "o" * 64, "n" * 64


async def post_login(client, code: str, login: str = "admin"):
    data = {"login": login, "password": PASSWORD, "next": "/monitor", "code": code}
    return await client.post("/login", data=data)


async def with_totp(session_factory) -> tuple[int, str]:
    user_id = await create_user(session_factory, "admin")
    secret = totp.new_secret()
    async with session_factory() as db, db.begin():
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(totp_secret=seal_totp_secret(user_id, secret, config=settings))
        )
    return user_id, secret


async def test_stored_secret_is_encrypted_and_login_works(make_client, session_factory) -> None:
    user_id, secret = await with_totp(session_factory)
    async with session_factory() as db:
        stored = await db.scalar(select(User.totp_secret).where(User.id == user_id))
    assert secret not in stored

    async with make_client() as client:
        ok = await post_login(client, totp.code_at(secret, totp.current_step()))
    assert ok.status_code == 303


async def test_rekey_encrypts_plaintext_and_old_key(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_key", OLD)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    legacy_id = await create_user(session_factory, "legacy")
    old_id = await create_user(session_factory, "old")
    async with session_factory() as db, db.begin():
        await db.execute(update(User).where(User.id == legacy_id).values(totp_secret="PLAIN234"))
        await db.execute(
            update(User)
            .where(User.id == old_id)
            .values(totp_secret=seal_totp_secret(old_id, "OLD2345", config=settings))
        )

    monkeypatch.setattr(settings, "secret_key", NEW)
    monkeypatch.setattr(settings, "secret_key_previous", OLD)
    async with session_factory() as db, db.begin():
        assert await rekey_totp_secrets(db, config=settings) == (2, 0)
    async with session_factory() as db, db.begin():
        assert await rekey_totp_secrets(db, config=settings) == (0, 0)

    # Прежний ключ больше не нужен: всё под текущим
    monkeypatch.setattr(settings, "secret_key_previous", "")
    from app.auth.service import open_totp_secret

    async with session_factory() as db:
        users = {u.login: u for u in (await db.scalars(select(User))).all()}
    assert open_totp_secret(users["legacy"], config=settings) == "PLAIN234"
    assert open_totp_secret(users["old"], config=settings) == "OLD2345"


async def test_rekey_reports_secret_under_unknown_key(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_key", OLD)
    user_id = await create_user(session_factory, "lost")
    async with session_factory() as db, db.begin():
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(totp_secret=seal_totp_secret(user_id, "X", config=settings))
        )
    monkeypatch.setattr(settings, "secret_key", NEW)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    async with session_factory() as db, db.begin():
        assert await rekey_totp_secrets(db, config=settings) == (0, 1)


async def test_recovery_code_single_use(make_client, session_factory) -> None:
    user_id, _ = await with_totp(session_factory)
    async with session_factory() as db, db.begin():
        codes = await issue_recovery_codes(db, user_id)
        stored = (await db.scalars(select(TotpRecoveryCode.code_hash))).all()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 14 and c.count("-") == 2 for c in codes)
    assert not any(c.replace("-", "")[4:] in h for c in codes for h in stored)

    async with make_client() as client:
        first = await post_login(client, codes[0].upper().replace("-", " "))
    async with make_client(ip="10.4.0.1") as client:
        reused = await post_login(client, codes[0])
        wrong = await post_login(client, codes[1][:-1] + ("a" if codes[1][-1] != "a" else "b"))
        second = await post_login(client, codes[1])

    assert first.status_code == 303
    assert (reused.status_code, wrong.status_code) == (401, 401)
    assert second.status_code == 303
    async with session_factory() as db:
        used = await db.scalar(
            select(func.count())
            .select_from(TotpRecoveryCode)
            .where(TotpRecoveryCode.used_at.is_not(None))
        )
    assert used == 2


async def test_new_recovery_codes_replace_old(session_factory) -> None:
    user_id, _ = await with_totp(session_factory)
    async with session_factory() as db, db.begin():
        old = await issue_recovery_codes(db, user_id)
    async with session_factory() as db, db.begin():
        await issue_recovery_codes(db, user_id)
    async with session_factory() as db:
        user = await db.get(User, user_id)
        from app.auth.service import use_recovery_code

        assert not await use_recovery_code(db, user, old[0])


async def test_idle_session_expires(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_idle_minutes", 30)
    user_id = await create_user(session_factory, "admin")
    token = await login_as(session_factory, user_id)
    async with session_factory() as db:
        assert await load_session(db, token, config=settings) is not None

    async with session_factory() as db, db.begin():
        await db.execute(
            update(UserSession).values(
                last_seen_at=UserSession.last_seen_at - timedelta(minutes=31)
            )
        )
    async with session_factory() as db:
        assert await load_session(db, token, config=settings) is None

    monkeypatch.setattr(settings, "session_idle_minutes", 0)
    async with session_factory() as db:
        assert await load_session(db, token, config=settings) is not None


def test_csrf_token_from_previous_key_accepted(monkeypatch) -> None:
    from app.auth.service import csrf_token_for, csrf_valid

    monkeypatch.setattr(settings, "secret_key", OLD)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    old_token = csrf_token_for("session", config=settings)
    monkeypatch.setattr(settings, "secret_key", NEW)
    assert not csrf_valid("session", old_token, config=settings)
    monkeypatch.setattr(settings, "secret_key_previous", OLD)
    assert csrf_valid("session", old_token, config=settings)
    assert csrf_valid("session", csrf_token_for("session", config=settings), config=settings)
