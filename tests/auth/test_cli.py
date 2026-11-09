import contextlib

import pytest
from sqlalchemy import func, select

from app.auth import cli as users_cli
from app.auth import service as auth_module
from app.auth.models import User, UserSession
from tests.conftest import PASSWORD, create_user, login_as


async def count(session_factory, model) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


@contextlib.contextmanager
def patched_cli(monkeypatch, session_factory, *passwords: str):
    answers = iter(passwords)
    monkeypatch.setattr(users_cli.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(users_cli, "open_session", session_factory)
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


async def test_cli_totp_enable_requires_valid_code(monkeypatch, session_factory, capsys) -> None:
    from app.auth import totp

    user_id = await create_user(session_factory, "root")
    await login_as(session_factory, user_id)
    monkeypatch.setattr(users_cli, "open_session", session_factory)

    monkeypatch.setattr(users_cli, "ask_code", lambda: "000000")
    monkeypatch.setattr(totp, "match_step", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        await users_cli.enable_totp("root")
    monkeypatch.undo()

    def answer() -> str:
        secret = capsys.readouterr().out.split("Ключ: ")[-1].split()[0]
        return totp.code_at(secret, totp.current_step())

    monkeypatch.setattr(users_cli, "open_session", session_factory)
    monkeypatch.setattr(users_cli, "ask_code", answer)
    await users_cli.enable_totp("root")
    out = capsys.readouterr().out
    async with session_factory() as db:
        user = await db.get(User, user_id)
    assert user.totp_secret.startswith("v1.") and user.totp_last_step
    assert "Резервные коды" in out
    assert out.count("-") >= 20

    await users_cli.disable_totp("root")
    async with session_factory() as db:
        user = await db.get(User, user_id)
    assert user.totp_secret is None
    assert await count(session_factory, UserSession) == 0


async def test_cli_totp_only_for_admins(monkeypatch, session_factory) -> None:
    await create_user(session_factory, "reader", role="viewer")
    monkeypatch.setattr(users_cli, "open_session", session_factory)
    with pytest.raises(SystemExit, match="только администраторам"):
        await users_cli.enable_totp("reader")


async def test_cli_rejects_short_or_mismatched_password(monkeypatch, session_factory) -> None:
    with patched_cli(monkeypatch, session_factory, "short", "short"), pytest.raises(SystemExit):
        await users_cli.create_admin("root")
    with (
        patched_cli(monkeypatch, session_factory, PASSWORD, PASSWORD + "!"),
        pytest.raises(SystemExit),
    ):
        await users_cli.create_admin("root")
    assert await count(session_factory, User) == 0
