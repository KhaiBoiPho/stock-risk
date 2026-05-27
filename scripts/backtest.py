"""
Backtest: Volume spike + scoring + technical filters + exit strategy.

Signal days: Apr 7 – May 27, 2026 (~7 tuần)
Exit: SL -7% | TP +7% (bộ thông số tốt nhất từ sweep trước)

Filters so sánh:
  A. Không filter (baseline)
  B. Market filter    : VNINDEX xanh cùng ngày
  C. Trend filter     : VNINDEX uptrend 5 ngày
  D. MA filter        : giá > MA20 > MA50 (cổ phiếu đang uptrend)
  E. RSI filter       : RSI14 trong vùng 40–70 (không overbought)
  F. MA + RSI         : kết hợp D + E
  G. Best combo       : C + F (market trend + MA + RSI)
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

# ── Config ────────────────────────────────────────────────────────────────────
STOP_LOSS    = -7.0
TAKE_PROFIT  =  7.0
MIN_SCORE    =  4
ATO_THRESH   =  3.0
WARN_THRESH  =  2.0
CRIT_THRESH  =  3.0
VNI_TREND_DAYS = 5
RSI_LOW  = 40.0
RSI_HIGH = 70.0

# Daily data cần từ Jan 5 để đủ lịch sử MA50 (50 ngày trước Apr 7)
DAILY_START = date(2026, 1, 5)
DAILY_END   = date(2026, 5, 29)

# ── Trading calendar ──────────────────────────────────────────────────────────
TRADING_CAL: list[date] = [
    # VMA9 / MA buffer (March)
    date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18), date(2026, 3, 19), date(2026, 3, 20),
    date(2026, 3, 23), date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26), date(2026, 3, 27),
    date(2026, 3, 30), date(2026, 3, 31),
    date(2026, 4,  1), date(2026, 4,  2), date(2026, 4,  3),
    date(2026, 4,  7), date(2026, 4,  8), date(2026, 4,  9), date(2026, 4, 10),
    date(2026, 4, 13), date(2026, 4, 14), date(2026, 4, 15), date(2026, 4, 16), date(2026, 4, 17),
    date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23), date(2026, 4, 24), date(2026, 4, 25),
    date(2026, 4, 28), date(2026, 4, 29),
    date(2026, 5,  5), date(2026, 5,  6), date(2026, 5,  7), date(2026, 5,  8), date(2026, 5,  9),
    date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14), date(2026, 5, 15), date(2026, 5, 16),
    date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 23),
    date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28), date(2026, 5, 29),
]

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
    ma_ok:       bool    # price > MA20 > MA50
    rsi_ok:      bool    # RSI14 trong 40–70
    ma20:        float | None
    ma50:        float | None
    rsi14:       float | None


@dataclass
class Trade:
    sig:         Signal
    exit_price:  float
    exit_day:    date
    exit_reason: str
    pnl_pct:     float


# ── Technical indicator helpers ───────────────────────────────────────────────

def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _closes_before(all_daily: dict, sym: str, sig_day: date, n: int) -> list[float]:
    """Lấy n close prices gần nhất trước sig_day."""
    data = all_daily.get(sym)
    if not data or not data.get("t"):
        return []
    cut = _ts(sig_day, time(0, 0))
    closes = [c for ts, c in zip(data["t"], data["c"]) if ts < cut and c and c > 0]
    return closes[-n:] if len(closes) >= n else closes


def _ma(all_daily: dict, sym: str, sig_day: date, period: int) -> float | None:
    closes = _closes_before(all_daily, sym, sig_day, period)
    return sum(closes) / len(closes) if len(closes) >= period else None


def _rsi14(all_daily: dict, sym: str, sig_day: date) -> float | None:
    """RSI14: dùng 15 close prices gần nhất (14 delta)."""
    closes = _closes_before(all_daily, sym, sig_day, 15)
    if len(closes) < 15:
        return None
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, 15)]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, 15)]
    avg_gain = sum(gains)  / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


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
    return sum(vols[-9:]) / len(vols[-9:]) if len(vols) >= 3 else None


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
    closes = [c for ts, c in zip(vni_daily["t"], vni_daily["c"]) if ts < cut and c]
    return len(closes) >= VNI_TREND_DAYS and closes[-1] > closes[-VNI_TREND_DAYS]


async def _fetch_vnindex_intra(d: date) -> float | None:
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "15",
            "from": _ts(d, time(9, 0)), "to": _ts(d, time(15, 0)),
        })
        if r.status_code != 200:
            return None
    _, o, cl = _parse_ohlcv(r.json())
    return _price_change_pct(o, cl)


async def _fetch_vnindex_daily() -> dict:
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "1D",
            "from": _ts(DAILY_START, time(0, 0)),
            "to":   _ts(DAILY_END,   time(23, 59, 59)),
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

            # Technical indicators
            ma20  = _ma(all_daily, sym, sig_day, 20)
            ma50  = _ma(all_daily, sym, sig_day, 50)
            rsi   = _rsi14(all_daily, sym, sig_day)
            price = close_p or 0.0

            ma_ok  = bool(ma20 and ma50 and price > ma20 > ma50)
            rsi_ok = rsi is not None and RSI_LOW <= rsi <= RSI_HIGH

            signals.append(Signal(
                signal_day=sig_day, symbol=sym,
                signal_time=check_t.strftime("%H:%M"),
                ratio=ratio, score=score.total,
                buy_price=price,
                vni_green=green, vni_uptrend=uptrend,
                ma_ok=ma_ok, rsi_ok=rsi_ok,
                ma20=ma20, ma50=ma50, rsi14=rsi,
            ))
    return signals


# ── Exit simulation ───────────────────────────────────────────────────────────

def _simulate(sig: Signal, all_daily: dict) -> Trade | None:
    buy = sig.buy_price
    if not buy or buy <= 0:
        return None
    sl_price = buy * (1 + STOP_LOSS / 100)
    tp_price = buy * (1 + TAKE_PROFIT / 100)
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
        low, high, close = ohlcv["low"] or buy, ohlcv["high"] or buy, ohlcv["close"] or buy
        if low <= sl_price:
            return Trade(sig, sl_price, check_day, "SL", STOP_LOSS)
        if high >= tp_price:
            return Trade(sig, tp_price, check_day, "TP", TAKE_PROFIT)
        if is_final:
            return Trade(sig, close, check_day, "T2", (close - buy) / buy * 100)
    return None


# ── Report helpers ────────────────────────────────────────────────────────────

def _stats(trades: list[Trade]) -> tuple[int, int, float]:
    n    = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    avg  = sum(t.pnl_pct for t in trades) / n if n else 0.0
    return n, wins, avg


def _row(label: str, trades: list[Trade], base_n: int):
    if not trades:
        print(f"  {label}: — (0 GD)")
        return
    n, wins, avg = _stats(trades)
    tp_n = sum(1 for t in trades if t.exit_reason == "TP")
    sl_n = sum(1 for t in trades if t.exit_reason == "SL")
    pct_of_base = n / base_n * 100 if base_n else 0
    win_rate = wins / n * 100
    bar = "█" * int(win_rate / 5)
    print(f"  {label}: {wins:>3}/{n:<3} ({win_rate:>3.0f}%)  avg {avg:>+6.2f}%  "
          f"TP:{tp_n:<3} SL:{sl_n:<3}  [{bar:<20}]  ({pct_of_base:.0f}% signals)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 72)
    print("  BACKTEST — MA20/MA50 + RSI14 Filter")
    print(f"  Apr 7 → May 27, 2026  |  Exit: SL {STOP_LOSS:+.0f}%  TP +{TAKE_PROFIT:.0f}%")
    print("=" * 72)

    # 1. Symbols
    print("\n[1/5] Danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # 2. Daily data (từ Jan 5 để có đủ MA50)
    print(f"\n[2/5] Daily OHLCV ({DAILY_START} → {DAILY_END}, cần cho MA50)...")
    all_daily = await fetch_all_ohlcv(
        symbols,
        _ts(DAILY_START, time(0, 0)),
        _ts(DAILY_END,   time(23, 59, 59)),
        resolution="1D",
    )
    # Spot-check MA50 availability
    sample_sym = next((s for s in symbols if all_daily.get(s, {}).get("t")), None)
    if sample_sym:
        n_closes = len(_closes_before(all_daily, sample_sym, date(2026, 4, 7), 50))
        print(f"      {sum(1 for v in all_daily.values() if v and v.get('t'))} mã có data  "
              f"(ví dụ {sample_sym}: {n_closes} closes trước 7/4 → "
              f"{'✅ đủ MA50' if n_closes >= 50 else '⚠️ chưa đủ MA50'})")

    # 3. VNINDEX daily
    print("\n[3/5] VNINDEX daily trend...")
    vni_daily = await _fetch_vnindex_daily()
    print(f"      {len(vni_daily.get('t', []))} candle")

    # 4. Intraday + VNINDEX per signal day
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

    # 5. Detect signals
    print("\n[5/5] Detect signals...")
    all_signals: list[Signal] = []
    pending_total = 0
    for d in SIGNAL_DAYS:
        sigs = _detect_day(d, symbols, intra_by_day[d], all_daily,
                           vni_chg_by_day[d], vni_daily)
        n_ok = sum(1 for s in sigs if _day_ohlcv(all_daily, s.symbol, _t_plus(d, 2) or d))
        pending_total += len(sigs) - n_ok
        all_signals.extend(sigs)

    print(f"      Tổng: {len(all_signals)} BUY SIGNAL  ({pending_total} chưa có T+2 data)")

    # Tính tất cả trades
    all_trades = [t for s in all_signals if (t := _simulate(s, all_daily)) is not None]
    n_base = len(all_trades)

    # ── Kết quả tổng hợp ─────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  SO SÁNH CÁC BỘ FILTER (exit: SL -7%, TP +7%)")
    print("=" * 72)

    A = all_trades
    B = [t for t in A if t.sig.vni_green]
    C = [t for t in A if t.sig.vni_uptrend]
    D = [t for t in A if t.sig.ma_ok]
    E = [t for t in A if t.sig.rsi_ok]
    F = [t for t in A if t.sig.ma_ok and t.sig.rsi_ok]
    G = [t for t in A if t.sig.vni_uptrend and t.sig.ma_ok and t.sig.rsi_ok]

    print(f"\n  {'Filter':<28}  {'Win/GD':<9}  {'WinRate':>7}  {'Avg P&L':>8}  "
          f"{'TP':>3}  {'SL':>3}  Visual (5%=1█)")
    print("  " + "─" * 68)
    _row("A. Baseline (không filter)   ", A, n_base)
    _row("B. VNI xanh cùng ngày       ", B, n_base)
    _row("C. VNI uptrend 5 ngày       ", C, n_base)
    _row("D. MA20 > MA50 + giá > MA20 ", D, n_base)
    _row("E. RSI14 trong 40–70        ", E, n_base)
    _row("F. D + E  (MA + RSI)        ", F, n_base)
    _row("G. C + D + E  (best combo)  ", G, n_base)

    # ── Chi tiết filter MA + RSI ──────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("  PHÂN TÍCH: MA filter loại gì?")
    ma_fail = [t for t in A if not t.sig.ma_ok]
    ma_pass = [t for t in A if t.sig.ma_ok]
    if ma_fail and ma_pass:
        _, w_fail, avg_fail = _stats(ma_fail)
        _, w_pass, avg_pass = _stats(ma_pass)
        print(f"  Cổ phiếu KHÔNG qua MA filter: {len(ma_fail):>3} GD  "
              f"win {w_fail/len(ma_fail)*100:.0f}%  avg {avg_fail:+.2f}%")
        print(f"  Cổ phiếu QUA MA filter:        {len(ma_pass):>3} GD  "
              f"win {w_pass/len(ma_pass)*100:.0f}%  avg {avg_pass:+.2f}%")

    print("\n  PHÂN TÍCH: RSI filter loại gì?")
    rsi_fail = [t for t in A if not t.sig.rsi_ok]
    rsi_pass = [t for t in A if t.sig.rsi_ok]
    if rsi_fail and rsi_pass:
        _, w_fail, avg_fail = _stats(rsi_fail)
        _, w_pass, avg_pass = _stats(rsi_pass)
        n_overbought  = sum(1 for t in rsi_fail if t.sig.rsi14 and t.sig.rsi14 > RSI_HIGH)
        n_oversold    = sum(1 for t in rsi_fail if t.sig.rsi14 and t.sig.rsi14 < RSI_LOW)
        n_no_rsi      = sum(1 for t in rsi_fail if t.sig.rsi14 is None)
        print(f"  Bị loại (overbought RSI>{RSI_HIGH:.0f}): {n_overbought:>3}  "
              f"bị loại (oversold RSI<{RSI_LOW:.0f}): {n_oversold}  "
              f"không có RSI: {n_no_rsi}")
        print(f"  KHÔNG qua RSI filter: {len(rsi_fail):>3} GD  "
              f"win {w_fail/len(rsi_fail)*100:.0f}%  avg {avg_fail:+.2f}%")
        print(f"  QUA RSI filter:        {len(rsi_pass):>3} GD  "
              f"win {w_pass/len(rsi_pass)*100:.0f}%  avg {avg_pass:+.2f}%")

    # ── Breakdown theo tháng cho G (best combo) ───────────────────────────────
    print(f"\n{'─'*72}")
    print("  BREAKDOWN THEO THÁNG — Filter G (VNI trend + MA + RSI):")
    for mo, label in [(4, "Tháng 4"), (5, "Tháng 5")]:
        mo_trades = [t for t in G if t.sig.signal_day.month == mo]
        if not mo_trades:
            continue
        n, wins, avg = _stats(mo_trades)
        print(f"  {label}: {wins}/{n} thắng ({wins/n*100:.0f}%)  avg {avg:+.2f}%")

    # ── Breakdown theo ngày cho G ─────────────────────────────────────────────
    print(f"\n  {'Ngày':<10}  {'VNI':>7}  {'Trend':>5}  "
          f"{'All':>4}  {'qua MA':>6}  {'qua RSI':>7}  {'G: Win/GD':>10}  Avg G")
    print("  " + "─" * 67)
    for d in SIGNAL_DAYS:
        day_all = [t for t in A if t.sig.signal_day == d]
        day_g   = [t for t in G if t.sig.signal_day == d]
        if not day_all:
            continue
        vni = vni_chg_by_day[d]
        g_flag = "🟢" if (vni and vni > 0) else "🔴"
        u_flag = "📈" if _vni_uptrend(vni_daily, d) else "📉"
        v_s    = f"{vni:+.2f}%" if vni is not None else "   N/A"
        n_ma   = sum(1 for t in day_all if t.sig.ma_ok)
        n_rsi  = sum(1 for t in day_all if t.sig.rsi_ok)
        if day_g:
            wins_g, avg_g = sum(1 for t in day_g if t.pnl_pct > 0), sum(t.pnl_pct for t in day_g)/len(day_g)
            g_str  = f"{wins_g}/{len(day_g)}"
            avg_str = f"{avg_g:>+6.2f}%"
        else:
            g_str, avg_str = "—", "    —"
        n_sig = sum(1 for s in all_signals if s.signal_day == d)
        print(f"  {str(d):<10}  {g_flag}{v_s:>7}  {u_flag}    "
              f"{n_sig:>4}  {n_ma:>6}  {n_rsi:>7}  {g_str:>10}  {avg_str}")

    if pending_total:
        print(f"\n  ⏳ {pending_total} tín hiệu chưa có T+2 data (không tính)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
