"""
Test UPCOM symbols fetch + format_alert + gửi Telegram thật.

Usage:
    python scripts/test_upcom.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.constants import VN_TIMEZONE
from src.data.dnse_client import fetch_upcom_symbols
from src.alerts.formatter import AlertLevel, format_alert
from src.alerts.telegram import send_message

_TZ = ZoneInfo(VN_TIMEZONE)


async def main():
    print("Fetching UPCoM symbols…")
    upcom = await fetch_upcom_symbols()
    print(f"  ✓ UPCoM: {len(upcom)} mã")
    print(f"  10 mã đầu: {upcom[:10]}")

    # Kiểm tra F88 có trong list không
    if "F88" in upcom:
        print("  ✓ F88 có trong danh sách UPCoM")
    else:
        print("  ✗ F88 KHÔNG có trong danh sách (kiểm tra lại VPS API)")

    # Test formatter với UPCOM
    now = datetime.now(_TZ)
    msg_upcom = format_alert(
        symbol="F88", vol_today=3_000_000, vma9=800_000,
        ratio=3.5, elapsed=90, level=AlertLevel.CRITICAL,
        session_name="MORNING", current_price=15_200.0,
        check_time=now, exchange="UPCOM",
    )
    print("\n--- Format preview (UPCoM) ---")
    print(msg_upcom)

    # Gửi Telegram
    print("\n--- Gửi Telegram ---")
    header = (
        f"🧪 <b>TEST — UPCoM integration</b>\n"
        f"   UPCoM: {len(upcom)} mã\n"
        f"   F88 trong list: {'✓' if 'F88' in upcom else '✗'}\n"
        f"   {now.strftime('%H:%M:%S %d/%m/%Y')}"
    )
    ok = await send_message(header)
    print(f"  Header: {'✓ sent' if ok else '✗ failed'}")

    ok = await send_message(msg_upcom)
    print(f"  Alert F88: {'✓ sent' if ok else '✗ failed'}")


if __name__ == "__main__":
    asyncio.run(main())
