"""
Replay ngày hôm qua — xem hệ thống sẽ báo mã nào (HOSE + HNX + UPCOM).
Không cần Redis, không gửi Telegram.

Usage:
    python scripts/test_yesterday.py            # hôm qua
    python scripts/test_yesterday.py 2026-06-02  # ngày cụ thể
"""
import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import get_settings
from src.core.checker import calculate_ratio, determine_alert_level, passes_liquidity_filter
from src.core.session import elapsed_minutes, get_session
from src.core.vma9 import compute_vma9
from src.data.dnse_client import (
    fetch_all_ohlcv,
    fetch_hose_symbols,
    fetch_hnx_symbols,
    fetch_upcom_symbols,
)

_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

CHECK_TIMES = [
    time(9,  0), time(9, 15), time(9, 30), time(9, 45),
    time(10, 0), time(10,15), time(10,30), time(10,45),
    time(11, 0), time(11,15), time(11,30),
    time(13, 0), time(13,15), time(13,30), time(13,45),
    time(14, 0), time(14,15), time(14,30), time(14,45),
]


def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _slice(raw: dict | None, cut_ts: int) -> dict | None:
    if not raw or not raw.get("t"):
        return None
    n = sum(1 for ts in raw["t"] if ts <= cut_ts)
    return {k: (v[:n] if isinstance(v, list) else v) for k, v in raw.items()} if n else None


def _parse(data: dict | None) -> tuple[int, float | None]:
    if not data or not data.get("t"):
        return 0, None
    vol = sum(v for v in data.get("v", []) if v)
    closes = data.get("c", [])
    return int(vol), (closes[-1] if closes else None)


async def main():
    # ── Chọn ngày replay ──────────────────────────────────────────────────────
    if len(sys.argv) > 1:
        target_day = date.fromisoformat(sys.argv[1])
    else:
        today = datetime.now(_TZ).date()
        target_day = today - timedelta(days=1)
        # Lùi qua weekend nếu hôm qua là T7/CN
        while target_day.weekday() >= 5:
            target_day -= timedelta(days=1)

    print(f"\n{'='*65}")
    print(f"  REPLAY: {target_day}  (HOSE + HNX + UPCoM)")
    print(f"{'='*65}")

    s = get_settings()

    # ── 1. Load symbols ───────────────────────────────────────────────────────
    print("\n[1/4] Fetch danh sách mã...")
    hose, hnx, upcom = await asyncio.gather(
        fetch_hose_symbols(),
        fetch_hnx_symbols(),
        fetch_upcom_symbols(),
    )
    exchange_map: dict[str, str] = {sym: "HOSE" for sym in hose}
    exchange_map.update({sym: "HNX" for sym in hnx})
    exchange_map.update({sym: "UPCOM" for sym in upcom})
    symbols = sorted(exchange_map.keys())
    print(f"  HoSE: {len(hose)}  |  HNX: {len(hnx)}  |  UPCoM: {len(upcom)}  |  Tổng: {len(symbols)}")

    # ── 2. Tính VMA9 từ daily data trước target_day ───────────────────────────
    print(f"\n[2/4] Tính VMA9 (daily data trước {target_day})...")
    from_date = target_day - timedelta(days=s.vma9_history_fetch_days)
    daily_raw = await fetch_all_ohlcv(
        symbols,
        _ts(from_date, time(0, 0)),
        _ts(target_day - timedelta(days=1), time(23, 59, 59)),
        resolution="1D",
    )
    vma9_map: dict[str, float | None] = {
        sym: compute_vma9(daily_raw.get(sym), s.vma9_min_days, s.vma9_lookback_days)
        for sym in symbols
    }
    n_vma9 = sum(1 for v in vma9_map.values() if v)
    print(f"  VMA9 sẵn sàng: {n_vma9}/{len(symbols)} mã")

    # ── 3. Fetch intraday data của target_day ────────────────────────────────
    print(f"\n[3/4] Fetch intraday 15-min ngày {target_day}...")
    intra_raw = await fetch_all_ohlcv(
        symbols,
        _ts(target_day, time(9, 0)),
        _ts(target_day, time(15, 0)),
        resolution="15",
    )
    n_intra = sum(1 for v in intra_raw.values() if v and v.get("t"))
    print(f"  Mã có data: {n_intra}/{len(symbols)}")

    # ── 4. Simulate detect theo từng check-time ───────────────────────────────
    print(f"\n[4/4] Chạy detector ({len(CHECK_TIMES)} check points)...\n")

    alerted: dict[str, dict] = {}  # sym → {time, ratio, level, exchange}

    for check_t in CHECK_TIMES:
        session = get_session(check_t)
        if session is None:
            continue
        elapsed = elapsed_minutes(check_t)
        if elapsed <= 0:
            continue
        cut_ts = _ts(target_day, check_t)

        for sym in symbols:
            if sym in alerted:
                continue
            vma9 = vma9_map.get(sym)
            if not vma9 or vma9 <= 0:
                continue
            sliced = _slice(intra_raw.get(sym), cut_ts)
            vol, last_close = _parse(sliced)

            if not passes_liquidity_filter(vma9, last_close, s.min_vma9_volume, s.min_vma9_value):
                continue

            ratio = calculate_ratio(vol, vma9, elapsed)
            if ratio is None:
                continue

            level = determine_alert_level(
                ratio, session.name,
                s.ato_threshold, s.warning_threshold, s.critical_threshold,
            )
            if level is None:
                continue

            alerted[sym] = {
                "time": check_t.strftime("%H:%M"),
                "session": session.name,
                "ratio": ratio,
                "level": level.value,
                "exchange": exchange_map[sym],
                "vol": vol,
                "vma9": vma9,
                "price": last_close,
            }

    # ── Kết quả ───────────────────────────────────────────────────────────────
    if not alerted:
        print("  Không có mã nào đạt ngưỡng alert.")
        return

    rows = sorted(alerted.items(), key=lambda x: x[1]["ratio"], reverse=True)

    # Group by exchange
    by_exchange: dict[str, list] = {}
    for sym, info in rows:
        ex = info["exchange"]
        by_exchange.setdefault(ex, []).append((sym, info))

    total = len(alerted)
    print(f"  Tổng mã sẽ được báo: {total}")
    print()

    _LEVEL_ICON = {"explosion": "🔥", "warning": "⚠️ ", "critical": "🚨"}

    for exchange in ["HOSE", "HNX", "UPCOM"]:
        group = by_exchange.get(exchange, [])
        if not group:
            continue
        ex_label = {"HOSE": "HoSE", "HNX": "HNX", "UPCOM": "UPCoM"}[exchange]
        print(f"  ── {ex_label} ({len(group)} mã) {'─'*40}")
        print(f"  {'Mã':<6}  {'Giờ':<5}  {'Session':<9}  {'Ratio':>6}  {'Level':<10}  {'Vol':>12}  {'VMA9':>12}  {'Giá':>8}")
        print(f"  {'─'*95}")
        for sym, info in sorted(group, key=lambda x: x[1]["ratio"], reverse=True):
            icon = _LEVEL_ICON.get(info["level"], "  ")
            price_str = f"{info['price']:>8,.0f}" if info["price"] else "       N/A"
            print(
                f"  {sym:<6}  {info['time']:<5}  {info['session']:<9}  "
                f"{info['ratio']:>5.2f}x  {icon} {info['level']:<8}  "
                f"{info['vol']:>12,}  {int(info['vma9']):>12,}  {price_str}"
            )
        print()

    # Stats tổng hợp
    n_crit     = sum(1 for _, i in rows if i["level"] == "critical")
    n_warn     = sum(1 for _, i in rows if i["level"] == "warning")
    n_expl     = sum(1 for _, i in rows if i["level"] == "explosion")
    n_upcom    = sum(1 for _, i in rows if i["exchange"] == "UPCOM")
    print(f"  Tổng: {total} mã  |  🔥 {n_expl}  🚨 {n_crit}  ⚠️  {n_warn}")
    print(f"  UPCOM trong đó: {n_upcom} mã")
    print()


if __name__ == "__main__":
    asyncio.run(main())
