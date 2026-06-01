import logging

from src.core.checker import run_check
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)


async def check_volume(
    symbols: list[str],
    store: RedisStore,
    exchange_map: dict[str, str] | None = None,
) -> None:
    logger.info("Volume check triggered (%d symbols)", len(symbols))
    try:
        await run_check(symbols, store, exchange_map)
    except Exception as exc:
        logger.error("check_volume failed: %s", exc, exc_info=True)
