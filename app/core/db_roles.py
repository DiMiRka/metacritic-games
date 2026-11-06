"""Роли базы с минимальными правами: python -m app db-roles

Выполняется владельцем схемы после миграций (сервис migrate) и идемпотентна: создаёт роли
или обновляет их пароли из .env, сбрасывает и заново выдаёт права на таблицы, пересоздаёт
функции SECURITY DEFINER и задаёт права по умолчанию для таблиц будущих миграций.

Писать в очередь сайт может только через функции: поставить ручной collect и повторить
dead-задачу. Воркер чистит сессии и попытки входа тоже через функцию, без прав на эти таблицы
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings, settings
from app.core.db import default_database

WEB_ROLE = "app_web"
WORKER_ROLE = "app_worker"

WORKER_TABLES = (
    "games",
    "game_platforms",
    "review_summaries",
    "letsplays",
    "crawl_state",
    "runs",
    "run_items",
    "worker_heartbeats",
    "jobs",
    "game_reviews",
    "breakers",
    "raw_pages",
    "score_history",
)
WEB_READ_TABLES = (
    "games",
    "game_platforms",
    "review_summaries",
    "letsplays",
    "runs",
    "crawl_state",
    "worker_heartbeats",
    "run_items",
    "breakers",
    "score_history",
    "jobs",
    "backup_runs",
)
WEB_WRITE_TABLES = ("users", "sessions", "login_attempts", "totp_recovery_codes")
DML = "SELECT, INSERT, UPDATE, DELETE"

# Должны совпадать с crawler.stages и jobs.queue, это проверяет тест
MANUAL_COLLECT_PRIORITY = 100
COLLECT_DEDUPE_KEY = "collect"
JOBS_CHANNEL = "jobs"
# Попытки входа хранятся не меньше суток: по ним монитор показывает серии неудачных входов
MIN_LOGIN_ATTEMPTS_KEEP_MINUTES = 24 * 60

WEB_FUNCTIONS = ("request_manual_collect(integer)", "retry_dead_job(bigint)")
WORKER_FUNCTIONS = ("purge_auth_records()",)


def function_statements(config: Settings) -> list[str]:
    """Функции с правами владельца: роль получает EXECUTE и не может больше, чем делает функция.
    Настройки подставляются при создании, поэтому их не подменить в обход db-roles
    """
    max_attempts = int(config.job_max_attempts)
    cooldown = int(config.manual_run_cooldown_minutes)
    keep = max(int(config.login_window_minutes), MIN_LOGIN_ATTEMPTS_KEEP_MINUTES)
    header = "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public"
    return [
        f"""
CREATE OR REPLACE FUNCTION request_manual_collect(p_user_id integer) RETURNS bigint
{header} AS $fn$
DECLARE
    new_id bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM users WHERE id = p_user_id AND role = 'admin' AND is_active
    ) THEN
        RAISE EXCEPTION 'user % is not an active admin', p_user_id USING ERRCODE = '42501';
    END IF;
    -- Пауза между ручными запусками: сайт проверяет её сам, здесь её нельзя обойти
    IF EXISTS (SELECT 1 FROM runs WHERE started_at > now() - make_interval(mins => {cooldown})) THEN
        RETURN NULL;
    END IF;
    INSERT INTO jobs (kind, payload, status, priority, attempts, max_attempts, dedupe_key,
                      requested_by)
    VALUES ('collect', '{{"trigger": "manual"}}', 'queued', {MANUAL_COLLECT_PRIORITY}, 0,
            {max_attempts}, '{COLLECT_DEDUPE_KEY}', p_user_id)
    ON CONFLICT (dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING
    RETURNING id INTO new_id;
    IF new_id IS NOT NULL THEN
        PERFORM pg_notify('{JOBS_CHANNEL}', 'collect');
    END IF;
    RETURN new_id;
END
$fn$""",
        f"""
CREATE OR REPLACE FUNCTION retry_dead_job(p_job_id bigint) RETURNS boolean
{header} AS $fn$
DECLARE
    changed integer;
BEGIN
    UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), finished_at = NULL
    WHERE id = p_job_id AND status = 'dead';
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed > 0 THEN
        PERFORM pg_notify('{JOBS_CHANNEL}', 'retry');
    END IF;
    RETURN changed > 0;
END
$fn$""",
        f"""
CREATE OR REPLACE FUNCTION purge_auth_records(OUT sessions integer, OUT login_attempts integer)
{header} AS $fn$
BEGIN
    DELETE FROM sessions s WHERE s.expires_at < now();
    GET DIAGNOSTICS sessions = ROW_COUNT;
    DELETE FROM login_attempts a WHERE a.attempted_at < now() - make_interval(mins => {keep});
    GET DIAGNOSTICS login_attempts = ROW_COUNT;
END
$fn$""",
        *(
            f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC"
            for name in (*WEB_FUNCTIONS, *WORKER_FUNCTIONS)
        ),
    ]


async def create_functions(conn: AsyncConnection, config: Settings) -> None:
    for statement in function_statements(config):
        await conn.execute(text(statement))


async def _ensure_role(conn: AsyncConnection, role: str, password: str) -> None:
    exists = await conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
    verb = "ALTER ROLE %I WITH" if exists else "CREATE ROLE %I WITH"
    # format() экранирует имя и пароль на стороне Postgres
    options = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L"
    statement = await conn.scalar(
        text(f"SELECT format('{verb} {options}', CAST(:r AS text), CAST(:p AS text))"),
        {"r": role, "p": password},
    )
    await conn.execute(text(statement))


async def _sequences(conn: AsyncConnection, tables: tuple[str, ...]) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT pg_get_serial_sequence(table_name, 'id') FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'id' "
            "AND table_name = ANY(CAST(:tables AS text[]))"
        ),
        {"tables": list(tables)},
    )
    return [seq for (seq,) in rows if seq]


async def apply_roles(
    conn: AsyncConnection,
    web_password: str,
    worker_password: str,
    web_role: str = WEB_ROLE,
    worker_role: str = WORKER_ROLE,
    *,
    config: Settings,
) -> None:
    await _ensure_role(conn, web_role, web_password)
    await _ensure_role(conn, worker_role, worker_password)
    owner = await conn.scalar(text("SELECT current_user"))
    database = await conn.scalar(text("SELECT current_database()"))
    roles = f'"{web_role}", "{worker_role}"'

    await create_functions(conn, config)
    statements = [
        f'GRANT CONNECT ON DATABASE "{database}" TO {roles}',
        f"GRANT USAGE ON SCHEMA public TO {roles}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}",
        f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {roles}",
        f'GRANT EXECUTE ON FUNCTION {", ".join(WEB_FUNCTIONS)} TO "{web_role}"',
        f'GRANT EXECUTE ON FUNCTION {", ".join(WORKER_FUNCTIONS)} TO "{worker_role}"',
        f'GRANT {DML} ON {", ".join(WORKER_TABLES)} TO "{worker_role}"',
        # Воркер проверяет, что пользователь из NOTIFY существует, хеши паролей ему не видны
        f'GRANT SELECT (id) ON users TO "{worker_role}"',
        f'GRANT SELECT ON {", ".join(WEB_READ_TABLES)} TO "{web_role}"',
        f'GRANT {DML} ON {", ".join(WEB_WRITE_TABLES)} TO "{web_role}"',
    ]
    for seq in await _sequences(conn, WORKER_TABLES):
        statements.append(f'GRANT USAGE ON SEQUENCE {seq} TO "{worker_role}"')
    for seq in await _sequences(conn, WEB_WRITE_TABLES):
        statements.append(f'GRANT USAGE ON SEQUENCE {seq} TO "{web_role}"')

    # Таблицы будущих миграций: воркеру рабочие права, сайту только чтение,
    # запись для сайта новая миграция выдаёт явно и вносит таблицу в списки выше
    default = f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public'
    statements += [
        f'{default} GRANT {DML} ON TABLES TO "{worker_role}"',
        f'{default} GRANT SELECT ON TABLES TO "{web_role}"',
        f'{default} GRANT USAGE ON SEQUENCES TO "{worker_role}"',
    ]
    for statement in statements:
        await conn.execute(text(statement))


async def setup_roles(config: Settings = settings) -> None:
    if not config.app_web_db_password or not config.app_worker_db_password:
        raise SystemExit("Задайте APP_WEB_DB_PASSWORD и APP_WORKER_DB_PASSWORD в .env")
    async with default_database().engine.begin() as conn:
        await apply_roles(
            conn, config.app_web_db_password, config.app_worker_db_password, config=config
        )
    print(f"Роли {WEB_ROLE} и {WORKER_ROLE} настроены")
