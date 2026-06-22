"""
Test EOD summary filter with real DNSE data from Thu 18/06 and Fri 19/06.
Full-scan approach: check ALL symbols for spike + EOD liquidity filter.
Does NOT require Redis or Telegram — pure API + logic test.
"""
import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.config.constants import VN_TIMEZONE
from src.config.settings import get_settings
from src.core.eod_summary import _parse_eod, passes_eod_filter
from src.core.vma9 import compute_vma9
from src.data.dnse_client import (
    fetch_all_ohlcv,
    fetch_hose_symbols,
    fetch_hnx_symbols,
    fetch_upcom_symbols,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
_TZ = ZoneInfo(VN_TIMEZONE)

HIGHLIGHT = ["LDG", "AGG", "TTA"]

TEST_DAYS = [
    ("Thu 18/06", datetime(2026, 6, 18, tzinfo=_TZ)),
    ("Fri 19/06", datetime(2026, 6, 19, tzinfo=_TZ)),
    ("Mon 22/06", datetime(2026, 6, 22, tzinfo=_TZ)),
]


async def fetch_vma9_for(symbols: list[str], ref_date: datetime) -> dict[str, float | None]:
    s = get_settings()
    yesterday = (ref_date - timedelta(days=1)).date()
    from_date = ref_date.date() - timedelta(days=s.vma9_history_fetch_days)
    from_ts = int(datetime.combine(from_date, time(0, 0), tzinfo=_TZ).timestamp())
    to_ts = int(datetime.combine(yesterday, time(23, 59, 59), tzinfo=_TZ).timestamp())

    raw = await fetch_all_ohlcv(symbols, from_ts, to_ts, resolution="1D")
    result = {}
    for sym in symbols:
        result[sym] = compute_vma9(raw.get(sym), s.vma9_min_days, s.vma9_lookback_days)
    return result


async def fetch_intraday(symbols: list[str], day: datetime) -> dict[str, dict | None]:
    start_ts = int(datetime.combine(day.date(), time(9, 0), tzinfo=_TZ).timestamp())
    end_ts = int(datetime.combine(day.date(), time(14, 50), tzinfo=_TZ).timestamp())
    return await fetch_all_ohlcv(symbols, start_ts, end_ts, resolution="15")


async def run_eod_sim(symbols: list[str], exchange_map: dict[str, str], day_label: str, day_dt: datetime):
    s = get_settings()

    print(f"\n{'═' * 60}")
    print(f"  {day_label} — {day_dt.strftime('%Y-%m-%d')}  (full scan {len(symbols):,} symbols)")
    print(f"{'═' * 60}")

    vma9_map = await fetch_vma9_for(symbols, day_dt)
    intraday = await fetch_intraday(symbols, day_dt)

    qualified = []
    for sym in symbols:
        vma9 = vma9_map.get(sym)
        if not vma9 or vma9 <= 0:
            continue
        data = intraday.get(sym)
        vol_today, last_close = _parse_eod(data)
        if vol_today == 0:
            continue

        ratio = vol_today / vma9
        if ratio < s.warning_threshold:
            continue

        if not passes_eod_filter(vol_today, last_close, s.eod_min_volume, s.eod_min_value):
            continue

        value_ty = vol_today * last_close / 1_000_000 if last_close else None
        qualified.append({
            "symbol": sym,
            "vol_today": vol_today,
            "vma9": vma9,
            "ratio": ratio,
            "last_close": last_close,
            "value_ty": value_ty,
            "exchange": exchange_map.get(sym, "?"),
        })

    qualified.sort(key=lambda x: x.get("value_ty") or 0, reverse=True)

    print(f"\n  Ket qua: {len(qualified)} ma vao ro (spike >= {s.warning_threshold}x"
          f" + vol >= {s.eod_min_volume:,} CP or value >= 10 ty)")
    print()

    if qualified:
        print(f"  {'Ma':<8} {'San':<6} {'Ratio':>7} {'KL (cp)':>14} {'GT (ty)':>10} {'Gia (VND)':>12}")
        print(f"  {'─'*8} {'─'*6} {'─'*7} {'─'*14} {'─'*10} {'─'*12}")
        for q in qualified:
            price_vnd = q['last_close'] * 1000 if q['last_close'] else 0
            mark = " ★" if q['symbol'] in HIGHLIGHT else ""
            print(f"  {q['symbol']:<8} {q['exchange']:<6} {q['ratio']:>6.2f}x "
                  f"{q['vol_today']:>13,} {q['value_ty']:>9.1f} {price_vnd:>11,.0f}{mark}")

    highlighted_found = [q['symbol'] for q in qualified if q['symbol'] in HIGHLIGHT]
    highlighted_missing = [s for s in HIGHLIGHT if s not in highlighted_found]
    if highlighted_found:
        print(f"\n  ★ Target found: {', '.join(highlighted_found)}")
    if highlighted_missing:
        print(f"\n  ✗ Target missing: {', '.join(highlighted_missing)}")

    return qualified


async def main():
    s = get_settings()
    print("Loading symbols...")
    hose = await fetch_hose_symbols()
    hnx = await fetch_hnx_symbols()
    upcom = await fetch_upcom_symbols()

    exchange_map = {s: "HOSE" for s in hose}
    exchange_map.update({s: "HNX" for s in hnx})
    exchange_map.update({s: "UPCOM" for s in upcom})
    symbols = sorted(exchange_map.keys())

    print(f"Total: {len(symbols):,} symbols (HOSE={len(hose)}, HNX={len(hnx)}, UPCOM={len(upcom)})")
    print(f"Spike threshold: >= {s.warning_threshold}x")
    print(f"EOD filter: vol >= {s.eod_min_volume:,} CP  OR  value >= 10 ty VND")

    for day_label, day_dt in TEST_DAYS:
        await run_eod_sim(symbols, exchange_map, day_label, day_dt)

    # Redis memory estimate
    print(f"\n{'═' * 60}")
    print("  REDIS MEMORY")
    print(f"{'═' * 60}")
    n = len(symbols)
    print(f"\n  Existing daily: ~{n*105/1024:.0f} KB (vma9 + vol_today + last_ratio + alerted)")
    print(f"  New per day:    ~400 B (alerted_today SET, cleared daily)")
    print(f"  New per year:   ~46 KB (eod_basket:DATE, 252 trading days)")
    print(f"  OOM risk:       NONE")


if __name__ == "__main__":
    asyncio.run(main())
