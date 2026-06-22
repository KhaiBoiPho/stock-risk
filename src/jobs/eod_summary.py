import logging

from src.core.eod_summary import run_eod_summary
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)


async def eod_summary(
    symbols: list[str],
    store: RedisStore,
    exchange_map: dict[str, str] | None = None,
) -> None:
    logger.info("EOD summary triggered (%d symbols)", len(symbols))
    try:
        await run_eod_summary(symbols, store, exchange_map)
    except Exception as exc:
        logger.error("eod_summary failed: %s", exc, exc_info=True)
