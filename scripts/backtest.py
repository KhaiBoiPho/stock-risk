"""
Backtest: Volume spike + scoring + market filter + exit strategy simulation.

Signal days: May 20-27, 2026 (6 trading days)
Buy price  : close price at moment signal fires (giá lúc phát hiện)
Exit rules :
  - Stop loss  -3%  → check T+1 day low
  - Take profit +5% → check T+1 day high
  - Else sell at T+2 close (T+2 = 2 ngày giao dịch sau ngày mua)

Algorithms vs feature/scoring:
  + Market filter: chỉ tính tín hiệu khi VNINDEX xanh
  + Momentum filter: ratio vẫn >= 2x ở phiên chiều (xác nhận duy trì)
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

# ── Trading calendar (cập nhật thủ công nếu có holiday) ─────────────────────
ALL_DAYS = [
    date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21),
    date(2026, 5, 22), date(2026, 5, 23),
    date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28), date(2026, 5, 29),
]
SIGNAL_DAYS = ALL_DAYS[1:7]   # May 20-27 (6 ngày có đủ T+2 data)
# T+1 và T+2: 2 ngày giao dịch tiếp theo
T1 = {d: ALL_DAYS[ALL_DAYS.index(d) + 1] for d in SIGNAL_DAYS}
T2 = {d: ALL_DAYS[ALL_DAYS.index(d) + 2] for d in SIGNAL_DAYS}

CHECK_TIMES = [
    time(9, 15), time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 0), time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45),
]

STOP_LOSS    = -3.0   # %
TAKE_PROFIT  =  5.0   # %
MIN_SCORE    =  4     # chỉ BUY SIGNAL (≥4đ)
ATO_THRESH   =  3.0
WARN_THRESH  =  2.0
CRIT_THRESH  =  3.0


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    signal_day:    date
    symbol:        str
    signal_time:   str
    ratio:         float
    score:         int
    breakdown:     str
    buy_price:     float
    exit_price:    float
    exit_day:      date
    exit_reason:   str    # STOP_LOSS | TAKE_PROFIT | T+2
    pnl_pct:       float
    vni_green:     bool   # VNINDEX xanh ngày mua?
    momentum_ok:   bool   # ratio vẫn ≥2x ở phiên chiều?


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _slice_intra(raw: dict | None, cut_ts: int) -> dict | None:
    if not raw or not raw.get("t"):
        return None
    n = sum(1 for ts in raw["t"] if ts <= cut_ts)
    return ({k: (v[:n] if isinstance(v, list) else v) for k, v in raw.items()}) if n else None


def _day_ohlcv(all_daily: dict, sym: str, d: date) -> dict | None:
    """Lấy OHLCV của 1 ngày cụ thể từ daily data."""
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
    recent = vols[-9:]
    return sum(recent) / len(recent)


def _high20_before(all_daily: dict, sym: str, sig_day: date) -> float | None:
    data = all_daily.get(sym)
    if not data or not data.get("t") or "h" not in data:
        return None
    cut = _ts(sig_day, time(0, 0))
    highs = [h for ts, h in zip(data["t"], data["h"]) if ts < cut and h and h > 0]
    return max(highs[-20:]) if highs else None


async def _fetch_vnindex(d: date) -> float | None:
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


# ── Signal detection ──────────────────────────────────────────────────────────

def _detect_day(
    sig_day: date, symbols: list[str],
    intra_raw: dict, all_daily: dict, vni_chg: float | None,
) -> list[dict]:
    """Phát hiện BUY SIGNAL (score≥4) đầu tiên của mỗi mã trong ngày."""
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
            price_chg   = _price_change_pct(open_p, close_p)
            high20      = _high20_before(all_daily, sym, sig_day)
            is_bo       = bool(high20 and close_p and close_p > high20)
            rs = (price_chg - vni_chg) if (price_chg is not None and vni_chg is not None) else None
            score = compute_score(ratio, price_chg, is_bo, rs)
            if score.total < MIN_SCORE:
                continue
            alerted.add(sym)

            # Momentum filter: check ratio ở phiên chiều (13:30)
            afternoon_sliced = _slice_intra(intra_raw.get(sym), _ts(sig_day, time(13, 30)))
            momentum_ok = False
            if afternoon_sliced and elapsed_minutes(time(13, 30)) > 0:
                aft_vol, _, _ = _parse_ohlcv(afternoon_sliced)
                aft_ratio = calculate_ratio(aft_vol, vma9, elapsed_minutes(time(13, 30)))
                momentum_ok = (aft_ratio is not None and aft_ratio >= 2.0)

            signals.append({
                "symbol": sym, "time": check_t.strftime("%H:%M"),
                "ratio": ratio, "score": score.total,
                "breakdown": score.breakdown(),
                "buy_price": close_p,
                "vni_green": vni_chg is not None and vni_chg > 0,
                "momentum_ok": momentum_ok,
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

    for check_day, is_final in [(T1[sig_day], False), (T2[sig_day], True)]:
        ohlcv = _day_ohlcv(all_daily, sym, check_day)
        if not ohlcv:
            if is_final:
                return None   # T+2 data chưa có (ngày tương lai)
            continue

        low  = ohlcv["low"]  or buy
        high = ohlcv["high"] or buy
        close = ohlcv["close"]

        if low <= sl:
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         sig["breakdown"], buy, sl, check_day,
                         "STOP_LOSS", STOP_LOSS, sig["vni_green"], sig["momentum_ok"])
        if high >= tp:
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         sig["breakdown"], buy, tp, check_day,
                         "TAKE_PROFIT", TAKE_PROFIT, sig["vni_green"], sig["momentum_ok"])
        if is_final:
            pnl = (close - buy) / buy * 100
            return Trade(sig_day, sym, sig["time"], sig["ratio"], sig["score"],
                         sig["breakdown"], buy, close, check_day,
                         "T+2", pnl, sig["vni_green"], sig["momentum_ok"])
    return None


# ── Output helpers ────────────────────────────────────────────────────────────

def _icon(t: Trade) -> str:
    return "✅" if t.pnl_pct > 0 else ("⚠️ " if abs(t.pnl_pct) < 0.01 else "❌")


def _print_table(trades: list[Trade], title: str):
    print(f"\n  ── {title} ({len(trades)} GD) ──")
    if not trades:
        print("    (không có giao dịch)")
        return
    print(f"  {'Ngày mua':<10}  {'Mã':<5}  {'Giờ':<5}  {'Sc':>4}  "
          f"{'Mua':>8}  {'Bán':>8}  {'P&L':>7}  {'Lý do':<13}  Bán ngày")
    print("  " + "─" * 78)
    for t in sorted(trades, key=lambda x: (x.signal_day, x.symbol)):
        print(f"  {str(t.signal_day):<10}  {t.symbol:<5}  {t.signal_time:<5}  "
              f"{t.score}/6  "
              f"{t.buy_price:>8,.1f}  {t.exit_price:>8,.1f}  "
              f"{t.pnl_pct:>+6.2f}%  {_icon(t)} {t.exit_reason:<10}  {t.exit_day}")
    _print_summary(trades)


def _print_summary(trades: list[Trade]):
    if not trades:
        return
    n     = len(trades)
    wins  = sum(1 for t in trades if t.pnl_pct > 0)
    avg   = sum(t.pnl_pct for t in trades) / n
    tp_n  = sum(1 for t in trades if t.exit_reason == "TAKE_PROFIT")
    sl_n  = sum(1 for t in trades if t.exit_reason == "STOP_LOSS")
    t2_n  = sum(1 for t in trades if t.exit_reason == "T+2")
    print(f"\n  Win rate: {wins}/{n} ({wins/n*100:.0f}%)  "
          f"Avg P&L: {avg:+.2f}%  |  TP:{tp_n}  SL:{sl_n}  T+2:{t2_n}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  BACKTEST — Scoring + Market Filter + Momentum + Exit Strategy")
    print(f"  Ngày test: {SIGNAL_DAYS[0]} → {SIGNAL_DAYS[-1]}")
    print(f"  Exit: Stop Loss {STOP_LOSS:+.0f}% | Take Profit +{TAKE_PROFIT:.0f}% | T+2 close")
    print("=" * 70)

    # 1. Symbols
    print("\n[1/4] Lấy danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # 2. Daily data (dùng cho VMA9 + exit prices)
    print(f"\n[2/4] Fetch daily OHLCV (2026-04-14 → 2026-05-29)...")
    all_daily = await fetch_all_ohlcv(
        symbols,
        _ts(date(2026, 4, 14), time(0, 0)),
        _ts(date(2026, 5, 29), time(23, 59, 59)),
        resolution="1D",
    )
    print(f"      {sum(1 for v in all_daily.values() if v and v.get('t'))} mã có dữ liệu")

    # 3. Intraday + VNINDEX mỗi ngày
    print(f"\n[3/4] Fetch VNINDEX + intraday 15-min ({len(SIGNAL_DAYS)} ngày)...")
    vni_by_day:   dict[date, float | None] = {}
    intra_by_day: dict[date, dict] = {}
    for d in SIGNAL_DAYS:
        vni, intra = await asyncio.gather(
            _fetch_vnindex(d),
            fetch_all_ohlcv(symbols, _ts(d, time(9, 0)), _ts(d, time(15, 0)), "15"),
        )
        vni_by_day[d]   = vni
        intra_by_day[d] = intra
        flag = "🟢" if (vni is not None and vni > 0) else "🔴"
        print(f"      {d} {flag} VNINDEX: {vni:+.2f}%" if vni is not None else f"      {d} ❓ VNINDEX: N/A")

    # 4. Detect + simulate
    print(f"\n[4/4] Phát hiện tín hiệu + simulate giao dịch...")
    all_trades: list[Trade] = []
    pending_count = 0

    for d in SIGNAL_DAYS:
        sigs = _detect_day(d, symbols, intra_by_day[d], all_daily, vni_by_day[d])
        trades, pending = [], 0
        for s in sigs:
            t = _simulate(s, d, all_daily)
            if t:
                trades.append(t)
                all_trades.append(t)
            else:
                pending += 1
        pending_count += pending
        t2_day = T2[d]
        note = f"⏳ {pending} chờ T+2 ({t2_day})" if pending else "✅ đủ data"
        print(f"      {d}: {len(sigs)} BUY SIGNAL → {len(trades)} GD  {note}")

    # ── Kết quả ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  KẾT QUẢ BACKTEST")
    print("=" * 70)

    if not all_trades:
        print("\n  Không có giao dịch nào có đủ T+2 data.")
        if pending_count:
            print(f"  ⏳ {pending_count} giao dịch chờ T+2 (ngày mai hoặc chưa về)")
        return

    # A. Không filter
    _print_table(all_trades, "A. Không có filter")

    # B. Market filter: chỉ ngày VNINDEX xanh
    mf = [t for t in all_trades if t.vni_green]
    _print_table(mf, "B. Market Filter: chỉ ngày VNINDEX xanh")

    # C. Market filter + Momentum filter
    mm = [t for t in all_trades if t.vni_green and t.momentum_ok]
    _print_table(mm, "C. Market + Momentum filter (ratio ≥2x vẫn duy trì chiều)")

    # ── So sánh tổng hợp ─────────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  SO SÁNH 3 CHIẾN LƯỢC:")
    print(f"{'─'*70}")
    for label, group in [
        ("A. Không filter       ", all_trades),
        ("B. Market filter (VNI)", mf),
        ("C. Market + Momentum  ", mm),
    ]:
        if not group:
            print(f"  {label}: 0 GD")
            continue
        n    = len(group)
        wins = sum(1 for t in group if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in group) / n
        print(f"  {label}: {wins:>2}/{n:<2} thắng ({wins/n*100:>3.0f}%)  avg P&L: {avg:>+6.2f}%")

    if pending_count:
        print(f"\n  ⏳ Còn {pending_count} tín hiệu chưa có T+2 data (ngày gần nhất)")
    print(f"{'─'*70}")

    # ── Phân tích ngày mua ───────────────────────────────────────────────────
    print("\n  BREAKDOWN THEO NGÀY MUA:")
    for d in SIGNAL_DAYS:
        day_trades = [t for t in all_trades if t.signal_day == d]
        if not day_trades:
            continue
        wins = sum(1 for t in day_trades if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in day_trades) / len(day_trades)
        vni  = vni_by_day[d]
        flag = "🟢" if (vni and vni > 0) else "🔴"
        vni_s = f"{vni:+.2f}%" if vni is not None else "N/A"
        t2d   = T2[d]
        print(f"  {d} {flag}{vni_s:<8} T+2={t2d}: "
              f"{wins}/{len(day_trades)} thắng  avg {avg:+.2f}%")

    print()


if __name__ == "__main__":
    asyncio.run(main())
