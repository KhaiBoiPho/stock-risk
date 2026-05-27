"""
Backtest: Volume spike + scoring + filters + exit strategy.

Signal days: Apr 7 – May 27, 2026 (~7 tuần thực tế)
Buy price  : close price tại thời điểm phát hiện tín hiệu
Exit rules : Stop loss | Take profit | T+2 close (so sánh nhiều mức)

Filters:
  A. Không filter (baseline)
  B. Market filter : VNINDEX xanh cùng ngày
  C. Trend filter  : VNINDEX uptrend 5 ngày
  D. B + C

Exit sweep: SL -3%, -5%, -7% × TP +5%, +7% để tìm thông số tốt nhất
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx

from src.core.checker import _parse_ohlcv, _price_change_pct, calculate_ratio, determine_alert_level
from src.core.scorer import compute_score
from src.core.session import get_session, elapsed_minutes
from src.data.dnse_client import fetch_all_ohlcv, fetch_hose_symbols

_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ── Trading calendar ──────────────────────────────────────────────────────────
# April 30 = Liberation Day, May 1 = Labor Day (holiday)
TRADING_CAL: list[date] = [
    # VMA9 history buffer (March)
    date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18), date(2026, 3, 19), date(2026, 3, 20),
    date(2026, 3, 23), date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26), date(2026, 3, 27),
    date(2026, 3, 30), date(2026, 3, 31),
    # April (signal start từ 7/4)
    date(2026, 4,  1), date(2026, 4,  2), date(2026, 4,  3),
    date(2026, 4,  7), date(2026, 4,  8), date(2026, 4,  9), date(2026, 4, 10),
    date(2026, 4, 13), date(2026, 4, 14), date(2026, 4, 15), date(2026, 4, 16), date(2026, 4, 17),
    date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23), date(2026, 4, 24), date(2026, 4, 25),
    date(2026, 4, 28), date(2026, 4, 29),
    # May (bỏ 30/4, 1/5 và 4/5 nếu là bridge)
    date(2026, 5,  5), date(2026, 5,  6), date(2026, 5,  7), date(2026, 5,  8), date(2026, 5,  9),
    date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14), date(2026, 5, 15), date(2026, 5, 16),
    date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 23),
    date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28), date(2026, 5, 29),
]

# Signal days: Apr 7 → May 27 (cần T+2 ≤ May 29)
SIGNAL_DAYS = [d for d in TRADING_CAL if date(2026, 4, 7) <= d <= date(2026, 5, 27)]

def _t_plus(d: date, n: int) -> date | None:
    try:
        idx = TRADING_CAL.index(d)
        return TRADING_CAL[idx + n]
    except (ValueError, IndexError):
        return None

CHECK_TIMES = [
    time(9, 15), time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 0), time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45),
]

MIN_SCORE    =  4
ATO_THRESH   =  3.0
WARN_THRESH  =  2.0
CRIT_THRESH  =  3.0
VNI_TREND_DAYS = 5

# Exit parameter sweep
SL_LIST = [-3.0, -5.0, -7.0]
TP_LIST = [ 5.0,  7.0]


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    signal_day:  date
    symbol:      str
    signal_time: str
    ratio:       float
    score:       int
    buy_price:   float
    vni_green:   bool
    vni_uptrend: bool


@dataclass
class Trade:
    sig:         Signal
    sl:          float
    tp:          float
    exit_price:  float
    exit_day:    date
    exit_reason: str    # STOP_LOSS | TAKE_PROFIT | T+2
    pnl_pct:     float


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _slice_intra(raw: dict | None, cut_ts: int) -> dict | None:
    if not raw or not raw.get("t"):
        return None
    n = sum(1 for ts in raw["t"] if ts <= cut_ts)
    return ({k: (v[:n] if isinstance(v, list) else v) for k, v in raw.items()}) if n else None


def _day_ohlcv(all_daily: dict, sym: str, d: date) -> dict | None:
    data = all_daily.get(sym)
    if not data or not data.get("t"):
        return None
    d0, d1 = _ts(d, time(0, 0)), _ts(d, time(23, 59, 59))
    idx = [i for i, ts in enumerate(data["t"]) if d0 <= ts <= d1]
    if not idx:
        return None
    return {
        "high":  max(data["h"][i] for i in idx) if "h" in data else None,
        "low":   min(data["l"][i] for i in idx) if "l" in data else None,
        "close": data["c"][idx[-1]],
    }


def _vma9_before(all_daily: dict, sym: str, sig_day: date) -> float | None:
    data = all_daily.get(sym)
    if not data or not data.get("t"):
        return None
    cut = _ts(sig_day, time(0, 0))
    vols = [v for ts, v in zip(data["t"], data["v"]) if ts < cut and v and v > 0]
    if len(vols) < 3:
        return None
    return sum(vols[-9:]) / len(vols[-9:])


def _high20_before(all_daily: dict, sym: str, sig_day: date) -> float | None:
    data = all_daily.get(sym)
    if not data or not data.get("t") or "h" not in data:
        return None
    cut = _ts(sig_day, time(0, 0))
    highs = [h for ts, h in zip(data["t"], data["h"]) if ts < cut and h and h > 0]
    return max(highs[-20:]) if highs else None


def _vni_uptrend(vni_daily: dict, sig_day: date) -> bool:
    if not vni_daily or not vni_daily.get("t"):
        return False
    cut = _ts(sig_day, time(0, 0))
    closes = [(ts, c) for ts, c in zip(vni_daily["t"], vni_daily["c"]) if ts < cut and c]
    if len(closes) < VNI_TREND_DAYS:
        return False
    return closes[-1][1] > closes[-VNI_TREND_DAYS][1]


async def _fetch_vnindex_intra(d: date) -> float | None:
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "15",
            "from": _ts(d, time(9, 0)), "to": _ts(d, time(15, 0)),
        })
        if r.status_code != 200:
            return None
    _, vni_open, vni_close = _parse_ohlcv(r.json())
    return _price_change_pct(vni_open, vni_close)


async def _fetch_vnindex_daily() -> dict:
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "1D",
            "from": _ts(TRADING_CAL[0],  time(0, 0)),
            "to":   _ts(TRADING_CAL[-1], time(23, 59, 59)),
        })
        return r.json() if r.status_code == 200 else {}


# ── Signal detection ──────────────────────────────────────────────────────────

def _detect_day(
    sig_day: date, symbols: list[str],
    intra_raw: dict, all_daily: dict,
    vni_chg: float | None, vni_daily: dict,
) -> list[Signal]:
    green   = vni_chg is not None and vni_chg > 0
    uptrend = _vni_uptrend(vni_daily, sig_day)
    signals: list[Signal] = []
    alerted: set[str] = set()

    for check_t in CHECK_TIMES:
        session = get_session(check_t)
        if not session:
            continue
        elapsed = elapsed_minutes(check_t)
        cut_ts  = _ts(sig_day, check_t)

        for sym in symbols:
            if sym in alerted:
                continue
            vma9 = _vma9_before(all_daily, sym, sig_day)
            if not vma9:
                continue
            sliced = _slice_intra(intra_raw.get(sym), cut_ts)
            if not sliced:
                continue
            vol, open_p, close_p = _parse_ohlcv(sliced)
            ratio = calculate_ratio(vol, vma9, elapsed)
            if ratio is None:
                continue
            level = determine_alert_level(ratio, session.name, ATO_THRESH, WARN_THRESH, CRIT_THRESH)
            if level is None:
                continue
            price_chg = _price_change_pct(open_p, close_p)
            high20    = _high20_before(all_daily, sym, sig_day)
            is_bo     = bool(high20 and close_p and close_p > high20)
            rs = (price_chg - vni_chg) if (price_chg is not None and vni_chg is not None) else None
            score = compute_score(ratio, price_chg, is_bo, rs)
            if score.total < MIN_SCORE:
                continue
            alerted.add(sym)
            signals.append(Signal(
                signal_day=sig_day, symbol=sym,
                signal_time=check_t.strftime("%H:%M"),
                ratio=ratio, score=score.total,
                buy_price=close_p or 0,
                vni_green=green, vni_uptrend=uptrend,
            ))
    return signals


# ── Exit simulation ───────────────────────────────────────────────────────────

def _simulate(sig: Signal, all_daily: dict, sl_pct: float, tp_pct: float) -> Trade | None:
    buy = sig.buy_price
    if not buy or buy <= 0:
        return None
    sl_price = buy * (1 + sl_pct / 100)
    tp_price = buy * (1 + tp_pct / 100)
    t1 = _t_plus(sig.signal_day, 1)
    t2 = _t_plus(sig.signal_day, 2)
    if not t1 or not t2:
        return None

    for check_day, is_final in [(t1, False), (t2, True)]:
        ohlcv = _day_ohlcv(all_daily, sig.symbol, check_day)
        if not ohlcv:
            if is_final:
                return None
            continue
        low   = ohlcv["low"]   or buy
        high  = ohlcv["high"]  or buy
        close = ohlcv["close"] or buy
        if low <= sl_price:
            return Trade(sig, sl_pct, tp_pct, sl_price, check_day, "SL", sl_pct)
        if high >= tp_price:
            return Trade(sig, sl_pct, tp_pct, tp_price, check_day, "TP", tp_pct)
        if is_final:
            pnl = (close - buy) / buy * 100
            return Trade(sig, sl_pct, tp_pct, close, check_day, "T2", pnl)
    return None


# ── Stats ─────────────────────────────────────────────────────────────────────

def _report(trades: list[Trade], label: str):
    if not trades:
        print(f"  {label}: 0 GD")
        return
    n    = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    avg  = sum(t.pnl_pct for t in trades) / n
    tp_n = sum(1 for t in trades if t.exit_reason == "TP")
    sl_n = sum(1 for t in trades if t.exit_reason == "SL")
    print(f"  {label}: {wins:>3}/{n:<3} ({wins/n*100:>3.0f}%)  "
          f"avg {avg:>+6.2f}%  TP:{tp_n:<3} SL:{sl_n}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  BACKTEST — Apr 7 → May 27, 2026  (~7 tuần)")
    print("  Exit sweep: SL [-3,-5,-7%] × TP [+5,+7%]")
    print("=" * 70)

    # 1. Symbols
    print("\n[1/5] Danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # 2. Daily data (VMA9 + exit prices)
    print("\n[2/5] Daily OHLCV stocks (2026-03-16 → 2026-05-29)...")
    all_daily = await fetch_all_ohlcv(
        symbols,
        _ts(TRADING_CAL[0],  time(0, 0)),
        _ts(TRADING_CAL[-1], time(23, 59, 59)),
        resolution="1D",
    )
    print(f"      {sum(1 for v in all_daily.values() if v and v.get('t'))} mã có data")

    # 3. VNINDEX daily trend
    print("\n[3/5] VNINDEX daily (trend 5 ngày)...")
    vni_daily = await _fetch_vnindex_daily()
    print(f"      {len(vni_daily.get('t', []))} candle")

    # 4. Intraday + VNINDEX per signal day (song song mỗi ngày)
    print(f"\n[4/5] Intraday 15-min + VNINDEX ({len(SIGNAL_DAYS)} ngày)...")
    vni_chg_by_day: dict[date, float | None] = {}
    intra_by_day:   dict[date, dict] = {}

    for d in SIGNAL_DAYS:
        vni_chg, intra = await asyncio.gather(
            _fetch_vnindex_intra(d),
            fetch_all_ohlcv(symbols, _ts(d, time(9, 0)), _ts(d, time(15, 0)), "15"),
        )
        vni_chg_by_day[d] = vni_chg
        intra_by_day[d]   = intra
        g = "🟢" if (vni_chg and vni_chg > 0) else "🔴"
        u = "📈" if _vni_uptrend(vni_daily, d) else "📉"
        v = f"{vni_chg:+.2f}%" if vni_chg is not None else "  N/A"
        print(f"      {d}  {g}{v:<8}  {u}")

    # 5. Detect all signals
    print("\n[5/5] Detect signals...")
    all_signals: list[Signal] = []
    pending_total = 0

    for d in SIGNAL_DAYS:
        sigs = _detect_day(d, symbols, intra_by_day[d], all_daily,
                           vni_chg_by_day[d], vni_daily)
        # Check how many have T+2 data
        n_data    = sum(1 for s in sigs if _day_ohlcv(all_daily, s.symbol, _t_plus(d, 2) or d))
        n_pending = len(sigs) - n_data
        pending_total += n_pending
        all_signals.extend(sigs)
        print(f"      {d}: {len(sigs):>2} BUY SIGNAL  {'⏳' + str(n_pending) if n_pending else '✅'}")

    print(f"\n  Tổng tín hiệu: {len(all_signals)}")
    if pending_total:
        print(f"  ⏳ {pending_total} chưa có T+2 data (bỏ qua trong backtest)")

    # ── Exit parameter sweep ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SWEEP EXIT PARAMETERS")
    print("=" * 70)

    # Tính tất cả trades cho mọi (SL, TP) combo
    combo_results: dict[tuple, list[Trade]] = {}
    for sl in SL_LIST:
        for tp in TP_LIST:
            trades = [t for s in all_signals
                      if (t := _simulate(s, all_daily, sl, tp)) is not None]
            combo_results[(sl, tp)] = trades

    print(f"\n  {'SL':>5}  {'TP':>5}  {'GD':>5}  {'Win%':>5}  {'Avg P&L':>8}  "
          f"{'TP#':>4}  {'SL#':>4}  T2#")
    print("  " + "─" * 55)
    best_combo, best_avg = (0, 0), -999.0
    for (sl, tp), trades in sorted(combo_results.items()):
        if not trades:
            continue
        n    = len(trades)
        wins = sum(1 for t in trades if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in trades) / n
        tp_n = sum(1 for t in trades if t.exit_reason == "TP")
        sl_n = sum(1 for t in trades if t.exit_reason == "SL")
        t2_n = n - tp_n - sl_n
        star = " ← best" if avg > best_avg else ""
        if avg > best_avg:
            best_avg, best_combo = avg, (sl, tp)
        print(f"  {sl:>+5.0f}%  {tp:>+5.0f}%  {n:>5}  {wins/n*100:>4.0f}%  "
              f"{avg:>+7.2f}%  {tp_n:>4}  {sl_n:>4}  {t2_n}{star}")

    # ── Tìm bộ thông số tốt nhất → phân tích filters ─────────────────────────
    sl_best, tp_best = best_combo
    best_trades = combo_results[best_combo]

    print(f"\n{'─'*70}")
    print(f"  BỘ THÔNG SỐ TỐT NHẤT: SL {sl_best:+.0f}%  TP +{tp_best:.0f}%")
    print(f"{'─'*70}")

    A = best_trades
    B = [t for t in A if t.sig.vni_green]
    C = [t for t in A if t.sig.vni_uptrend]
    D = [t for t in A if t.sig.vni_green and t.sig.vni_uptrend]

    _report(A, "A. Không filter      ")
    _report(B, "B. Market filter VNI ")
    _report(C, "C. Trend filter 5d   ")
    _report(D, "D. Market + Trend    ")

    # ── Breakdown theo tháng ─────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  BREAKDOWN THEO THÁNG:")
    for mo, mo_label in [(4, "Tháng 4"), (5, "Tháng 5")]:
        mo_trades = [t for t in A if t.sig.signal_day.month == mo]
        if not mo_trades:
            continue
        n    = len(mo_trades)
        wins = sum(1 for t in mo_trades if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in mo_trades) / n
        print(f"  {mo_label}: {wins}/{n} thắng ({wins/n*100:.0f}%)  avg {avg:+.2f}%")

    # ── Breakdown ngày ────────────────────────────────────────────────────────
    print(f"\n  {'Ngày':<10}  {'VNI':>7}  {'Trend':>5}  {'Sig':>4}  {'Win/GD':>7}  Avg P&L")
    print("  " + "─" * 55)
    for d in SIGNAL_DAYS:
        day_trades = [t for t in A if t.sig.signal_day == d]
        if not day_trades:
            continue
        wins  = sum(1 for t in day_trades if t.pnl_pct > 0)
        avg   = sum(t.pnl_pct for t in day_trades) / len(day_trades)
        vni   = vni_chg_by_day[d]
        g     = "🟢" if (vni and vni > 0) else "🔴"
        u     = "📈" if _vni_uptrend(vni_daily, d) else "📉"
        v_s   = f"{vni:+.2f}%" if vni is not None else "   N/A"
        n_sig = sum(1 for s in all_signals if s.signal_day == d)
        print(f"  {str(d):<10}  {g}{v_s:>7}  {u}    {n_sig:>4}  "
              f"{wins:>2}/{len(day_trades):<2}      {avg:>+6.2f}%")

    if pending_total:
        print(f"\n  ⏳ {pending_total} tín hiệu chưa có T+2 data (không tính vào kết quả)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
