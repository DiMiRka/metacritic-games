"""Переменные окружения сервисов: каждому только своё"""

import re
from pathlib import Path

import yaml

from app.core.config import Settings

ROOT = Path(__file__).parents[1]
COMPOSE = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.yml").read_text("utf-8"))
SECRETS = {"ANTHROPIC_API_KEY", "LLM_API_KEY", "YOUTUBE_API_KEY"}
TELEGRAM = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
ROLE_PASSWORDS = {"APP_WEB_DB_PASSWORD", "APP_WORKER_DB_PASSWORD", "POSTGRES_PASSWORD"}


def env(service: str) -> set[str]:
    return set(COMPOSE["services"][service].get("environment") or {})


def test_no_shared_env_file() -> None:
    for name, service in COMPOSE["services"].items():
        assert "env_file" not in service, name


def test_api_keys_only_in_worker() -> None:
    assert env("worker") >= SECRETS
    assert not SECRETS & (env("web") | env("migrate"))


def test_role_passwords_only_in_migrate() -> None:
    assert not ROLE_PASSWORDS & (env("web") | env("worker"))
    assert {"APP_WEB_DB_PASSWORD", "APP_WORKER_DB_PASSWORD"} <= env("migrate")


def test_every_setting_reaches_some_service() -> None:
    passed = env("web") | env("worker") | env("migrate")
    missing = {name.upper() for name in Settings.model_fields} - passed
    assert not missing, f"добавьте в docker-compose.yml: {sorted(missing)}"


def test_telegram_only_in_alertmanager() -> None:
    assert env("alertmanager") == TELEGRAM | {"DEADMAN_PING_URL"}
    for name in ("web", "worker", "migrate", "prometheus"):
        assert not TELEGRAM & env(name), name


def test_monitoring_ports_only_on_loopback() -> None:
    for name in ("prometheus", "alertmanager"):
        for port in COMPOSE["services"][name]["ports"]:
            assert port.startswith("127.0.0.1:"), (name, port)


def test_secret_key_only_in_web() -> None:
    assert "SECRET_KEY" in env("web")
    for name in ("worker", "migrate", "prometheus", "alertmanager"):
        assert "SECRET_KEY" not in env(name), name


def test_owner_password_only_in_migrate_and_backup() -> None:
    owners = {
        name
        for name, service in COMPOSE["services"].items()
        if "PGPASSWORD" in (service.get("environment") or {})
        or "POSTGRES_PASSWORD" in str(service.get("environment") or {})
    }
    assert owners == {"db", "migrate", "backup"}


def test_offsite_encrypts_and_has_no_private_key() -> None:
    environment = env("backup-offsite")
    assert "BACKUP_AGE_RECIPIENTS" in environment
    assert not {name for name in environment if "IDENTITY" in name or "PRIVATE" in name}
    assert COMPOSE["services"]["backup-offsite"]["build"]["target"] == "offsite"


def test_web_metrics_port_not_behind_caddy() -> None:
    caddyfile = (Path(__file__).parents[1] / "Caddyfile").read_text("utf-8")
    assert "9101" not in caddyfile


def test_env_example_lists_every_setting() -> None:
    example = (Path(__file__).parents[1] / ".env.example").read_text("utf-8")
    names = {line.split("=")[0].lstrip("# ") for line in example.splitlines() if "=" in line}
    skip = {"DATABASE_URL", "CONCURRENCY"}
    missing = {name.upper() for name in Settings.model_fields} - names - skip
    assert not missing, f"добавьте в .env.example: {sorted(missing)}"


def test_offsite_keys_only_in_offsite() -> None:
    keys = {"RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID", "RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY"}
    assert keys <= env("backup-offsite")
    for name in COMPOSE["services"]:
        if name != "backup-offsite":
            assert not keys & env(name), name


# Внешние образы: точная версия и digest. Обновляет их Dependabot, а не тихий pull
PINNED = re.compile(r"[\w./-]+:[\w.-]*\d[\w.-]*@sha256:[0-9a-f]{64}")


def external_images() -> dict[str, str]:
    images = {
        name: service["image"]
        for name, service in COMPOSE["services"].items()
        if "image" in service and "build" not in service
    }
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    stages = set(re.findall(r"^FROM \S+ AS (\S+)", dockerfile, re.M))
    for ref in re.findall(r"^FROM (\S+)", dockerfile, re.M):
        if ref not in stages:
            images[f"Dockerfile {ref.split('@')[0]}"] = ref
    return images


def test_external_images_pinned_by_version_and_digest() -> None:
    loose = {name: ref for name, ref in external_images().items() if not PINNED.fullmatch(ref)}
    assert not loose, f"закрепите версию и digest: {loose}"
    assert not any(
        ref.split("@")[0].endswith((":latest", ":2", ":pg16")) for ref in external_images().values()
    )


def test_ci_postgres_matches_compose() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    db = COMPOSE["services"]["db"]["image"]
    for job in ("test", "e2e"):
        assert ci["jobs"][job]["services"]["db"]["image"] == db, job
