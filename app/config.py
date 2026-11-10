from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from environment variables or .env, each prefixed with BATCH_."""

    model_config = SettingsConfigDict(
        env_prefix="BATCH_",
        env_file=".env",
        env_ignore_empty=True,  # a blank `BATCH_MODEL=` means "use the default"
        extra="ignore",
    )

    api_key: str  # required: no key, no start
    model: str = "gpt-oss-120b"
    base_url: str = "https://api.cerebras.ai/v1"
    request_timeout: float = 120

    database_url: str = "postgresql+psycopg://batch:batch@localhost:5433/batch"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "extraction"

    # Shared by every worker. Cerebras' developer tier allows 5/minute and
    # 150/hour; 2.5/minute stays under both, so a long batch never hits a 429.
    requests_per_minute: float = 2.5
    max_attempts: int = 3

    # USD per million tokens, used to record the cost of every job.
    input_cost_per_mtok: float = 0.35
    output_cost_per_mtok: float = 0.75


@lru_cache
def get_settings() -> Settings:
    return Settings()
