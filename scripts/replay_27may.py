"""
Replay 27/05/2026 with the new scoring algorithm.
Prints results to terminal ONLY — does NOT send to Telegram.
"""
import asyncio
import os
import sys
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.config.constants import TOTAL_TRADING_MINUTES, VN_TIMEZONE
from src.core.checker import _parse_ohlcv, _price_change_pct, calculate_ratio, determine_alert_level
from src.core.scorer import compute_score
from src.core.session import get_session, elapsed_minutes
from src.core.vma9 import compute_vma9, compute_high20
from src.data.dnse_client import fetch_all_ohlcv, fetch_hose_symbols
from src.alerts.formatter import format_alert

_TZ = ZoneInfo(VN_TIMEZONE)

SIM_DATE   = date(2026, 5, 27)
# Check every 15 min during trading hours
CHECK_TIMES = [
    time(9, 15),   # ATO result
    time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 0),  time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45),
]

ATO_THRESH  = 3.0
WARN_THRESH = 2.0
CRIT_THRESH = 3.0


def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def main():
    print("=" * 65)
    print(f"  REPLAY  27/05/2026  —  thuật toán scoring mới")
    print("=" * 65)

    # --- 1. Fetch symbol list ---
    print("\n[1/4] Lấy danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # --- 2. Fetch daily OHLCV for VMA9 + High20 ---
    # Use 30 trading days before 27/05 → from ~16/04
    from_date = SIM_DATE - timedelta(days=40)
    to_date   = SIM_DATE - timedelta(days=1)          # day BEFORE sim date
    from_ts   = _ts(from_date, time(0, 0))
    to_ts     = _ts(to_date,   time(23, 59, 59))

    print(f"\n[2/4] Fetch daily OHLCV ({from_date} → {to_date}) cho VMA9 + High20...")
    daily_raw = await fetch_all_ohlcv(symbols, from_ts, to_ts, resolution="1D")
    vma9_map  = {}
    high20_map = {}
    for sym, data in daily_raw.items():
        v = compute_vma9(data, min_days=3, lookback=9)
        if v:
            vma9_map[sym] = v
        h = compute_high20(data)
        if h:
            high20_map[sym] = h
    print(f"      VMA9 có: {len(vma9_map)} mã | High20 có: {len(high20_map)} mã")

    # --- 3. Fetch intraday (15-min) for 27/05 ---
    day_start_ts = _ts(SIM_DATE, time(9, 0))
    day_end_ts   = _ts(SIM_DATE, time(15, 0))

    print(f"\n[3/4] Fetch intraday 15-min ngày {SIM_DATE} cho {len(symbols)} mã + VNINDEX...")
    intra_raw = await fetch_all_ohlcv(symbols, day_start_ts, day_end_ts, resolution="15")

    # VNINDEX uses a separate /ohlcs/index endpoint
    import httpx as _httpx
    from src.config.constants import DNSE_OHLCV_URL
    _vni_url = DNSE_OHLCV_URL.replace("/ohlcs/stock", "/ohlcs/index")
    async with _httpx.AsyncClient(timeout=15.0) as _c:
        _vni_resp = await _c.get(_vni_url, params={
            "symbol": "VNINDEX", "resolution": "15",
            "from": day_start_ts, "to": day_end_ts,
        })
        vnindex_data = _vni_resp.json() if _vni_resp.status_code == 200 else None

    _, vni_open, vni_close = _parse_ohlcv(vnindex_data)
    vni_chg = _price_change_pct(vni_open, vni_close)
    vni_chg_str = f"{vni_chg:+.2f}%" if vni_chg is not None else "N/A"
    print(f"      VNINDEX ngày {SIM_DATE}: {vni_chg_str}")

    # --- 4. Replay ---
    print(f"\n[4/4] Replay theo {len(CHECK_TIMES)} mốc thời gian...\n")

    alerted: dict[str, set[str]] = {}   # sym -> set of levels already alerted
    buy_signals: list[dict] = []
    watch_signals: list[dict] = []

    for check_t in CHECK_TIMES:
        session = get_session(check_t)
        if session is None:
            continue
        elapsed = elapsed_minutes(check_t)
        if elapsed <= 0:
            continue

        # Slice intraday data up to check_t
        cut_ts = _ts(SIM_DATE, check_t)

        for sym in symbols:
            vma9 = vma9_map.get(sym)
            if not vma9:
                continue

            raw = intra_raw.get(sym)
            if not raw or not raw.get("t"):
                continue

            # Slice candles up to cut_ts
            times_  = raw["t"]
            cut_idx = sum(1 for ts in times_ if ts <= cut_ts)
            if cut_idx == 0:
                continue
            sliced = {k: (v[:cut_idx] if isinstance(v, list) else v) for k, v in raw.items()}

            vol_today, open_price, last_close = _parse_ohlcv(sliced)
            ratio = calculate_ratio(vol_today, vma9, elapsed)
            if ratio is None:
                continue

            level = determine_alert_level(ratio, session.name, ATO_THRESH, WARN_THRESH, CRIT_THRESH)
            if level is None:
                continue

            # Anti-spam: skip if already alerted this level
            if level.value in alerted.get(sym, set()):
                continue
            alerted.setdefault(sym, set()).add(level.value)

            price_chg    = _price_change_pct(open_price, last_close)
            high20       = high20_map.get(sym)
            is_breakout20 = bool(high20 and last_close and last_close > high20)
            rs = (price_chg - vni_chg) if (price_chg is not None and vni_chg is not None) else None

            score = compute_score(ratio, price_chg, is_breakout20, rs)
            if score.signal is None:
                continue

            check_dt = datetime.combine(SIM_DATE, check_t, tzinfo=_TZ)
            text = format_alert(
                symbol=sym,
                vol_today=vol_today,
                vma9=vma9,
                ratio=ratio,
                elapsed=elapsed,
                level=level,
                session_name=session.name,
                open_price=open_price,
                current_price=last_close,
                check_time=check_dt,
                price_change_pct=price_chg,
                is_breakout20=is_breakout20,
                rs_vs_index=rs,
                score=score,
            )
            print(_strip_html(text))
            print()

            entry = {
                "sym": sym, "time": check_t.strftime("%H:%M"),
                "ratio": ratio, "score": score.total,
                "signal": score.signal,
                "price_chg": price_chg,
                "rs": rs,
                "breakout": is_breakout20,
                "level": level.value,
            }
            if score.total >= 4:
                buy_signals.append(entry)
            else:
                watch_signals.append(entry)

    # --- Summary ---
    print("\n" + "=" * 65)
    print(f"  TÓM TẮT  27/05/2026  (VNINDEX: {vni_chg_str})")
    print("=" * 65)

    if buy_signals:
        print(f"\n🎯 BUY SIGNAL ({len(buy_signals)} mã):")
        print(f"  {'Mã':<6}  {'Giờ':<6}  {'Ratio':>6}  {'Score':>5}  {'Giá%':>7}  {'RS%':>7}  {'BO':>3}  {'Level'}")
        print(f"  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*3}  {'-'*8}")
        for e in sorted(buy_signals, key=lambda x: -x["score"]):
            price_str = f"{e['price_chg']:+.2f}%" if e["price_chg"] is not None else "  N/A "
            rs_str    = f"{e['rs']:+.2f}%"         if e["rs"]         is not None else "  N/A "
            bo_str    = "✅" if e["breakout"] else "  "
            print(f"  {e['sym']:<6}  {e['time']:<6}  {e['ratio']:>6.2f}x  {e['score']:>5}/6  {price_str:>7}  {rs_str:>7}  {bo_str:>3}  {e['level']}")
    else:
        print("\n  (Không có BUY SIGNAL nào)")

    if watch_signals:
        print(f"\n👀 WATCH ({len(watch_signals)} mã):")
        print(f"  {'Mã':<6}  {'Giờ':<6}  {'Ratio':>6}  {'Score':>5}  {'Giá%':>7}  {'RS%':>7}  {'BO':>3}")
        print(f"  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*3}")
        for e in sorted(watch_signals, key=lambda x: (-x["score"], -x["ratio"])):
            price_str = f"{e['price_chg']:+.2f}%" if e["price_chg"] is not None else "  N/A "
            rs_str    = f"{e['rs']:+.2f}%"         if e["rs"]         is not None else "  N/A "
            bo_str    = "✅" if e["breakout"] else "  "
            print(f"  {e['sym']:<6}  {e['time']:<6}  {e['ratio']:>6.2f}x  {e['score']:>5}/6  {price_str:>7}  {rs_str:>7}  {bo_str:>3}")

    print(f"\n  Tổng: {len(buy_signals)} BUY + {len(watch_signals)} WATCH")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
