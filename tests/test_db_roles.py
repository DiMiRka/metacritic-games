import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db_roles import apply_roles

WEB = "test_app_web"
WORKER = "test_app_worker"


async def allowed(conn, role: str, sql: str) -> bool:
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
        await conn.execute(text(sql))
        return True
    except DBAPIError as exc:
        assert "permission denied" in str(exc) or "must be owner" in str(exc)
        return False
    finally:
        await savepoint.rollback()


@pytest.fixture
async def conn(db_engine, session_factory):
    async with db_engine.connect() as connection:
        await connection.begin()
        for _ in range(2):
            await apply_roles(connection, "web-pass", "worker-pass", WEB, WORKER)
        yield connection
        await connection.rollback()


GAME_INSERT = "INSERT INTO games (slug, title) VALUES ('x', 'X')"
RUN_INSERT = (
    "INSERT INTO runs (trigger, status, processed, created, updated) "
    "VALUES ('schedule', 'running', 0, 0, 0)"
)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT count(*) FROM games", True),
        ("SELECT count(*) FROM runs", True),
        ("SELECT count(*) FROM worker_heartbeats", True),
        (GAME_INSERT, False),
        (RUN_INSERT, False),
        ("DELETE FROM crawl_state", False),
        ("INSERT INTO users (login, password_hash, role) VALUES ('u', 'h', 'viewer')", True),
        ("SELECT count(*) FROM sessions", True),
        ("INSERT INTO login_attempts (ip, login) VALUES ('1.1.1.1', 'u')", True),
        ("CREATE TABLE web_ddl (id int)", False),
    ],
)
async def test_web_role(conn, sql, expected) -> None:
    assert await allowed(conn, WEB, sql) is expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (GAME_INSERT, True),
        (RUN_INSERT, True),
        ("UPDATE crawl_state SET next_page = 1", True),
        ("SELECT id FROM users", True),
        ("SELECT password_hash FROM users", False),
        ("INSERT INTO users (login, password_hash, role) VALUES ('u', 'h', 'admin')", False),
        ("SELECT count(*) FROM sessions", False),
        ("SELECT count(*) FROM login_attempts", False),
        ("CREATE TABLE worker_ddl (id int)", False),
        ("DROP TABLE games", False),
    ],
)
async def test_worker_role(conn, sql, expected) -> None:
    assert await allowed(conn, WORKER, sql) is expected


async def test_default_privileges_cover_future_tables(conn) -> None:
    await conn.execute(text("CREATE TABLE future_table (id serial PRIMARY KEY, v int)"))

    assert await allowed(conn, WORKER, "INSERT INTO future_table (v) VALUES (1)")
    assert await allowed(conn, WEB, "SELECT * FROM future_table")
    assert not await allowed(conn, WEB, "INSERT INTO future_table (v) VALUES (1)")
