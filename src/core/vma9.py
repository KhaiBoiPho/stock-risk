import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.config.constants import VN_TIMEZONE
from src.config.settings import get_settings
from src.data.dnse_client import fetch_all_ohlcv
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)
_TZ = ZoneInfo(VN_TIMEZONE)


def compute_vma9(data: dict | None, min_days: int, lookback: int) -> float | None:
    """Return VMA9 from DNSE daily OHLCV response, or None if insufficient data."""
    if not data or not data.get("t"):
        return None

    daily_vols = [v for v in data.get("v", []) if v and v > 0]

    if len(daily_vols) < min_days:
        return None

    recent = daily_vols[-lookback:]
    return sum(recent) / len(recent)


async def update_vma9_all(symbols: list[str], store: RedisStore) -> int:
    """Fetch daily OHLCV for all symbols and persist VMA9 to Redis. Returns count updated."""
    s = get_settings()
    today = datetime.now(_TZ).date()
    yesterday = today - timedelta(days=1)
    from_date = today - timedelta(days=s.vma9_history_fetch_days)

    from_ts = int(datetime.combine(from_date, time(0, 0), tzinfo=_TZ).timestamp())
    to_ts = int(datetime.combine(yesterday, time(23, 59, 59), tzinfo=_TZ).timestamp())

    logger.info("Fetching daily OHLCV for VMA9 (%d symbols, %s → %s)", len(symbols), from_date, yesterday)
    raw = await fetch_all_ohlcv(symbols, from_ts, to_ts, resolution="1D")

    vma9_pairs: list[tuple[str, float]] = []
    for symbol, data in raw.items():
        vma9 = compute_vma9(data, s.vma9_min_days, s.vma9_lookback_days)
        if vma9 is not None:
            vma9_pairs.append((symbol, vma9))
        else:
            logger.debug("No VMA9 data for %s", symbol)

    await store.set_vma9_bulk(vma9_pairs)
    logger.info("VMA9 updated: %d/%d symbols", len(vma9_pairs), len(symbols))
    return len(vma9_pairs)
