import logging

from src.core.vma9 import update_vma9_all
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)


async def update_vma9(symbols: list[str], store: RedisStore) -> None:
    logger.info("VMA9 update triggered (%d symbols)", len(symbols))
    try:
        await update_vma9_all(symbols, store)
    except Exception as exc:
        logger.error("update_vma9 failed: %s", exc, exc_info=True)
