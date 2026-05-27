from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # DNSE API
    dnse_concurrency: int = 50
    dnse_timeout: float = 10.0
    dnse_max_retries: int = 3

    # Alert thresholds
    ato_threshold: float = 3.0
    warning_threshold: float = 2.0
    critical_threshold: float = 3.0

    # VMA9
    vma9_min_days: int = 3
    vma9_lookback_days: int = 9
    vma9_history_fetch_days: int = 25

    log_level: str = "INFO"


_instance: Settings | None = None


def get_settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
