"""
Layer 1 — Market Timing: Tự động xác định giai đoạn thị trường uptrend
Layer 2 — Stock Selection: Volume spike + MA + RSI trong giai đoạn đó

Uptrend score (0-3) mỗi ngày:
  +1  VNI close > VNI MA20   (ngắn hạn bullish)
  +1  VNI MA20 > VNI MA50    (trung hạn bullish)
  +1  VNI slope dương        (close > close 10 ngày trước)

Uptrend period = score == 3 ít nhất 5 ngày liên tiếp
Chỉ chạy thuật toán mua cổ khi đang trong uptrend period.
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from statistics import mean
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
# Layer 1: Market timing
VNI_HISTORY_START = date(2025, 6,  1)   # lấy từ đây để đủ MA50
VNI_HISTORY_END   = date(2026, 5, 29)
UPTREND_MIN_DAYS  = 5                   # phải duy trì score=3 ít nhất N ngày

# Layer 2: Stock filter
STOP_LOSS        = -7.0
TAKE_PROFIT      =  7.0
MIN_SCORE        =  4
ATO_THRESH       =  3.0
WARN_THRESH      =  2.0
CRIT_THRESH      =  3.0
RSI_LOW, RSI_HIGH = 40.0, 70.0
EARLY_EXIT_T1_PCT = -3.0   # exit T+1 close nếu đã lỗ hơn ngưỡng này

# Daily stock data từ khi nào (cần đủ MA50 + VMA9 trước signal day đầu tiên)
STOCK_DAILY_START = date(2025, 9,  1)

CHECK_TIMES = [
    time(9, 15), time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(10, 30),
    time(10, 45), time(11, 0), time(11, 15), time(11, 30),
    time(13, 0), time(13, 15), time(13, 30), time(13, 45), time(14, 0),
    time(14, 15), time(14, 30), time(14, 45),
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DayScore:
    d:         date
    close:     float
    ma20:      float
    ma50:      float
    slope_ok:  bool
    score:     int   # 0-3


@dataclass
class UptrendPeriod:
    start: date
    end:   date
    days:  int
    avg_score: float


@dataclass
class Signal:
    signal_day:  date
    symbol:      str
    signal_time: str
    ratio:       float
    score:       int
    buy_price:   float
    ma_ok:       bool
    rsi_ok:      bool
    rsi14:       float | None
    vni_green:   bool = False   # VNI xanh cùng ngày
    is_morning:  bool = False   # signal trong phiên sáng (≤11:30)


@dataclass
class Trade:
    sig:         Signal
    exit_price:  float
    exit_day:    date
    exit_reason: str
    pnl_pct:     float


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=_TZ).timestamp())


def _trading_cal_from_vni(vni_daily: dict) -> list[date]:
    """Lấy trading calendar thực tế từ VNINDEX daily data."""
    if not vni_daily or not vni_daily.get("t"):
        return []
    return sorted({datetime.fromtimestamp(ts, tz=_TZ).date() for ts in vni_daily["t"]})


def _vni_closes_up_to(vni_daily: dict, up_to: date) -> list[float]:
    """Close prices VNINDEX trước ngày up_to (không gồm up_to)."""
    cut = _ts(up_to, time(0, 0))
    return [c for ts, c in zip(vni_daily["t"], vni_daily["c"]) if ts < cut and c]


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


def _closes_before(all_daily: dict, sym: str, sig_day: date, n: int) -> list[float]:
    data = all_daily.get(sym)
    if not data or not data.get("t"):
        return []
    cut = _ts(sig_day, time(0, 0))
    closes = [c for ts, c in zip(data["t"], data["c"]) if ts < cut and c and c > 0]
    return closes[-n:]


def _ma(closes: list[float], period: int) -> float | None:
    return mean(closes[-period:]) if len(closes) >= period else None


def _rsi14(all_daily: dict, sym: str, sig_day: date) -> float | None:
    closes = _closes_before(all_daily, sym, sig_day, 15)
    if len(closes) < 15:
        return None
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, 15)]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, 15)]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _vma9_before(all_daily: dict, sym: str, sig_day: date) -> float | None:
    data = all_daily.get(sym)
    if not data or not data.get("t"):
        return None
    cut = _ts(sig_day, time(0, 0))
    vols = [v for ts, v in zip(data["t"], data["v"]) if ts < cut and v and v > 0]
    return mean(vols[-9:]) if len(vols) >= 3 else None


def _high20_before(all_daily: dict, sym: str, sig_day: date) -> float | None:
    data = all_daily.get(sym)
    if not data or not data.get("t") or "h" not in data:
        return None
    cut = _ts(sig_day, time(0, 0))
    highs = [h for ts, h in zip(data["t"], data["h"]) if ts < cut and h and h > 0]
    return max(highs[-20:]) if highs else None


# ── Layer 1: Market timing ────────────────────────────────────────────────────

def compute_day_scores(vni_daily: dict, cal: list[date]) -> list[DayScore]:
    """Tính uptrend score cho mỗi ngày trong trading calendar."""
    scores: list[DayScore] = []
    for d in cal:
        closes = _vni_closes_up_to(vni_daily, d)
        # Lấy close của ngày d
        d0, d1 = _ts(d, time(0, 0)), _ts(d, time(23, 59, 59))
        today_closes = [c for ts, c in zip(vni_daily["t"], vni_daily["c"]) if d0 <= ts <= d1]
        if not today_closes or len(closes) < 50:
            continue
        close  = today_closes[-1]
        ma20   = mean(closes[-20:])
        ma50   = mean(closes[-50:])
        slope  = close > closes[-10] if len(closes) >= 10 else False
        sc = (1 if close > ma20 else 0) + (1 if ma20 > ma50 else 0) + (1 if slope else 0)
        scores.append(DayScore(d, close, ma20, ma50, slope, sc))
    return scores


def find_uptrend_periods(scores: list[DayScore], min_days: int) -> list[UptrendPeriod]:
    """Tìm các giai đoạn uptrend score=3 liên tục ≥ min_days."""
    periods: list[UptrendPeriod] = []
    run: list[DayScore] = []
    for ds in scores:
        if ds.score == 3:
            run.append(ds)
        else:
            if len(run) >= min_days:
                periods.append(UptrendPeriod(
                    run[0].d, run[-1].d, len(run),
                    mean(s.score for s in run),
                ))
            run = []
    if len(run) >= min_days:
        periods.append(UptrendPeriod(run[0].d, run[-1].d, len(run), mean(s.score for s in run)))
    return periods


async def fetch_vnindex_daily() -> dict:
    url = "https://services.entrade.com.vn/chart-api/v2/ohlcs/index"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params={
            "symbol": "VNINDEX", "resolution": "1D",
            "from": _ts(VNI_HISTORY_START, time(0, 0)),
            "to":   _ts(VNI_HISTORY_END,   time(23, 59, 59)),
        })
        return r.json() if r.status_code == 200 else {}


async def fetch_vnindex_chg(d: date) -> float | None:
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


# ── Layer 2: Stock selection ──────────────────────────────────────────────────

def detect_signals_day(
    sig_day: date, symbols: list[str],
    intra_raw: dict, all_daily: dict, vni_chg: float | None,
) -> list[Signal]:
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
            closes = _closes_before(all_daily, sym, sig_day, 50)
            ma20 = _ma(closes, 20)
            ma50 = _ma(closes, 50)
            price = close_p or 0.0
            rsi   = _rsi14(all_daily, sym, sig_day)
            ma_ok  = bool(ma20 and ma50 and price > ma20 > ma50)
            rsi_ok = rsi is not None and RSI_LOW <= rsi <= RSI_HIGH
            signals.append(Signal(
                signal_day=sig_day, symbol=sym,
                signal_time=check_t.strftime("%H:%M"),
                ratio=ratio, score=score.total,
                buy_price=price,
                ma_ok=ma_ok, rsi_ok=rsi_ok, rsi14=rsi,
                vni_green=bool(vni_chg and vni_chg > 0),
                is_morning=(check_t <= time(11, 30)),
            ))
    return signals


def simulate(sig: Signal, all_daily: dict, cal: list[date]) -> Trade | None:
    buy = sig.buy_price
    if not buy or buy <= 0:
        return None
    sl_p = buy * (1 + STOP_LOSS / 100)
    tp_p = buy * (1 + TAKE_PROFIT / 100)
    try:
        idx = cal.index(sig.signal_day)
        t1  = cal[idx + 1] if idx + 1 < len(cal) else None
        t2  = cal[idx + 2] if idx + 2 < len(cal) else None
    except ValueError:
        return None
    if not t1 or not t2:
        return None
    for check_day, is_final in [(t1, False), (t2, True)]:
        ohlcv = _day_ohlcv(all_daily, sig.symbol, check_day)
        if not ohlcv:
            if is_final:
                return None
            continue
        low, high, close = ohlcv["low"] or buy, ohlcv["high"] or buy, ohlcv["close"] or buy
        if low <= sl_p:
            return Trade(sig, sl_p, check_day, "SL", STOP_LOSS)
        if high >= tp_p:
            return Trade(sig, tp_p, check_day, "TP", TAKE_PROFIT)
        if is_final:
            return Trade(sig, close, check_day, "T2", (close - buy) / buy * 100)
    return None


def simulate_v2(sig: Signal, all_daily: dict, cal: list[date]) -> Trade | None:
    """Exit cải tiến: T+1 early exit nếu đã lỗ > EARLY_EXIT_T1_PCT%, dynamic TP."""
    buy = sig.buy_price
    if not buy or buy <= 0:
        return None
    sl_p    = buy * (1 + STOP_LOSS / 100)
    tp_p    = buy * (1 + TAKE_PROFIT / 100)
    early_p = buy * (1 + EARLY_EXIT_T1_PCT / 100)   # -3% default
    try:
        idx = cal.index(sig.signal_day)
        t1  = cal[idx + 1] if idx + 1 < len(cal) else None
        t2  = cal[idx + 2] if idx + 2 < len(cal) else None
    except ValueError:
        return None
    if not t1 or not t2:
        return None

    # ── T+1: SL / TP / early exit ────────────────────────────────────────────
    t1_ohlcv = _day_ohlcv(all_daily, sig.symbol, t1)
    if t1_ohlcv:
        low1  = t1_ohlcv["low"]   or buy
        high1 = t1_ohlcv["high"]  or buy
        cls1  = t1_ohlcv["close"] or buy
        if low1 <= sl_p:
            return Trade(sig, sl_p, t1, "SL", STOP_LOSS)
        if high1 >= tp_p:
            return Trade(sig, tp_p, t1, "TP", TAKE_PROFIT)
        # early exit: đóng cửa T+1 đã lỗ hơn ngưỡng, đừng chờ T+2
        if cls1 <= early_p:
            return Trade(sig, cls1, t1, "T1E", (cls1 - buy) / buy * 100)

    # ── T+2: final exit ───────────────────────────────────────────────────────
    t2_ohlcv = _day_ohlcv(all_daily, sig.symbol, t2)
    if not t2_ohlcv:
        return None
    low2  = t2_ohlcv["low"]   or buy
    high2 = t2_ohlcv["high"]  or buy
    cls2  = t2_ohlcv["close"] or buy
    if low2 <= sl_p:
        return Trade(sig, sl_p, t2, "SL", STOP_LOSS)
    if high2 >= tp_p:
        return Trade(sig, tp_p, t2, "TP", TAKE_PROFIT)
    return Trade(sig, cls2, t2, "T2", (cls2 - buy) / buy * 100)


# ── Report ────────────────────────────────────────────────────────────────────

def _pnl_bar(avg: float) -> str:
    if avg >= 0:
        return "🟢" * min(int(avg / 0.5) + 1, 10)
    return "🔴" * min(int(-avg / 0.5) + 1, 10)


def print_result(label: str, trades: list[Trade]):
    if not trades:
        print(f"  {label}: — (0 GD)")
        return
    n    = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    avg  = sum(t.pnl_pct for t in trades) / n
    tp_n = sum(1 for t in trades if t.exit_reason == "TP")
    sl_n = sum(1 for t in trades if t.exit_reason == "SL")
    t2_n = n - tp_n - sl_n
    print(f"  {label}: {wins:>3}/{n:<3}  win {wins/n*100:>3.0f}%  "
          f"avg {avg:>+6.2f}%  TP:{tp_n:<3}SL:{sl_n:<3}T2:{t2_n}  {_pnl_bar(avg)}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  MARKET TIMING BACKTEST")
    print("  Layer 1: Tự động xác định uptrend VNINDEX")
    print("  Layer 2: Volume spike + MA20/MA50 + RSI14")
    print(f"  Exit: SL {STOP_LOSS:+.0f}%  TP +{TAKE_PROFIT:.0f}%")
    print("=" * 70)

    # ── 1. VNINDEX daily — xác định uptrend ──────────────────────────────────
    print(f"\n[1/6] Fetch VNINDEX daily ({VNI_HISTORY_START} → {VNI_HISTORY_END})...")
    vni_daily = await fetch_vnindex_daily()
    if not vni_daily.get("t"):
        print("  ❌ Không lấy được VNINDEX data")
        return
    print(f"      {len(vni_daily['t'])} candle")

    cal_full = _trading_cal_from_vni(vni_daily)
    print(f"      Trading calendar: {cal_full[0]} → {cal_full[-1]} ({len(cal_full)} ngày)")

    # ── 2. Tính uptrend score mỗi ngày ───────────────────────────────────────
    print("\n[2/6] Tính uptrend score từng ngày...")
    day_scores = compute_day_scores(vni_daily, cal_full)
    uptrend_periods = find_uptrend_periods(day_scores, UPTREND_MIN_DAYS)

    print(f"\n  VNINDEX score (0=downtrend … 3=uptrend mạnh):")
    print(f"  {'Ngày':<10}  {'Close':>7}  {'MA20':>7}  {'MA50':>7}  {'Slope':>5}  Score")
    print("  " + "─" * 50)
    for ds in day_scores[-30:]:   # hiện 30 ngày gần nhất
        bar = "█" * ds.score + "░" * (3 - ds.score)
        trend = "📈" if ds.score == 3 else ("➡️ " if ds.score == 2 else "📉")
        print(f"  {str(ds.d):<10}  {ds.close:>7.1f}  {ds.ma20:>7.1f}  "
              f"{ds.ma50:>7.1f}  {'✅' if ds.slope_ok else '❌':>5}  [{bar}] {ds.score} {trend}")

    print(f"\n  Tất cả giai đoạn UPTREND (score=3, ≥{UPTREND_MIN_DAYS} ngày):")
    if not uptrend_periods:
        print("  ❌ Không tìm thấy giai đoạn uptrend đủ dài.")
        return
    for p in uptrend_periods:
        print(f"    📈 {p.start} → {p.end}  ({p.days} ngày)")

    # ── 3. Symbols ───────────────────────────────────────────────────────────
    print("\n[3/6] Danh sách mã HoSE...")
    symbols = await fetch_hose_symbols()
    print(f"      {len(symbols)} mã")

    # Chọn giai đoạn gần nhất có intraday data (probe từ recent → oldest)
    print("\n  → Kiểm tra intraday data (gần nhất trước)...")
    probe_sym = symbols[:5]
    target = None
    signal_days: list[date] = []
    for period in sorted(uptrend_periods, key=lambda p: p.start, reverse=True):
        cand = [d for d in cal_full if period.start <= d <= period.end]
        if len(cand) <= 2:
            continue
        probe_day = cand[0]
        probe = await fetch_all_ohlcv(
            probe_sym,
            _ts(probe_day, time(9, 0)), _ts(probe_day, time(15, 0)), "15",
        )
        has_data = any(v and v.get("t") for v in probe.values())
        status = "✅ có data" if has_data else "❌ không có data"
        print(f"     {period.start} → {period.end} ({period.days}d): {status}")
        if has_data and not target:
            target = period
            signal_days = cand[:-2]  # bỏ 2 ngày cuối để có T+2

    if not target or not signal_days:
        print("  ❌ Không có giai đoạn uptrend nào có intraday data.")
        return

    print(f"\n  ✅ Chọn giai đoạn có data gần nhất:")
    print(f"     {target.start} → {target.end}  ({target.days} ngày giao dịch)")
    print(f"     Signal days: {signal_days[0]} → {signal_days[-1]} ({len(signal_days)} ngày)")

    # ── 4. Daily stock OHLCV ─────────────────────────────────────────────────
    idx_last = cal_full.index(signal_days[-1]) if signal_days[-1] in cal_full else -1
    stock_end = cal_full[idx_last + 2] if idx_last >= 0 and idx_last + 2 < len(cal_full) else signal_days[-1]
    print(f"\n[4/6] Daily OHLCV stocks ({STOCK_DAILY_START} → {stock_end})...")
    all_daily = await fetch_all_ohlcv(
        symbols,
        _ts(STOCK_DAILY_START, time(0, 0)),
        _ts(stock_end,         time(23, 59, 59)),
        resolution="1D",
    )
    print(f"      {sum(1 for v in all_daily.values() if v and v.get('t'))} mã có data")

    # ── 5. Intraday + VNINDEX per signal day ─────────────────────────────────
    print(f"\n[5/6] Intraday 15-min + VNINDEX ({len(signal_days)} ngày signal)...")
    vni_chg_by_day: dict[date, float | None] = {}
    intra_by_day:   dict[date, dict] = {}

    for d in signal_days:
        vni_chg, intra = await asyncio.gather(
            fetch_vnindex_chg(d),
            fetch_all_ohlcv(symbols, _ts(d, time(9, 0)), _ts(d, time(15, 0)), "15"),
        )
        vni_chg_by_day[d] = vni_chg
        intra_by_day[d]   = intra
        n_with_data = sum(1 for v in intra.values() if v and v.get("t"))
        g = "🟢" if (vni_chg and vni_chg > 0) else "🔴"
        v = f"{vni_chg:+.2f}%" if vni_chg is not None else "  N/A"
        print(f"      {d}  {g}{v:<8}  {n_with_data} mã có data")

    # ── 6. Detect + simulate ─────────────────────────────────────────────────
    print("\n[6/6] Detect signals + simulate trades...")
    all_signals:   list[Signal] = []
    trades_v1:     list[Trade]  = []  # exit gốc
    trades_v2:     list[Trade]  = []  # exit cải tiến (T+1 early exit)
    pending = 0

    for d in signal_days:
        if not any(v and v.get("t") for v in intra_by_day.get(d, {}).values()):
            continue
        sigs = detect_signals_day(d, symbols, intra_by_day[d], all_daily, vni_chg_by_day[d])
        all_signals.extend(sigs)
        for s in sigs:
            t1 = simulate(s, all_daily, cal_full)
            t2 = simulate_v2(s, all_daily, cal_full)
            if t1:
                trades_v1.append(t1)
            else:
                pending += 1
            if t2:
                trades_v2.append(t2)
        print(f"      {d}: {len(sigs):>2} BUY SIGNAL")

    print(f"\n      Tổng: {len(all_signals)} tín hiệu → {len(trades_v1)} GD có đủ T+2 data")

    # ── Kết quả ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  KẾT QUẢ — Uptrend {signal_days[0]} → {signal_days[-1]}")
    print(f"  Exit V1 = SL{STOP_LOSS:+.0f}% / TP+{TAKE_PROFIT:.0f}% / T+2")
    print(f"  Exit V2 = V1 + T1 early exit nếu lỗ >{abs(EARLY_EXIT_T1_PCT):.0f}% sau T+1")
    print("=" * 70)

    def _f(trades, fn):
        return [t for t in trades if fn(t.sig)]

    A  = trades_v1
    F  = _f(trades_v1,  lambda s: s.ma_ok and s.rsi_ok)
    G  = _f(trades_v1,  lambda s: s.ma_ok and s.rsi_ok and s.score >= 5)
    H  = _f(trades_v1,  lambda s: s.ma_ok and s.rsi_ok and s.vni_green)
    I  = _f(trades_v1,  lambda s: s.ma_ok and s.rsi_ok and s.is_morning)
    J  = _f(trades_v1,  lambda s: s.ma_ok and s.rsi_ok and s.score >= 5 and s.vni_green and s.is_morning)

    Fv = _f(trades_v2,  lambda s: s.ma_ok and s.rsi_ok)
    Gv = _f(trades_v2,  lambda s: s.ma_ok and s.rsi_ok and s.score >= 5)
    Jv = _f(trades_v2,  lambda s: s.ma_ok and s.rsi_ok and s.score >= 5 and s.vni_green and s.is_morning)

    print(f"\n  {'Filter':<52} {'Win':>4} {'Total':>5} {'Win%':>5} {'Avg P&L':>8}  {'TP':>4} {'SL':>4} {'T1E':>4} {'T2':>4}")
    print("  " + "─" * 95)

    def _row(label, group):
        if not group:
            print(f"  {label:<52}   —     —      —        —")
            return
        n    = len(group)
        wins = sum(1 for t in group if t.pnl_pct > 0)
        avg  = sum(t.pnl_pct for t in group) / n
        tp_n = sum(1 for t in group if t.exit_reason == "TP")
        sl_n = sum(1 for t in group if t.exit_reason == "SL")
        t1e_n= sum(1 for t in group if t.exit_reason == "T1E")
        t2_n = n - tp_n - sl_n - t1e_n
        bar  = "🟢" if avg >= 0 else "🔴"
        print(f"  {label:<52} {wins:>4}/{n:<5} {wins/n*100:>4.0f}%  {avg:>+7.2f}%  {bar}"
              f"  {tp_n:>4} {sl_n:>4} {t1e_n:>4} {t2_n:>4}")

    _row("A.  Tất cả (baseline, exit V1)", A)
    _row("F.  MA+RSI (exit V1)", F)
    _row("G.  MA+RSI + Score≥5 (exit V1)", G)
    _row("H.  MA+RSI + VNI xanh (exit V1)", H)
    _row("I.  MA+RSI + Sáng ≤11:30 (exit V1)", I)
    _row("J.  MA+RSI + Score≥5 + VNI xanh + Sáng (exit V1)", J)
    print()
    _row("F'. MA+RSI (exit V2: T1 early exit)", Fv)
    _row("G'. MA+RSI + Score≥5 (exit V2)", Gv)
    _row("J'. MA+RSI + Score≥5 + VNI xanh + Sáng (exit V2)", Jv)

    # ── Detail table cho J' (best expected) ──────────────────────────────────
    best_group = Jv if Jv else J
    best_label = "J' (full strict + exit V2)" if Jv else "J (full strict)"
    if best_group:
        print(f"\n  CHI TIẾT — {best_label} ({len(best_group)} giao dịch):")
        print(f"  {'Ngày mua':<10}  {'Mã':<5}  {'Giờ':<5}  {'Sc':>4}  "
              f"{'Mua':>8}  {'Bán':>8}  {'P&L':>7}  Lý do   Ngày bán")
        print("  " + "─" * 72)
        for t in sorted(best_group, key=lambda x: x.sig.signal_day):
            icon = "✅" if t.pnl_pct > 0 else "❌"
            print(f"  {str(t.sig.signal_day):<10}  {t.sig.symbol:<5}  "
                  f"{t.sig.signal_time:<5}  {t.sig.score}/6  "
                  f"{t.sig.buy_price:>8,.1f}  {t.exit_price:>8,.1f}  "
                  f"{t.pnl_pct:>+6.2f}%  {icon} {t.exit_reason:<6}  {t.exit_day}")

    # ── Summary tổng hợp ──────────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  TỔNG KẾT:")
    for label, group in [("F (baseline)", F), ("J (strict entry)", J), ("J' (strict+V2 exit)", Jv)]:
        if not group:
            continue
        n, wins = len(group), sum(1 for t in group if t.pnl_pct > 0)
        avg   = sum(t.pnl_pct for t in group) / n
        best  = max(group, key=lambda t: t.pnl_pct)
        worst = min(group, key=lambda t: t.pnl_pct)
        tp_n  = sum(1 for t in group if t.exit_reason == "TP")
        sl_n  = sum(1 for t in group if t.exit_reason == "SL")
        t1e_n = sum(1 for t in group if t.exit_reason == "T1E")
        t2_n  = n - tp_n - sl_n - t1e_n
        print(f"\n  {label}:")
        print(f"    Win rate : {wins}/{n} ({wins/n*100:.0f}%)")
        print(f"    Avg P&L  : {avg:+.2f}%")
        print(f"    Best     : {best.sig.symbol} {best.pnl_pct:+.2f}% ({best.exit_reason})")
        print(f"    Worst    : {worst.sig.symbol} {worst.pnl_pct:+.2f}% ({worst.exit_reason})")
        print(f"    Exit     : TP {tp_n}({tp_n/n*100:.0f}%)  SL {sl_n}({sl_n/n*100:.0f}%)"
              f"  T1E {t1e_n}({t1e_n/n*100:.0f}%)  T+2 {t2_n}({t2_n/n*100:.0f}%)")

    if pending:
        print(f"\n  ⏳ {pending} tín hiệu thiếu T+2 data")

    print()


if __name__ == "__main__":
    asyncio.run(main())
