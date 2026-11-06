"""Переменные окружения сервисов: каждому только своё"""

from pathlib import Path

import yaml

from app.core.config import Settings

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
    assert env("alertmanager") == TELEGRAM
    for name in ("web", "worker", "migrate", "prometheus"):
        assert not TELEGRAM & env(name), name


def test_monitoring_ports_only_on_loopback() -> None:
    for name in ("prometheus", "alertmanager"):
        for port in COMPOSE["services"][name]["ports"]:
            assert port.startswith("127.0.0.1:"), (name, port)
