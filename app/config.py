from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://metacritic:metacritic@localhost:5432/metacritic"

    timezone: str = "Europe/Moscow"
    batch_size: int = 20
    concurrency: int = 3
    crawl_interval_minutes: int = 60
    crawl_on_start: bool = True

    request_delay: float = 1.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )

    anthropic_api_key: str = ""
    summary_model: str = "claude-haiku-5-5"
    youtube_api_key: str = ""

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "data/models"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()
