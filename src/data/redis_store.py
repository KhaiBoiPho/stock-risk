import logging

from redis.asyncio import Redis, from_url

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


class RedisStore:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    @classmethod
    async def create(cls) -> "RedisStore":
        s = get_settings()
        auth = f":{s.redis_password}@" if s.redis_password else ""
        url = f"redis://{auth}{s.redis_host}:{s.redis_port}/{s.redis_db}"
        redis: Redis = await from_url(url, encoding="utf-8", decode_responses=True)
        return cls(redis)

    async def close(self) -> None:
        await self._r.aclose()

    # --- VMA9 ---

    async def set_vma9(self, symbol: str, value: float) -> None:
        try:
            await self._r.set(f"vma9:{symbol}", value)
        except Exception as exc:
            logger.error("set vma9:%s: %s", symbol, exc)

    async def set_vma9_bulk(self, pairs: list[tuple[str, float]]) -> None:
        if not pairs:
            return
        try:
            pipe = self._r.pipeline(transaction=False)
            for symbol, value in pairs:
                pipe.set(f"vma9:{symbol}", value)
            await pipe.execute()
        except Exception as exc:
            logger.error("set_vma9_bulk: %s", exc)

    async def get_all_vma9(self, symbols: list[str]) -> dict[str, float | None]:
        try:
            values = await self._r.mget([f"vma9:{sym}" for sym in symbols])
            return {sym: float(v) if v is not None else None for sym, v in zip(symbols, values)}
        except Exception as exc:
            logger.error("mget vma9: %s", exc)
            return {sym: None for sym in symbols}

    # --- Intraday volume (observability) ---

    async def set_vol_today(self, symbol: str, volume: int) -> None:
        try:
            await self._r.set(f"vol_today:{symbol}", volume)
        except Exception as exc:
            logger.error("set vol_today:%s: %s", symbol, exc)

    # --- Alert dedup ---

    async def is_alerted(self, symbol: str, level: str) -> bool:
        try:
            return bool(await self._r.exists(f"alerted:{symbol}:{level}"))
        except Exception as exc:
            logger.error("exists alerted:%s:%s: %s", symbol, level, exc)
            return False

    async def mark_alerted(self, symbol: str, level: str, ttl: int) -> None:
        try:
            await self._r.setex(f"alerted:{symbol}:{level}", ttl, "1")
        except Exception as exc:
            logger.error("setex alerted:%s:%s: %s", symbol, level, exc)

    # --- Last ratio (reference) ---

    async def set_last_ratio(self, symbol: str, ratio: float) -> None:
        try:
            await self._r.set(f"last_ratio:{symbol}", f"{ratio:.4f}")
        except Exception as exc:
            logger.error("set last_ratio:%s: %s", symbol, exc)

    # --- Daily reset ---

    async def reset_daily(self) -> None:
        try:
            deleted = 0
            for pattern in ("vol_today:*", "alerted:*"):
                keys = [k async for k in self._r.scan_iter(pattern, count=500)]
                if keys:
                    pipe = self._r.pipeline(transaction=False)
                    for k in keys:
                        pipe.delete(k)
                    await pipe.execute()
                    deleted += len(keys)
            logger.info("Daily Redis keys cleared (%d keys)", deleted)
        except Exception as exc:
            logger.error("reset_daily: %s", exc)
