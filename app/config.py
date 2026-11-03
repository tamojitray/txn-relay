from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Transaction Webhook Service"
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str | None = None
    auto_create_schema: bool = True

    processing_delay_seconds: float = Field(default=30.0, ge=0)
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0)
    worker_batch_size: int = Field(default=10, ge=1, le=100)
    worker_concurrency: int = Field(default=10, ge=1, le=100)
    processing_lease_seconds: int = Field(default=120, ge=30)
    max_processing_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_seconds: int = Field(default=5, ge=1, le=3600)


@lru_cache
def get_settings() -> Settings:
    return Settings()
