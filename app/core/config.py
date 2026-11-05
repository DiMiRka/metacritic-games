from typing import Literal
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "prod"] = "dev"

    database_url: str = "postgresql+asyncpg://metacritic:metacritic@localhost:5432/metacritic"
    # Пароли ролей app_web и app_worker, их создаёт python -m app db-roles
    app_web_db_password: str = ""
    app_worker_db_password: str = ""

    timezone: str = "Europe/Moscow"
    batch_size: int = 20
    concurrency: int = 3
    crawl_interval_minutes: int = 60
    crawl_on_start: bool = True
    manual_run_cooldown_minutes: int = 10

    session_ttl_hours: int = 12
    login_max_attempts: int = 5
    login_window_minutes: int = 15

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
    youtube_api_key: str = ""

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "data/models"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()
