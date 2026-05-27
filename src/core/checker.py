import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.alerts.formatter import AlertLevel, format_alert
from src.alerts.telegram import send_message
from src.config.constants import ALERT_TTL_SECONDS, TOTAL_TRADING_MINUTES, VN_TIMEZONE
from src.config.settings import get_settings
from src.core.scorer import compute_score
from src.core.session import elapsed_minutes, get_session
from src.data.dnse_client import fetch_all_ohlcv
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)
_TZ = ZoneInfo(VN_TIMEZONE)


def _parse_ohlcv(data: dict | None) -> tuple[int, float | None, float | None]:
    """Return (cumulative_volume, open_price_today, last_close)."""
    if not data or not data.get("t"):
        return 0, None, None
    vol = sum(v for v in data.get("v", []) if v)
    opens  = data.get("o", [])
    closes = data.get("c", [])
    return int(vol), (opens[0] if opens else None), (closes[-1] if closes else None)


def _price_change_pct(open_price: float | None, close_price: float | None) -> float | None:
    if not open_price or not close_price or open_price <= 0:
        return None
    return (close_price - open_price) / open_price * 100


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
    high20: float | None,
    elapsed: int,
    session_name: str,
    check_time: datetime,
    vnindex_change_pct: float | None,
    store: RedisStore,
    s,
) -> None:
    if not vma9 or vma9 <= 0:
        return

    vol_today, open_price, last_close = _parse_ohlcv(data)
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

    price_chg = _price_change_pct(open_price, last_close)
    is_breakout20 = bool(high20 and last_close and last_close > high20)
    rs = (price_chg - vnindex_change_pct) if (price_chg is not None and vnindex_change_pct is not None) else None

    score = compute_score(ratio, price_chg, is_breakout20, rs)

    text = format_alert(
        symbol=symbol,
        vol_today=vol_today,
        vma9=vma9,
        ratio=ratio,
        elapsed=elapsed,
        level=level,
        session_name=session_name,
        open_price=open_price,
        current_price=last_close,
        check_time=check_time,
        price_change_pct=price_chg,
        is_breakout20=is_breakout20,
        rs_vs_index=rs,
        score=score,
    )
    sent = await send_message(text)
    if sent:
        await store.mark_alerted(symbol, level.value, ALERT_TTL_SECONDS)
        logger.info("Alert sent: %s %s ratio=%.2f score=%d/%s",
                    symbol, level.value, ratio, score.total, score.signal or "—")


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

    # Fetch intraday data cho tất cả symbols + VNINDEX cùng một batch
    all_symbols = symbols + ["VNINDEX"]
    raw = await fetch_all_ohlcv(all_symbols, today_start_ts, now_ts, resolution="15")

    # Tính % thay đổi VN-Index hôm nay
    vnindex_data = raw.pop("VNINDEX", None)
    _, vnindex_open, vnindex_close = _parse_ohlcv(vnindex_data)
    vnindex_change_pct = _price_change_pct(vnindex_open, vnindex_close)
    if vnindex_change_pct is not None:
        logger.debug("VNINDEX change today: %.2f%%", vnindex_change_pct)

    vma9_map   = await store.get_all_vma9(symbols)
    high20_map = await store.get_all_high20(symbols)
    s = get_settings()

    await asyncio.gather(
        *[
            _check_one(
                sym, raw.get(sym), vma9_map.get(sym), high20_map.get(sym),
                elapsed, session.name, now, vnindex_change_pct, store, s,
            )
            for sym in symbols
        ]
    )
