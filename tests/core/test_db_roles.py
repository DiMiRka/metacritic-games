import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db_roles import apply_roles

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
RUN_INSERT = "INSERT INTO runs (trigger, status) VALUES ('schedule', 'running')"
JOB_INSERT = (
    "INSERT INTO jobs (kind, status, priority, attempts, max_attempts) "
    "VALUES ('collect', 'queued', 0, 0, 5)"
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
        (JOB_INSERT, False),
        ("UPDATE jobs SET status = 'done'", False),
        ("DELETE FROM jobs", False),
        ("SELECT count(*) FROM jobs", True),
        ("SELECT retry_dead_job(1)", True),
        ("SELECT * FROM purge_auth_records()", False),
        ("SELECT count(*) FROM run_items", True),
        ("DELETE FROM run_items", False),
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
        (JOB_INSERT, True),
        ("DELETE FROM game_reviews", True),
        ("SELECT * FROM purge_auth_records()", True),
        ("SELECT retry_dead_job(1)", False),
        ("SELECT request_manual_collect(1)", False),
    ],
)
async def test_worker_role(conn, sql, expected) -> None:
    assert await allowed(conn, WORKER, sql) is expected


async def test_default_privileges_cover_future_tables(conn) -> None:
    await conn.execute(text("CREATE TABLE future_table (id serial PRIMARY KEY, v int)"))

    assert await allowed(conn, WORKER, "INSERT INTO future_table (v) VALUES (1)")
    assert await allowed(conn, WEB, "SELECT * FROM future_table")
    assert not await allowed(conn, WEB, "INSERT INTO future_table (v) VALUES (1)")


async def test_manual_collect_function_checks_admin_and_dedupes(conn) -> None:
    ids = {}
    for login, role in (("boss", "admin"), ("guest", "viewer")):
        ids[role] = await conn.scalar(
            text(
                "INSERT INTO users (login, password_hash, role) VALUES (:l, 'h', :r) RETURNING id"
            ),
            {"l": login, "r": role},
        )
    await conn.execute(text(f'SET LOCAL ROLE "{WEB}"'))

    first = (
        await conn.execute(text("SELECT request_manual_collect(:u)"), {"u": ids["admin"]})
    ).scalar()
    second = (
        await conn.execute(text("SELECT request_manual_collect(:u)"), {"u": ids["admin"]})
    ).scalar()
    job = (
        await conn.execute(
            text("SELECT kind, priority, requested_by FROM jobs WHERE id = :id"), {"id": first}
        )
    ).one()

    assert first is not None and second is None
    assert tuple(job) == ("collect", 100, ids["admin"])
    savepoint = await conn.begin_nested()
    with pytest.raises(DBAPIError, match="not an active admin"):
        await conn.execute(text("SELECT request_manual_collect(:u)"), {"u": ids["viewer"]})
    await savepoint.rollback()


async def test_manual_collect_function_respects_cooldown(conn) -> None:
    admin = await conn.scalar(
        text(
            "INSERT INTO users (login, password_hash, role) VALUES ('b', 'h', 'admin') RETURNING id"
        )
    )
    await conn.execute(text(RUN_INSERT))
    await conn.execute(text("UPDATE runs SET status = 'success'"))
    await conn.execute(text(f'SET LOCAL ROLE "{WEB}"'))

    assert (
        await conn.execute(text("SELECT request_manual_collect(:u)"), {"u": admin})
    ).scalar() is None


async def test_purge_auth_records(conn) -> None:
    user = await conn.scalar(
        text(
            "INSERT INTO users (login, password_hash, role) "
            "VALUES ('u', 'h', 'viewer') RETURNING id"
        )
    )
    for token, hours in (("old", -1), ("live", 5)):
        await conn.execute(
            text(
                "INSERT INTO sessions (user_id, token_hash, expires_at) "
                "VALUES (:u, :t, now() + make_interval(hours => :h))"
            ),
            {"u": user, "t": token, "h": hours},
        )
    await conn.execute(
        text(
            "INSERT INTO login_attempts (ip, login, attempted_at) VALUES "
            "('1.1.1.1', 'u', now() - interval '2 days'), ('1.1.1.1', 'u', now())"
        )
    )
    await conn.execute(text(f'SET LOCAL ROLE "{WORKER}"'))

    purged = (await conn.execute(text("SELECT * FROM purge_auth_records()"))).one()

    assert tuple(purged) == (1, 1)


def test_function_constants_match_code() -> None:
    from app.core import db_roles
    from app.crawler import stages
    from app.jobs import queue

    assert db_roles.MANUAL_COLLECT_PRIORITY == stages.MANUAL_COLLECT_PRIORITY
    assert db_roles.COLLECT_DEDUPE_KEY == stages.COLLECT_DEDUPE_KEY
    assert db_roles.JOBS_CHANNEL == queue.CHANNEL
