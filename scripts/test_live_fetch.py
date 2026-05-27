"""
Test live DNSE API fetch + VMA9 + ratio calculation for a sample of symbols.
Does NOT require Redis or Telegram — prints results to stdout.

Usage:
    python scripts/test_live_fetch.py [--symbols VNM,FPT,HPG] [--days 4]
"""
import asyncio
import sys
import argparse
from datetime import datetime, date, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")

from src.config.constants import VN_TIMEZONE, TOTAL_TRADING_MINUTES
from src.core.vma9 import compute_vma9
from src.core.session import elapsed_minutes, get_session
from src.core.checker import calculate_ratio, determine_alert_level
from src.alerts.formatter import AlertLevel
from src.data.dnse_client import fetch_all_ohlcv

_TZ = ZoneInfo(VN_TIMEZONE)

DEFAULT_SYMBOLS = ["VNM", "FPT", "HPG", "MWG", "VCB", "TCB", "ACB", "SSI", "DGC", "GMD"]

ATO_THRESH   = 3.0
WARN_THRESH  = 2.0
CRIT_THRESH  = 3.0

LEVEL_ICON = {
    AlertLevel.EXPLOSION: "🔥",
    AlertLevel.WARNING:   "⚠️ ",
    AlertLevel.CRITICAL:  "🚨",
    None:                 "   ",
}


async def fetch_vma9_data(symbols: list[str], lookback_days: int = 25) -> dict[str, dict | None]:
    today = datetime.now(_TZ).date()
    from_date = today - timedelta(days=lookback_days)
    from_ts = int(datetime.combine(from_date, time(0, 0), tzinfo=_TZ).timestamp())
    to_ts   = int(datetime.combine(today - timedelta(days=1), time(23, 59, 59), tzinfo=_TZ).timestamp())
    return await fetch_all_ohlcv(symbols, from_ts, to_ts, resolution="1D")


async def simulate_intraday(
    symbols: list[str],
    sim_date: date,
    check_times: list[time],
) -> None:
    """Replay a single trading day for given symbols at each check time."""
    from_ts = int(datetime.combine(sim_date, time(9, 0), tzinfo=_TZ).timestamp())
    to_ts   = int(datetime.combine(sim_date, time(14, 45, 59), tzinfo=_TZ).timestamp())

    print(f"\n  Fetching 15-min candles for {sim_date} …", end="", flush=True)
    intraday_raw = await fetch_all_ohlcv(symbols, from_ts, to_ts, resolution="15")
    print(" done")

    # VMA9 (use data up to day before sim_date)
    vma9_raw = await fetch_vma9_data(symbols, lookback_days=30)
    vma9_map: dict[str, float | None] = {}
    for sym, data in vma9_raw.items():
        vma9_map[sym] = compute_vma9(data, min_days=3, lookback=9)

    print(f"\n  {'Symbol':<8} {'VMA9':>12}  ", end="")
    for t in check_times:
        print(f"  {t.strftime('%H:%M')}", end="")
    print()
    print("  " + "-" * (22 + len(check_times) * 8))

    for sym in symbols:
        intra = intraday_raw.get(sym)
        vma9  = vma9_map.get(sym)

        if not intra or not intra.get("t"):
            print(f"  {sym:<8} {'no data':>12}")
            continue

        timestamps = intra.get("t", [])
        volumes    = intra.get("v", [])
        vma9_str   = f"{int(vma9):,}" if vma9 else "N/A"
        print(f"  {sym:<8} {vma9_str:>12}  ", end="")

        for check_t in check_times:
            check_ts = int(datetime.combine(sim_date, check_t, tzinfo=_TZ).timestamp())
            # Sum volumes up to check_ts
            vol_today = sum(
                v for ts, v in zip(timestamps, volumes)
                if ts <= check_ts and v
            )
            elapsed = elapsed_minutes(check_t)
            session = get_session(check_t)

            if session is None or elapsed <= 0 or not vma9:
                print(f"  {'—':>6}", end="")
                continue

            ratio = calculate_ratio(vol_today, vma9, elapsed)
            if ratio is None:
                print(f"  {'—':>6}", end="")
                continue

            level = determine_alert_level(ratio, session.name, ATO_THRESH, WARN_THRESH, CRIT_THRESH)
            icon  = LEVEL_ICON[level]
            print(f"  {icon}{ratio:>4.1f}x", end="")

        print()


async def main(symbols: list[str], days: int) -> None:
    today = datetime.now(_TZ).date()

    print("=" * 70)
    print(f"DNSE Live Fetch Test — {len(symbols)} symbols, last {days} trading days")
    print(f"Symbols: {', '.join(symbols)}")
    print("=" * 70)

    # Check times matching the scheduler
    check_times = [
        time(9, 15), time(9, 30), time(9, 45),
        time(10, 0), time(10, 15), time(10, 30), time(10, 45),
        time(11, 0), time(11, 15),
        time(13, 0), time(13, 15), time(13, 30), time(13, 45),
        time(14, 0), time(14, 15), time(14, 30), time(14, 45),
    ]

    # Walk back `days` calendar days and pick trading days
    sim_dates: list[date] = []
    d = today - timedelta(days=1)  # start from yesterday
    while len(sim_dates) < days:
        if d.weekday() < 5:        # Mon–Fri (skip weekends; holidays not filtered)
            sim_dates.append(d)
        d -= timedelta(days=1)
    sim_dates.reverse()

    for sim_date in sim_dates:
        print(f"\n{'─'*70}")
        print(f"  Ngày: {sim_date}  ({sim_date.strftime('%A')})")
        await simulate_intraday(symbols, sim_date, check_times)

    print(f"\n{'='*70}")
    print("Legend: 🔥 Explosion (ATO ≥3x)  ⚠️  Warning (2–3x)  🚨 Critical (≥3x)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated list of symbols")
    parser.add_argument("--days", type=int, default=4,
                        help="Number of trading days to simulate")
    args = parser.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(main(syms, args.days))
