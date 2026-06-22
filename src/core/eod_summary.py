import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.alerts.formatter import format_eod_summary
from src.alerts.telegram import send_message
from src.config.constants import VN_TIMEZONE
from src.config.settings import get_settings
from src.data.dnse_client import fetch_all_ohlcv
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)
_TZ = ZoneInfo(VN_TIMEZONE)


def _parse_eod(data: dict | None) -> tuple[int, float | None]:
    if not data or not data.get("t"):
        return 0, None
    vol = sum(v for v in data.get("v", []) if v)
    closes = data.get("c", [])
    return int(vol), (closes[-1] if closes else None)


def passes_eod_filter(
    vol_today: int,
    last_close: float | None,
    min_volume: int,
    min_value: int,
) -> bool:
    if vol_today >= min_volume:
        return True
    if last_close and last_close > 0:
        return vol_today * last_close >= min_value
    return False


async def run_eod_summary(
    symbols: list[str],
    store: RedisStore,
    exchange_map: dict[str, str] | None = None,
) -> None:
    now = datetime.now(_TZ)
    s = get_settings()
    ex_map = exchange_map or {}

    logger.info("EOD summary: scanning %d symbols", len(symbols))

    today_start_ts = int(datetime.combine(now.date(), time(9, 0), tzinfo=_TZ).timestamp())
    now_ts = int(now.timestamp())

    raw = await fetch_all_ohlcv(symbols, today_start_ts, now_ts, resolution="15")
    vma9_map = await store.get_all_vma9(symbols)

    qualified: list[dict] = []
    for sym in symbols:
        vma9 = vma9_map.get(sym)
        if not vma9 or vma9 <= 0:
            continue

        vol_today, last_close = _parse_eod(raw.get(sym))
        if vol_today == 0:
            continue

        ratio = vol_today / vma9 if vma9 > 0 else 0
        if ratio < s.warning_threshold:
            continue

        if not passes_eod_filter(vol_today, last_close, s.eod_min_volume, s.eod_min_value):
            logger.debug("EOD skip %s: vol=%d, price=%s (passes spike but not liquidity)", sym, vol_today, last_close)
            continue

        value = vol_today * last_close / 1_000_000 if last_close else None
        qualified.append({
            "symbol": sym,
            "vol_today": vol_today,
            "vma9": vma9,
            "ratio": ratio,
            "last_close": last_close,
            "value_ty": value,
            "exchange": ex_map.get(sym, "HOSE"),
        })

    date_str = now.strftime("%Y-%m-%d")
    basket_symbols = [q["symbol"] for q in qualified]
    await store.set_eod_basket(basket_symbols, date_str)

    text = format_eod_summary(
        qualified=qualified,
        total_scanned=len(symbols),
        check_time=now,
    )
    await send_message(text)
    logger.info("EOD summary sent: %d/%d symbols qualified", len(qualified), len(symbols))
