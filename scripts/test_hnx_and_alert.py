"""
Test HNX symbols fetch + format_alert cho cả HOSE và HNX + gửi Telegram thật.

Usage:
    python scripts/test_hnx_and_alert.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.constants import VN_TIMEZONE
from src.data.dnse_client import fetch_hose_symbols, fetch_hnx_symbols
from src.alerts.formatter import AlertLevel, format_alert
from src.alerts.telegram import send_message

_TZ = ZoneInfo(VN_TIMEZONE)


def _test_format():
    now = datetime.now(_TZ)
    cases = [
        ("VNM",  "HOSE", AlertLevel.WARNING,   2.5),
        ("ACB",  "HNX",  AlertLevel.CRITICAL,  3.2),
        ("HPG",  "HOSE", AlertLevel.EXPLOSION,  4.1),
        ("SHB",  "HNX",  AlertLevel.WARNING,   2.1),
    ]
    print("\n--- Format preview ---")
    for symbol, exchange, level, ratio in cases:
        msg = format_alert(
            symbol=symbol, vol_today=5_000_000, vma9=2_000_000,
            ratio=ratio, elapsed=60, level=level,
            session_name="MORNING", current_price=24_500.0,
            check_time=now, exchange=exchange,
        )
        print(msg)
        print()
    return [
        format_alert(
            symbol=s, vol_today=5_000_000, vma9=2_000_000,
            ratio=r, elapsed=60, level=lv,
            session_name="MORNING", current_price=24_500.0,
            check_time=now, exchange=ex,
        )
        for s, ex, lv, r in cases
    ]


async def main():
    # 1. Fetch symbols
    print("Fetching HoSE symbols…")
    hose = await fetch_hose_symbols()
    print(f"  ✓ HoSE: {len(hose)} mã — 5 mã đầu: {hose[:5]}")

    print("Fetching HNX symbols…")
    hnx = await fetch_hnx_symbols()
    print(f"  ✓ HNX : {len(hnx)} mã — 5 mã đầu: {hnx[:5]}")

    exchange_map = {s: "HOSE" for s in hose}
    exchange_map.update({s: "HNX" for s in hnx})
    all_symbols = sorted(exchange_map.keys())
    print(f"\n  Tổng: {len(all_symbols)} mã ({len(hose)} HOSE + {len(hnx)} HNX)")

    # 2. Test formatter
    messages = _test_format()

    # 3. Gửi Telegram thật
    print("--- Gửi Telegram ---")
    now = datetime.now(_TZ)
    header = (
        f"🧪 <b>TEST — HoSE + HNX integration</b>\n"
        f"   HoSE: {len(hose)} mã | HNX: {len(hnx)} mã | Tổng: {len(all_symbols)} mã\n"
        f"   {now.strftime('%H:%M:%S %d/%m/%Y')}"
    )
    ok = await send_message(header)
    print(f"  Header: {'✓ sent' if ok else '✗ failed'}")

    for msg in messages:
        ok = await send_message(msg)
        first_line = msg.splitlines()[0]
        print(f"  {first_line[:60]}: {'✓' if ok else '✗'}")


if __name__ == "__main__":
    asyncio.run(main())
