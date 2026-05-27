from datetime import datetime
from enum import Enum

from src.config.constants import TOTAL_TRADING_MINUTES
from src.core.scorer import Score

_SESSION_LABEL = {
    "ATO": "ATO",
    "MORNING": "sáng",
    "AFTERNOON": "chiều",
    "ATC": "ATC",
}


class AlertLevel(str, Enum):
    EXPLOSION = "explosion"
    WARNING = "warning"
    CRITICAL = "critical"


_EMOJI = {
    AlertLevel.EXPLOSION: "🔥",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}

_LABEL = {
    AlertLevel.EXPLOSION: "Bùng nổ mở cửa",
    AlertLevel.WARNING: "WARNING",
    AlertLevel.CRITICAL: "CRITICAL",
}


def format_alert(
    symbol: str,
    vol_today: int,
    vma9: float,
    ratio: float,
    elapsed: int,
    level: AlertLevel,
    session_name: str,
    open_price: float | None,
    current_price: float | None,
    check_time: datetime,
    price_change_pct: float | None = None,
    is_breakout20: bool = False,
    rs_vs_index: float | None = None,
    score: Score | None = None,
) -> str:
    emoji = _EMOJI[level]
    label = _LABEL[level]
    session_label = _SESSION_LABEL.get(session_name, session_name)
    expected = int(vma9 * elapsed / TOTAL_TRADING_MINUTES)

    # --- Giá ---
    price_str = f"{current_price:,.0f} đ" if current_price else "N/A"
    if price_change_pct is not None:
        direction = "🟢" if price_change_pct >= 0 else "🔴"
        price_str += f"  {direction} {price_change_pct:+.2f}%"

    # --- RS vs VN-Index ---
    rs_str = ""
    if rs_vs_index is not None:
        rs_icon = "💪" if rs_vs_index > 1.5 else ("➡️" if rs_vs_index > -1.0 else "👎")
        rs_str = f"\n   vs VN-Index : {rs_icon} {rs_vs_index:+.2f}% RS"

    # --- Breakout ---
    bo_str = "\n   Breakout 20: ✅ phá đỉnh 20 phiên" if is_breakout20 else ""

    # --- Score ---
    score_str = ""
    if score is not None and score.signal:
        score_str = (
            f"\n   {'─'*30}"
            f"\n   {score.signal}  [{score.breakdown()}]  ({score.total}/6đ)"
        )

    return (
        f"{emoji} <b>Volume đột biến – Mã: {symbol}</b>\n"
        f"   Thời điểm  : {check_time.strftime('%H:%M')} ({session_label}, {elapsed}/{TOTAL_TRADING_MINUTES} phút)\n"
        f"   Vol lũy kế : {vol_today:,} cp\n"
        f"   Vol kỳ vọng: {expected:,} cp (VMA9: {int(vma9):,})\n"
        f"   Tỉ lệ      : {ratio:.2f}x → {emoji} {label}\n"
        f"   Giá        : {price_str}"
        f"{rs_str}"
        f"{bo_str}"
        f"{score_str}"
    )
