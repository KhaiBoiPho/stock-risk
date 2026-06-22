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

    # Liquidity filter — skip symbols where BOTH conditions are below threshold
    # NOTE: DNSE API trả giá theo nghìn đồng (e.g. FPT = 74.8 = 74,800 VND)
    # min_vma9_value dùng cùng đơn vị: 10_000_000 nghìn đ = 10 tỷ đồng thực tế
    min_vma9_volume: int = 1_000_000     # 1 triệu cp/phiên
    min_vma9_value: int = 10_000_000     # 10 tỷ đồng/phiên (đơn vị: nghìn đồng × cp)

    # EOD summary filter — actual vol/value cuối phiên (cùng đơn vị DNSE)
    eod_min_volume: int = 1_000_000      # 1 triệu cp
    eod_min_value: int = 10_000_000      # 10 tỷ đồng (nghìn đồng × cp)

    log_level: str = "INFO"


_instance: Settings | None = None


def get_settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
