import logging

from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)


async def reset_daily(store: RedisStore) -> None:
    logger.info("Running daily reset")
    await store.reset_daily()
