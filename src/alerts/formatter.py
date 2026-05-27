from datetime import datetime
from enum import Enum

from src.config.constants import TOTAL_TRADING_MINUTES

_SESSION_LABEL = {
    "ATO": "ATO",
    "MORNING": "sáng",
    "AFTERNOON": "chiều",
    "ATC": "ATC",
}


class AlertLevel(str, Enum):
    EXPLOSION = "explosion"  # ATO ratio ≥ 3
    WARNING = "warning"      # 2 ≤ ratio < 3
    CRITICAL = "critical"    # ratio ≥ 3


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
    current_price: float | None,
    check_time: datetime,
) -> str:
    emoji = _EMOJI[level]
    label = _LABEL[level]
    session_label = _SESSION_LABEL.get(session_name, session_name)
    expected = int(vma9 * elapsed / TOTAL_TRADING_MINUTES)
    price_str = f"{current_price:,.0f} đ" if current_price else "N/A"

    return (
        f"{emoji} <b>Volume đột biến – Mã: {symbol}</b>\n"
        f"   Thời điểm  : {check_time.strftime('%H:%M')} ({session_label}, {elapsed}/{TOTAL_TRADING_MINUTES} phút)\n"
        f"   Vol lũy kế : {vol_today:,} cp\n"
        f"   Vol kỳ vọng: {expected:,} cp (VMA9: {int(vma9):,})\n"
        f"   Tỉ lệ      : {ratio:.2f}x → {emoji} {label}\n"
        f"   Giá hiện tại: {price_str}"
    )
