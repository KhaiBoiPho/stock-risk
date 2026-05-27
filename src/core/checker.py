import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.alerts.formatter import AlertLevel, format_alert
from src.alerts.telegram import send_message
from src.config.constants import ALERT_TTL_SECONDS, TOTAL_TRADING_MINUTES, VN_TIMEZONE
from src.config.settings import get_settings
from src.core.session import elapsed_minutes, get_session
from src.data.dnse_client import fetch_all_ohlcv
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)
_TZ = ZoneInfo(VN_TIMEZONE)


def _parse_ohlcv(data: dict | None) -> tuple[int, float | None]:
    """Return (cumulative_volume, last_close) from DNSE OHLCV response."""
    if not data or not data.get("t"):
        return 0, None
    vol = sum(v for v in data.get("v", []) if v)
    closes = data.get("c", [])
    return int(vol), (closes[-1] if closes else None)


def calculate_ratio(vol_today: int, vma9: float, elapsed: int) -> float | None:
    if vma9 <= 0 or elapsed <= 0:
        return None
    expected = vma9 * elapsed / TOTAL_TRADING_MINUTES
    return vol_today / expected if expected > 0 else None


def determine_alert_level(
    ratio: float,
    session_name: str,
    ato_threshold: float,
    warning_threshold: float,
    critical_threshold: float,
) -> AlertLevel | None:
    if session_name == "ATO":
        return AlertLevel.EXPLOSION if ratio >= ato_threshold else None
    if ratio >= critical_threshold:
        return AlertLevel.CRITICAL
    if ratio >= warning_threshold:
        return AlertLevel.WARNING
    return None


async def _check_one(
    symbol: str,
    data: dict | None,
    vma9: float | None,
    elapsed: int,
    session_name: str,
    check_time: datetime,
    store: RedisStore,
    s,
) -> None:
    if not vma9 or vma9 <= 0:
        return

    vol_today, last_close = _parse_ohlcv(data)
    ratio = calculate_ratio(vol_today, vma9, elapsed)
    if ratio is None:
        return

    await store.set_vol_today(symbol, vol_today)
    await store.set_last_ratio(symbol, ratio)

    level = determine_alert_level(
        ratio, session_name,
        s.ato_threshold, s.warning_threshold, s.critical_threshold,
    )
    if level is None:
        return

    if await store.is_alerted(symbol, level.value):
        return

    text = format_alert(
        symbol=symbol,
        vol_today=vol_today,
        vma9=vma9,
        ratio=ratio,
        elapsed=elapsed,
        level=level,
        session_name=session_name,
        current_price=last_close,
        check_time=check_time,
    )
    sent = await send_message(text)
    if sent:
        await store.mark_alerted(symbol, level.value, ALERT_TTL_SECONDS)
        logger.info("Alert sent: %s %s ratio=%.2f", symbol, level.value, ratio)


async def run_check(symbols: list[str], store: RedisStore) -> None:
    now = datetime.now(_TZ)
    session = get_session(now.time())
    if session is None:
        logger.debug("Outside trading hours, skipping")
        return

    elapsed = elapsed_minutes(now.time())
    if elapsed <= 0:
        return

    today_start_ts = int(datetime.combine(now.date(), time(9, 0), tzinfo=_TZ).timestamp())
    now_ts = int(now.timestamp())

    logger.info("Checking %d symbols at %s (elapsed=%d min)", len(symbols), now.strftime("%H:%M"), elapsed)
    raw = await fetch_all_ohlcv(symbols, today_start_ts, now_ts, resolution="15")
    vma9_map = await store.get_all_vma9(symbols)
    s = get_settings()

    await asyncio.gather(
        *[
            _check_one(sym, raw.get(sym), vma9_map.get(sym), elapsed, session.name, now, store, s)
            for sym in symbols
        ]
    )
