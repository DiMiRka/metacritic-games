import os
import secrets
import socket
import warnings
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    # Серверный секрет для HMAC CSRF-токенов, обязателен в prod: openssl rand -hex 32.
    # В dev без него берётся случайный на время жизни процесса
    secret_key: str = ""

    # json в контейнерах, text для чтения глазами при локальной разработке
    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"
    # Пусто: отчёты об исключениях не отправляются
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0

    database_url: str = "postgresql+asyncpg://metacritic:metacritic@localhost:5432/metacritic"
    # Пароли ролей app_web и app_worker, их создаёт python -m app db-roles
    app_web_db_password: str = ""
    app_worker_db_password: str = ""

    timezone: str = "Europe/Moscow"
    batch_size: int = 20
    crawl_interval_minutes: int = 60
    crawl_on_start: bool = True
    manual_run_cooldown_minutes: int = 10
    # Хранение служебных данных в днях, 0: не чистить. Последний сырой ответ игры хранится всегда
    retention_days: int = 30
    raw_retention_days: int = 90
    cleanup_hour: int = 4

    # Очередь задач
    worker_id: str = ""
    job_lease_seconds: int = 300
    job_max_attempts: int = 5
    job_poll_seconds: float = 5.0
    job_retry_base_seconds: float = 30.0
    job_retry_max_seconds: float = 3600.0
    scrape_concurrency: int = 3
    # Устаревший синоним SCRAPE_CONCURRENCY
    concurrency: int | None = None
    summary_concurrency: int = 2
    embed_concurrency: int = 1
    letsplay_concurrency: int = 1
    worker_shutdown_grace_seconds: float = 30.0
    # Файл, который воркер обновляет после каждого удачного heartbeat, для healthcheck в compose
    worker_health_file: str = ""
    # Порт метрик Prometheus воркера во внутренней сети, 0: не поднимать
    worker_metrics_port: int = 0

    # Автоматы защиты от массового сбоя
    breaker_metacritic_threshold: int = 5
    breaker_metacritic_open_seconds: float = 600.0
    breaker_llm_threshold: int = 3
    breaker_llm_open_seconds: float = 1800.0
    breaker_probe_seconds: float = 60.0

    session_ttl_hours: int = 12
    # Неудачные попытки с одного IP под одним логином, после них вход с этого IP закрыт на окно
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    # Под одним логином с любых адресов: после стольких неудач каждая следующая попытка
    # ждёт всё дольше, до LOGIN_DELAY_MAX_SECONDS. Это задержка, а не блокировка
    login_delay_after: int = 5
    login_delay_max_seconds: int = 60
    # На каждой такой по счёту неудаче подряд в журнал пишется предупреждение
    login_alert_threshold: int = 10
    # Второй фактор TOTP для администраторов, по умолчанию обязателен в prod
    admin_totp_required: bool | None = None

    rate_limit_catalog_per_minute: int = 120
    rate_limit_api_per_minute: int = 120
    rate_limit_login_per_minute: int = 20

    request_delay: float = 1.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )

    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    summary_model: str = "claude-haiku-5-5"
    anthropic_api_key: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    # Цена модели резюме в $ за миллион токенов, 0: монитор не показывает стоимость
    llm_price_input_per_mtok: float = 0
    llm_price_output_per_mtok: float = 0
    youtube_api_key: str = ""

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "data/models"

    @model_validator(mode="after")
    def _secret_key(self) -> "Settings":
        if not self.secret_key:
            if self.is_prod:
                raise ValueError("SECRET_KEY обязателен при ENV=prod: openssl rand -hex 32")
            self.secret_key = secrets.token_hex(32)
        return self

    @model_validator(mode="after")
    def _legacy_concurrency(self) -> "Settings":
        if self.concurrency is not None:
            warnings.warn(
                "CONCURRENCY устарела, используйте SCRAPE_CONCURRENCY",
                DeprecationWarning,
                stacklevel=2,
            )
            if "scrape_concurrency" not in self.model_fields_set:
                self.scrape_concurrency = self.concurrency
        return self

    @property
    def worker_identity(self) -> str:
        return self.worker_id or f"{socket.gethostname()}:{os.getpid()}"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def totp_required_for_admins(self) -> bool:
        return self.is_prod if self.admin_totp_required is None else self.admin_totp_required

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()
