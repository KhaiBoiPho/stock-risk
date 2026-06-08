#!/usr/bin/env python3
"""
Replay toàn bộ một ngày giao dịch — kiểm tra mã nào đủ điều kiện alert.
KHÔNG gửi Telegram. Chạy độc lập, không cần Redis.

Usage:
    python scripts/replay_day.py [YYYY-MM-DD] [SYMBOL1,SYMBOL2,...]

    YYYY-MM-DD : ngày cần replay (mặc định: hôm nay)
    SYMBOL,... : danh sách mã cần xem chi tiết (mặc định: TPB)

Ví dụ:
    python scripts/replay_day.py
    python scripts/replay_day.py 2026-06-06
    python scripts/replay_day.py 2026-06-06 TPB,VPB,MBB
"""

import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.constants import TOTAL_TRADING_MINUTES, VN_TIMEZONE
from src.config.settings import get_settings
from src.core.checker import (
    calculate_ratio,
    determine_alert_level,
    passes_liquidity_filter,
)
from src.core.session import elapsed_minutes, get_session
from src.core.vma9 import compute_vma9
from src.data.dnse_client import fetch_all_ohlcv

_TZ = ZoneInfo(VN_TIMEZONE)

_EMOJI = {"explosion": "🔥", "warning": "⚠️ ", "critical": "🚨"}
_LABEL = {"explosion": "EXPLOSION", "warning": "WARNING ", "critical": "CRITICAL"}
_EXCH  = {"HOSE": "HSX", "HNX": "HNX", "UPCOM": "OTC"}

# Tất cả mốc check theo cron (hour in 9,10,11,13,14 × minute in 0,15,30,45)
_ALL_SLOTS = [
    time(h, m)
    for h in [9, 10, 11, 13, 14]
    for m in [0, 15, 30, 45]
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_symbols() -> tuple[list[str], dict[str, str]]:
    """Đọc symbol từ cache file. Trả về (sorted_symbols, exchange_map)."""
    root = Path(__file__).resolve().parent.parent / "data"
    exchange_map: dict[str, str] = {}
    for fname, exch in [
        ("symbols_hose.txt",  "HOSE"),
        ("symbols_hnx.txt",   "HNX"),
        ("symbols_upcom.txt", "UPCOM"),
    ]:
        p = root / fname
        if p.exists():
            for line in p.read_text().splitlines():
                sym = line.strip()
                if sym:
                    exchange_map[sym] = exch
    return sorted(exchange_map.keys()), exchange_map


def _valid_slots() -> list[time]:
    """Các mốc check hợp lệ trong ngày (có session, elapsed > 0)."""
    return [t for t in _ALL_SLOTS if get_session(t) is not None and elapsed_minutes(t) > 0]


def _slice_ohlcv(data: dict | None, up_to_ts: int) -> dict | None:
    """Cắt OHLCV chỉ giữ candle có t < up_to_ts (strict).

    DNSE dùng start_time làm timestamp. Candle t=09:30 covers 09:30–09:45,
    chưa complete khi check lúc 09:30:xx → dùng strict < để loại candle đang chạy.
    """
    if not data or not data.get("t"):
        return None
    n = sum(1 for ts in data["t"] if ts < up_to_ts)
    if n == 0:
        return None
    return {k: v[:n] for k, v in data.items() if isinstance(v, list)}


def _parse(data: dict | None) -> tuple[int, float | None]:
    if not data or not data.get("t"):
        return 0, None
    vol = sum(v for v in data.get("v", []) if v)
    closes = data.get("c", [])
    return int(vol), (closes[-1] if closes else None)


def _sep(char: str = "─", n: int = 70) -> str:
    return char * n


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------

async def replay(trade_date: date, watch: list[str]) -> None:
    cfg = get_settings()
    symbols, exchange_map = _load_symbols()

    print(f"\n{'='*70}")
    print(f"  REPLAY  {trade_date.strftime('%d/%m/%Y')}  —  {len(symbols)} mã (HoSE + HNX + UPCoM)")
    print(f"{'='*70}")

    # ── 1. Fetch daily OHLCV → tính VMA9 ──────────────────────────────────
    yesterday   = trade_date - timedelta(days=1)
    from_date   = trade_date - timedelta(days=cfg.vma9_history_fetch_days)
    from_ts     = int(datetime.combine(from_date,  time(0, 0),        tzinfo=_TZ).timestamp())
    to_ts_daily = int(datetime.combine(yesterday,  time(23, 59, 59),  tzinfo=_TZ).timestamp())

    print(f"\n[1/2] VMA9: fetch daily OHLCV  {from_date} → {yesterday} ...")
    daily_raw = await fetch_all_ohlcv(symbols, from_ts, to_ts_daily, resolution="1D")

    vma9_map: dict[str, float | None] = {
        sym: compute_vma9(daily_raw.get(sym), cfg.vma9_min_days, cfg.vma9_lookback_days)
        for sym in symbols
    }
    n_vma9   = sum(1 for v in vma9_map.values() if v is not None)
    missing  = [s for s, v in vma9_map.items() if v is None]
    print(f"    VMA9 OK: {n_vma9}/{len(symbols)}  |  thiếu: {len(missing)}")
    if missing:
        shown = missing[:15]
        more  = f" ... (+{len(missing)-15})" if len(missing) > 15 else ""
        print(f"    ⚠️  Thiếu VMA9: {shown}{more}")

    # ── 2. Fetch intraday 15-min (toàn ngày) ──────────────────────────────
    day_start_ts = int(datetime.combine(trade_date, time(9,  0),  tzinfo=_TZ).timestamp())
    day_end_ts   = int(datetime.combine(trade_date, time(14, 46), tzinfo=_TZ).timestamp())

    print(f"\n[2/2] Intraday: fetch 15-min  {trade_date} 09:00→14:46 ...")
    intraday = await fetch_all_ohlcv(symbols, day_start_ts, day_end_ts, resolution="15")
    n_candles = sum(1 for d in intraday.values() if d and d.get("t"))
    print(f"    Có dữ liệu: {n_candles}/{len(symbols)} mã")

    # ── 3. Duyệt từng mốc ─────────────────────────────────────────────────
    # all_alerts: list of (slot_str, sym, level_str, ratio, vol, vma9_val, price)
    all_alerts: list[tuple[str, str, str, float, int, float, float | None]] = []
    valid_slots = _valid_slots()

    print(f"\n{_sep()}")
    print(f"{'Slot':>6}  {'Phiên':<10} {'Elapsed':>7}  Alerts")
    print(_sep())

    for slot in valid_slots:
        slot_dt  = datetime.combine(trade_date, slot, tzinfo=_TZ)
        slot_ts  = int(slot_dt.timestamp())
        elapsed  = elapsed_minutes(slot)
        session  = get_session(slot)
        slot_str = slot.strftime("%H:%M")

        fired: list[tuple[str, str, float, int, float, float | None]] = []

        for sym in symbols:
            vma9_val = vma9_map.get(sym)
            if not vma9_val or vma9_val <= 0:
                continue

            data     = _slice_ohlcv(intraday.get(sym), slot_ts)
            vol, px  = _parse(data)

            if not passes_liquidity_filter(vma9_val, px, cfg.min_vma9_volume, cfg.min_vma9_value):
                continue

            ratio = calculate_ratio(vol, vma9_val, elapsed)
            if ratio is None:
                continue

            level = determine_alert_level(
                ratio, session.name,
                cfg.ato_threshold, cfg.warning_threshold, cfg.critical_threshold,
            )
            if level is None:
                continue

            fired.append((sym, level.value, ratio, vol, vma9_val, px))
            all_alerts.append((slot_str, sym, level.value, ratio, vol, vma9_val, px))

        n = len(fired)
        print(f"{slot_str:>6}  {session.name:<10} {elapsed:>4}/{TOTAL_TRADING_MINUTES}p  — {n} alerts" + ("" if n else "  (không có)"))

        if fired:
            fired.sort(key=lambda x: x[2], reverse=True)
            for sym, lv, ratio, vol, vma9_val, px in fired:
                exch      = _EXCH.get(exchange_map.get(sym, "HOSE"), "HSX")
                price_str = f"{px * 1000:>10,.0f}đ" if px else "        N/A"
                print(
                    f"         {_EMOJI[lv]} {_LABEL[lv]}  [{exch}] {sym:<6}  "
                    f"ratio={ratio:>5.2f}x  vol={vol:>12,}  vma9={int(vma9_val):>12,}  {price_str}"
                )

    # ── 4. Tổng kết ───────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"TÓM TẮT  {trade_date.strftime('%d/%m/%Y')}")
    print(f"{'='*70}")

    if not all_alerts:
        print("Không có alert nào trong ngày.")
    else:
        syms_fired = sorted(set(sym for _, sym, *_ in all_alerts))
        print(f"Tổng lần alert : {len(all_alerts)}")
        print(f"Số mã có alert : {len(syms_fired)}")
        print(f"Danh sách mã   : {', '.join(syms_fired)}\n")

        print(f"{'Mã':<8} {'Sàn':<5} {'Alert':>5} {'MaxRatio':>9}  {'Lần đầu':>7}  Levels")
        print(_sep("─", 55))
        sym_stats: dict[str, dict] = {}
        for slot_str, sym, lv, ratio, vol, vma9_val, px in all_alerts:
            st = sym_stats.setdefault(sym, {"count": 0, "max": 0.0, "first": slot_str, "levels": set()})
            st["count"] += 1
            st["max"]    = max(st["max"], ratio)
            st["levels"].add(lv)

        for sym in sorted(sym_stats, key=lambda x: sym_stats[x]["max"], reverse=True):
            st   = sym_stats[sym]
            exch = exchange_map.get(sym, "HOSE")
            lvls = "+".join(sorted(st["levels"]))
            print(f"{sym:<8} {exch:<5} {st['count']:>5}  {st['max']:>8.2f}x  {st['first']:>7}  {lvls}")

    # ── 5. Chi tiết mã cần kiểm tra ───────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"CHI TIẾT MÃ WATCH: {', '.join(watch)}")
    print(f"{'='*70}")

    for sym in watch:
        sym = sym.upper()
        vma9_val    = vma9_map.get(sym)
        sym_alerts  = [(sl, lv, r, v, vm, p) for sl, sy, lv, r, v, vm, p in all_alerts if sy == sym]
        print(f"\n{_sep('─', 50)}")
        print(f"  {sym}  [{exchange_map.get(sym, '?')}]")
        print(_sep("─", 50))

        if vma9_val is None:
            print(f"  ❌ VMA9 = None  →  bỏ qua hoàn toàn trong ngày")
            print(f"     Kiểm tra: không đủ {cfg.vma9_min_days} ngày dữ liệu, hoặc DNSE lỗi khi fetch")
            continue

        print(f"  VMA9 = {int(vma9_val):,} cp/phiên")

        # Kiểm tra liquidity một lần để biết có bị filter không
        liq_ok = None
        for slot in valid_slots:
            slot_ts  = int(datetime.combine(trade_date, slot, tzinfo=_TZ).timestamp())
            data     = _slice_ohlcv(intraday.get(sym), slot_ts)
            _, px    = _parse(data)
            liq_ok   = passes_liquidity_filter(vma9_val, px, cfg.min_vma9_volume, cfg.min_vma9_value)
            if px:
                break

        if not liq_ok:
            print(f"  ❌ Không qua liquidity filter (VMA9={int(vma9_val):,} < {cfg.min_vma9_volume:,} cp/phiên)")

        if sym_alerts:
            print(f"  Đã alert {len(sym_alerts)} lần:")
            for sl, lv, ratio, vol, vm, px in sym_alerts:
                price_str = f"{px*1000:,.0f}đ" if px else "N/A"
                print(f"    {_EMOJI[lv]} {sl}  {_LABEL[lv]}  ratio={ratio:.2f}x  vol={vol:,}  {price_str}")
        else:
            print(f"  ⚠️  Không alert — ratio tại từng mốc:")
            print(f"  {'Slot':>6}  {'Phiên':<10} {'Elapsed':>7}  {'Vol':>14}  {'Ratio':>7}  Ghi chú")
            for slot in valid_slots:
                slot_ts  = int(datetime.combine(trade_date, slot, tzinfo=_TZ).timestamp())
                elapsed  = elapsed_minutes(slot)
                session  = get_session(slot)
                data     = _slice_ohlcv(intraday.get(sym), slot_ts)
                vol, px  = _parse(data)
                liq      = passes_liquidity_filter(vma9_val, px, cfg.min_vma9_volume, cfg.min_vma9_value)
                ratio    = calculate_ratio(vol, vma9_val, elapsed) if liq else None
                note     = ""
                if not liq:
                    note = "❌ liquidity fail"
                elif ratio is None:
                    note = "❌ ratio=None"
                elif ratio >= cfg.critical_threshold:
                    note = f"🚨 >= {cfg.critical_threshold}x"
                elif ratio >= cfg.warning_threshold:
                    note = f"⚠️  >= {cfg.warning_threshold}x"
                ratio_str = f"{ratio:.2f}x" if ratio is not None else "  N/A"
                print(f"  {slot.strftime('%H:%M'):>6}  {session.name:<10} {elapsed:>4}/{TOTAL_TRADING_MINUTES}  {vol:>14,}  {ratio_str:>7}  {note}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    target_date = date.today()
    watch_list  = ["TPB"]

    if len(sys.argv) >= 2:
        try:
            target_date = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Lỗi: ngày không hợp lệ '{sys.argv[1]}'. Dùng định dạng YYYY-MM-DD")
            sys.exit(1)

    if len(sys.argv) >= 3:
        watch_list = [s.strip().upper() for s in sys.argv[2].split(",") if s.strip()]

    asyncio.run(replay(target_date, watch_list))
