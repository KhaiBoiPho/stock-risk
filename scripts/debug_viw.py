"""
Debug tại sao VIW không được alert ngày hôm qua.

Usage:
    python scripts/debug_viw.py [SYMBOL] [DATE]
    python scripts/debug_viw.py VIW 2026-06-09
"""
import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.config.constants import TOTAL_TRADING_MINUTES, VN_TIMEZONE
from src.config.settings import get_settings
from src.core.session import elapsed_minutes, get_session
from src.data.dnse_client import fetch_all_ohlcv


def compute_vma9(data, min_days, lookback):
    if not data or not data.get("t"):
        return None
    daily_vols = [v for v in data.get("v", []) if v and v > 0]
    if len(daily_vols) < min_days:
        return None
    recent = daily_vols[-lookback:]
    return sum(recent) / len(recent)


def compute_high20(data):
    if not data or not data.get("t"):
        return None
    highs = [h for h in data.get("h", []) if h and h > 0]
    if not highs:
        return None
    return max(highs[-20:])


# Inline các hàm từ checker.py (tránh import file đang có conflict)
def _parse_ohlcv(data):
    if not data or not data.get("t"):
        return 0, None, None
    vol = sum(v for v in data.get("v", []) if v)
    opens  = data.get("o", [])
    closes = data.get("c", [])
    return int(vol), (opens[0] if opens else None), (closes[-1] if closes else None)


def _price_change_pct(open_price, close_price):
    if not open_price or not close_price or open_price <= 0:
        return None
    return (close_price - open_price) / open_price * 100


def passes_liquidity_filter(vma9, last_close, min_volume, min_value):
    if vma9 >= min_volume:
        return True
    if last_close and last_close > 0:
        return vma9 * last_close >= min_value
    return False


def calculate_ratio(vol_today, vma9, elapsed):
    if vma9 <= 0 or elapsed <= 0:
        return None
    expected = vma9 * elapsed / TOTAL_TRADING_MINUTES
    return vol_today / expected if expected > 0 else None


def determine_alert_level(ratio, session_name, ato_threshold, warning_threshold, critical_threshold):
    if session_name == "ATO":
        return "EXPLOSION" if ratio >= ato_threshold else None
    if ratio >= critical_threshold:
        return "CRITICAL"
    if ratio >= warning_threshold:
        return "WARNING"
    return None

_TZ = ZoneInfo(VN_TIMEZONE)

CHECK_TIMES = [
    time(9, 15),
    time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 0), time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45),
]


def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "VIW"
    sim_date = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(days=1)
    s = get_settings()

    print("=" * 60)
    print(f"  DEBUG: {symbol}  —  ngày {sim_date}")
    print("=" * 60)

    # 1. VMA9
    from_date = sim_date - timedelta(days=40)
    to_date   = sim_date - timedelta(days=1)
    from_ts   = _ts(from_date, time(0, 0))
    to_ts     = _ts(to_date, time(23, 59, 59))

    print(f"\n[1] Fetch daily OHLCV ({from_date} → {to_date}) để tính VMA9...")
    daily_raw = await fetch_all_ohlcv([symbol], from_ts, to_ts, resolution="1D")
    data_daily = daily_raw.get(symbol)

    if not data_daily or not data_daily.get("t"):
        print(f"    ✗ KHÔNG có dữ liệu daily cho {symbol} — có thể mã sai hoặc chưa list")
        return

    daily_vols = [v for v in data_daily.get("v", []) if v and v > 0]
    print(f"    Số phiên có dữ liệu: {len(daily_vols)}")
    print(f"    Khối lượng 9 phiên gần nhất: {[int(v) for v in daily_vols[-9:]]}")

    vma9 = compute_vma9(data_daily, s.vma9_min_days, s.vma9_lookback_days)
    high20 = compute_high20(data_daily)

    if vma9 is None:
        print(f"    ✗ VMA9 = None (cần tối thiểu {s.vma9_min_days} phiên, có {len(daily_vols)} phiên)")
        print(f"      → Đây là lý do {symbol} bị bỏ qua!")
        return

    print(f"    ✓ VMA9 = {vma9:,.0f} cp/phiên")
    print(f"    ✓ High20 = {high20}")

    # 2. Liquidity filter
    print(f"\n[2] Kiểm tra liquidity filter...")
    print(f"    Ngưỡng min_vma9_volume = {s.min_vma9_volume:,} cp")
    print(f"    Ngưỡng min_vma9_value  = {s.min_vma9_value:,} (nghìn đ × cp)")

    # Get last close to estimate value
    closes = [c for c in data_daily.get("c", []) if c and c > 0]
    last_close = closes[-1] if closes else None

    if last_close:
        est_value = vma9 * last_close
        print(f"    Giá đóng cửa gần nhất = {last_close:,.1f} nghìn đ (~{last_close*1000:,.0f} VND)")
        print(f"    VMA9 × giá = {est_value:,.0f}  ({est_value/1000:.1f} tỷ VND thực tế)")
    else:
        est_value = 0
        print(f"    Không có giá close để tính giá trị")

    passes = passes_liquidity_filter(vma9, last_close, s.min_vma9_volume, s.min_vma9_value)
    if not passes:
        print(f"    ✗ KHÔNG pass liquidity filter!")
        print(f"      VMA9 {vma9:,.0f} < {s.min_vma9_volume:,} cp  VÀ")
        print(f"      Giá trị {est_value:,.0f} < {s.min_vma9_value:,}")
        print(f"      → Đây là lý do {symbol} bị bỏ qua!")
    else:
        print(f"    ✓ Pass liquidity filter")

    # 3. Intraday
    print(f"\n[3] Fetch intraday 15-min ngày {sim_date}...")
    day_start_ts = _ts(sim_date, time(9, 0))
    day_end_ts   = _ts(sim_date, time(15, 0))
    intra_raw = await fetch_all_ohlcv([symbol], day_start_ts, day_end_ts, resolution="15")
    raw = intra_raw.get(symbol)

    if not raw or not raw.get("t"):
        print(f"    ✗ KHÔNG có dữ liệu intraday ngày {sim_date}")
        print(f"      → Đây là lý do {symbol} bị bỏ qua!")
        return

    candle_count = len(raw.get("t", []))
    print(f"    ✓ Có {candle_count} nến 15-min")

    # 4. Replay từng mốc
    print(f"\n[4] Replay từng mốc check (Warning >= {s.warning_threshold}x, Critical >= {s.critical_threshold}x)...\n")
    print(f"  {'Giờ':<7}  {'Session':<10}  {'Elapsed':>7}  {'Vol':>12}  {'Ratio':>7}  {'Kết quả'}")
    print(f"  {'-'*7}  {'-'*10}  {'-'*7}  {'-'*12}  {'-'*7}  {'-'*20}")

    any_triggered = False
    for check_t in CHECK_TIMES:
        session = get_session(check_t)
        if session is None:
            continue
        elapsed = elapsed_minutes(check_t)
        if elapsed <= 0:
            continue

        cut_ts = _ts(sim_date, check_t)
        times_ = raw["t"]
        cut_idx = sum(1 for ts in times_ if ts <= cut_ts)
        if cut_idx == 0:
            print(f"  {check_t.strftime('%H:%M'):<7}  {session.name:<10}  {elapsed:>7}  {'(no data)':>12}  {'—':>7}  bỏ qua (chưa có nến)")
            continue

        sliced = {k: (v[:cut_idx] if isinstance(v, list) else v) for k, v in raw.items()}
        vol_today, open_price, cur_close = _parse_ohlcv(sliced)

        ratio = calculate_ratio(vol_today, vma9, elapsed)
        if ratio is None:
            print(f"  {check_t.strftime('%H:%M'):<7}  {session.name:<10}  {elapsed:>7}  {vol_today:>12,}  {'—':>7}  ratio=None")
            continue

        level = determine_alert_level(ratio, session.name, s.ato_threshold, s.warning_threshold, s.critical_threshold)
        level_str = level if level else "—"

        if not passes:
            result = f"BỊ FILTER (liquidity)"
        elif level:
            result = f"✓ SẼ ALERT: {level_str}"
            any_triggered = True
        else:
            result = f"ratio thấp ({ratio:.2f}x)"

        print(f"  {check_t.strftime('%H:%M'):<7}  {session.name:<10}  {elapsed:>7}  {vol_today:>12,}  {ratio:>7.2f}x  {result}")

    print()
    if any_triggered and passes:
        print("  → Hệ thống SẼ alert nếu pass liquidity filter!")
    elif not passes:
        print(f"  → KẾT LUẬN: {symbol} bị bỏ qua do KHÔNG ĐỦ THANH KHOẢN")
        print(f"     Muốn theo dõi VIW cần giảm ngưỡng hoặc thêm whitelist.")
    else:
        print(f"  → KẾT LUẬN: {symbol} pass liquidity nhưng volume không đủ để trigger alert ngày {sim_date}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
