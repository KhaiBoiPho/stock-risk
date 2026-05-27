"""
Backtest: Volume spike + scoring + filters + exit strategy.

Signal days: May 12 – May 27, 2026 (3 tuần)
Buy price  : close price tại thời điểm phát hiện tín hiệu
Exit rules :
  - Stop loss  -3%  → check T+1 day low
  - Take profit +5% → check T+1 day high
  - Else sell at T+2 close

Filters so sánh:
  A. Không filter (baseline)
  B. Market filter : VNINDEX xanh cùng ngày
  C. Trend filter  : VNINDEX 5 ngày đang tăng (uptrend)
  D. B + C kết hợp
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

# ── Trading calendar (thêm ngày nếu cần) ─────────────────────────────────────
# May 1 = Labor Day holiday; April 30 = Liberation Day holiday
TRADING_CAL: list[date] = [
    # History buffer cho VMA9 (April)
    date(2026, 4, 14), date(2026, 4, 15), date(2026, 4, 16), date(2026, 4, 17),
    date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23), date(2026, 4, 24),
    date(2026, 4, 28), date(2026, 4, 29),
    # May (bỏ 30/4 và 1/5)
    date(2026, 5,  5), date(2026, 5,  6), date(2026, 5,  7), date(2026, 5,  8), date(2026, 5,  9),
    date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14), date(2026, 5, 15), date(2026, 5, 16),
    date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 23),
    date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28), date(2026, 5, 29),
]

# Ngày cần T+2 data (signal_day + 2 trading days) → cut ở May 27 để T+2 ≤ May 29
SIGNAL_DAYS = [d for d in TRADING_CAL if date(2026, 5, 12) <= d <= date(2026, 5, 27)]

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

STOP_LOSS    = -3.0
TAKE_PROFIT  =  5.0
MIN_SCORE    =  4
ATO_THRESH   =  3.0
WARN_THRESH  =  2.0
CRIT_THRESH  =  3.0
VNI_TREND_DAYS = 5   # so sánh VNI hôm nay với 5 ngày trước


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    signal_day:  date
    symbol:      str
    signal_time: str
    ratio:       float
    score:       int
    buy_price:   float
    exit_price:  float
    exit_day:    date
    exit_reason: str       # STOP_LOSS | TAKE_PROFIT | T+2
    pnl_pct:     float
    vni_green:   bool      # VNINDEX xanh cùng ngày?
    vni_uptrend: bool      # VNINDEX 5-ngày đang tăng?


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
        "open":  data["o"][idx[0]],
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


def _vni_green(vni_chg: float | None) -> bool:
    return vni_chg is not None and vni_chg > 0


def _vni_uptrend(vni_daily: dict | None, sig_day: date) -> bool:
    """True nếu VNINDEX close hôm nay > close cách đây VNI_TREND_DAYS ngày giao dịch."""
    if not vni_daily or not vni_daily.get("t"):
        return False
    cut = _ts(sig_day, time(0, 0))
    closes = [(ts, c) for ts, c in zip(vni_daily["t"], vni_daily["c"]) if ts < cut and c]
    if len(closes) < VNI_TREND_DAYS:
        return False
    close_now = closes[-1][1]
    close_nd  = closes[-VNI_TREND_DAYS][1]
    return close_now > close_nd


async def _fetch_vnindex_intra(d: date) -> float | None:
    """VNINDEX change% trong ngày d."""
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "15",
            "from": _ts(d, time(9, 0)), "to": _ts(d, time(15, 0)),
        })
        if r.status_code != 200:
            return None
        data = r.json()
    _, vni_open, vni_close = _parse_ohlcv(data)
    return _price_change_pct(vni_open, vni_close)


async def _fetch_vnindex_daily() -> dict:
    """VNINDEX daily data (dùng để tính trend)."""
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    from_ts = _ts(TRADING_CAL[0], time(0, 0))
    to_ts   = _ts(TRADING_CAL[-1], time(23, 59, 59))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "1D",
            "from": from_ts, "to": to_ts,
        })
        return r.json() if r.status_code == 200 else {}


# ── Signal detection ──────────────────────────────────────────────────────────

def _detect_day(
    sig_day: date, symbols: list[str],
    intra_raw: dict, all_daily: dict,
    vni_chg: float | None, vni_daily: dict,
) -> list[dict]:
    green   = _vni_green(vni_chg)
    uptrend = _vni_uptrend(vni_daily, sig_day)
    signals = []
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
            signals.append({
                "symbol": sym, "time": check_t.strftime("%H:%M"),
                "ratio": ratio, "score": score.total,
                "buy_price": close_p,
                "vni_green": green,
                "vni_uptrend": uptrend,
            })
    return signals


# ── Exit simulation ───────────────────────────────────────────────────────────

def _simulate(sig: dict, sig_day: date, all_daily: dict) -> Trade | None:
    sym = sig["symbol"]
    buy = sig["buy_price"]
    if not buy or buy <= 0:
        return None
    sl = buy * (1 + STOP_LOSS / 100)
    tp = buy * (1 + TAKE_PROFIT / 100)
    t1 = _t_plus(sig_day, 1)
    t2 = _t_plus(sig_day, 2)
    if not t1 or not t2:
        return None

    for check_day, is_final in [(t1, False), (t2, True)]:
        ohlcv = _day_ohlcv(all_daily, sym, check_day)
        if not ohlcv:
            if is_final:
                return None   # T+2 data chưa có
            continue
        low   = ohlcv["low"]  or buy
        high  = ohlcv["high"] or buy
        close = ohlcv["close"]
        if low <= sl:
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         buy, sl, check_day, "STOP_LOSS", STOP_LOSS,
                         sig["vni_green"], sig["vni_uptrend"])
        if high >= tp:
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         buy, tp, check_day, "TAKE_PROFIT", TAKE_PROFIT,
                         sig["vni_green"], sig["vni_uptrend"])
        if is_final:
            pnl = (close - buy) / buy * 100
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         buy, close, check_day, "T+2", pnl,
                         sig["vni_green"], sig["vni_uptrend"])
    return None


# ── Output ────────────────────────────────────────────────────────────────────

def _icon(pnl: float) -> str:
    return "✅" if pnl > 0 else ("⚠️ " if abs(pnl) < 0.01 else "❌")


def _stats(trades: list[Trade]) -> str:
    if not trades:
        return "0 GD"
    n    = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    avg  = sum(t.pnl_pct for t in trades) / n
    tp_n = sum(1 for t in trades if t.exit_reason == "TAKE_PROFIT")
    sl_n = sum(1 for t in trades if t.exit_reason == "STOP_LOSS")
    return (f"{wins}/{n} thắng ({wins/n*100:.0f}%)  avg {avg:+.2f}%  "
            f"TP:{tp_n} SL:{sl_n} T2:{n-tp_n-sl_n}")


def _print_group(trades: list[Trade], title: str):
    print(f"\n  ── {title} ──")
    if not trades:
        print("    (không có GD)")
        return
    print(f"  {'Ngày mua':<10}  {'Mã':<5}  {'Giờ':<5}  {'Sc':>4}  "
          f"{'Mua':>8}  {'Bán':>8}  {'P&L':>7}  Lý do")
    print("  " + "─" * 64)
    for t in sorted(trades, key=lambda x: (x.signal_day, x.symbol)):
        print(f"  {str(t.signal_day):<10}  {t.symbol:<5}  {t.signal_time:<5}  "
              f"{t.score}/6  {t.buy_price:>8,.1f}  {t.exit_price:>8,.1f}  "
              f"{t.pnl_pct:>+6.2f}%  {_icon(t.pnl_pct)} {t.exit_reason}")
    print(f"\n  → {_stats(trades)}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  BACKTEST — 3 tuần thực tế (May 12 – 27, 2026)")
    print(f"  Exit: SL {STOP_LOSS:+.0f}% | TP +{TAKE_PROFIT:.0f}% | T+2 close")
    print("=" * 70)

    # 1. Symbols
    print("\n[1/5] Lấy danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # 2. Daily stock data (VMA9 + exit prices)
    print("\n[2/5] Fetch daily OHLCV stocks (2026-04-14 → 2026-05-29)...")
    all_daily = await fetch_all_ohlcv(
        symbols,
        _ts(TRADING_CAL[0],  time(0, 0)),
        _ts(TRADING_CAL[-1], time(23, 59, 59)),
        resolution="1D",
    )
    print(f"      {sum(1 for v in all_daily.values() if v and v.get('t'))} mã có data")

    # 3. VNINDEX daily (cho trend filter)
    print("\n[3/5] Fetch VNINDEX daily (trend filter)...")
    vni_daily = await _fetch_vnindex_daily()
    if vni_daily.get("t"):
        print(f"      {len(vni_daily['t'])} candle VNINDEX daily")
    else:
        print("      ❌ Không lấy được VNINDEX daily")

    # 4. VNINDEX intraday + intraday stocks mỗi ngày signal
    print(f"\n[4/5] Fetch VNINDEX intraday + stocks 15-min ({len(SIGNAL_DAYS)} ngày)...")
    vni_chg_by_day: dict[date, float | None] = {}
    intra_by_day:   dict[date, dict] = {}

    for d in SIGNAL_DAYS:
        vni_chg, intra = await asyncio.gather(
            _fetch_vnindex_intra(d),
            fetch_all_ohlcv(symbols, _ts(d, time(9, 0)), _ts(d, time(15, 0)), "15"),
        )
        vni_chg_by_day[d] = vni_chg
        intra_by_day[d]   = intra
        green   = _vni_green(vni_chg)
        uptrend = _vni_uptrend(vni_daily, d)
        flag_g  = "🟢" if green   else "🔴"
        flag_t  = "📈" if uptrend else "📉"
        vni_s   = f"{vni_chg:+.2f}%" if vni_chg is not None else "N/A"
        print(f"      {d}  {flag_g} ngày:{vni_s:<8}  {flag_t} trend5d")

    # 5. Detect + simulate
    print("\n[5/5] Detect signals + simulate trades...")
    all_trades: list[Trade] = []
    day_stats:  list[tuple] = []
    pending_total = 0

    for d in SIGNAL_DAYS:
        sigs = _detect_day(
            d, symbols, intra_by_day[d], all_daily,
            vni_chg_by_day[d], vni_daily,
        )
        trades, pending = [], 0
        for s in sigs:
            t = _simulate(s, d, all_daily)
            if t:
                trades.append(t)
                all_trades.append(t)
            else:
                pending += 1
        pending_total += pending
        note = f"⏳{pending}" if pending else "✅"
        print(f"      {d}: {len(sigs):>2} BUY → {len(trades):>2} GD  {note}")
        day_stats.append((d, vni_chg_by_day[d], len(sigs), trades))

    # ── Kết quả ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  KẾT QUẢ")
    print("=" * 70)

    A = all_trades
    B = [t for t in A if t.vni_green]
    C = [t for t in A if t.vni_uptrend]
    D = [t for t in A if t.vni_green and t.vni_uptrend]

    _print_group(A, "A. Không filter (baseline)")
    _print_group(B, "B. Market filter: VNI xanh cùng ngày")
    _print_group(C, "C. Trend filter: VNI uptrend 5 ngày")
    _print_group(D, "D. Kết hợp B + C (VNI xanh + uptrend)")

    # So sánh
    print(f"\n{'─'*70}")
    print("  SO SÁNH 4 CHIẾN LƯỢC:")
    print(f"{'─'*70}")
    for label, group in [
        ("A. Không filter      ", A),
        ("B. Market filter     ", B),
        ("C. Trend filter 5d   ", C),
        ("D. Market + Trend    ", D),
    ]:
        if not group:
            print(f"  {label}: 0 GD — không đủ điều kiện")
            continue
        n    = len(group)
        wins = sum(1 for t in group if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in group) / n
        tp_n = sum(1 for t in group if t.exit_reason == "TAKE_PROFIT")
        sl_n = sum(1 for t in group if t.exit_reason == "STOP_LOSS")
        print(f"  {label}: {wins:>3}/{n:<3} thắng  ({wins/n*100:>3.0f}%)  "
              f"avg {avg:>+6.2f}%  TP:{tp_n:<2} SL:{sl_n}")

    # Breakdown theo tuần
    print(f"\n{'─'*70}")
    print("  BREAKDOWN THEO NGÀY:")
    print(f"  {'Ngày':<10}  {'VNI':>7}  {'Trend':>5}  {'Signal':>6}  Win/Total  Avg P&L")
    print(f"  {'─'*10}  {'─'*7}  {'─'*5}  {'─'*6}  {'─'*9}  {'─'*7}")
    for d, vni_chg, n_sigs, trades in day_stats:
        if not trades:
            continue
        wins  = sum(1 for t in trades if t.pnl_pct > 0)
        avg   = sum(t.pnl_pct for t in trades) / len(trades)
        green   = _vni_green(vni_chg)
        uptrend = _vni_uptrend(vni_daily, d)
        vni_s   = f"{vni_chg:+.2f}%" if vni_chg is not None else "   N/A"
        trend_s = "📈 up" if uptrend else "📉 dn"
        print(f"  {str(d):<10}  {vni_s:>7}  {trend_s}  {n_sigs:>6}  "
              f"{wins:>2}/{len(trades):<2}       {avg:>+6.2f}%")

    if pending_total:
        print(f"\n  ⏳ {pending_total} tín hiệu chưa có T+2 data")
    print(f"{'─'*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
