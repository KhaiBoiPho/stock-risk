from datetime import datetime
from enum import Enum

from src.config.constants import TOTAL_TRADING_MINUTES

_SESSION_LABEL = {
    "ATO": "ATO",
    "MORNING": "Sang",
    "AFTERNOON": "Chieu",
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
    AlertLevel.EXPLOSION: "Bung no mo cua",
    AlertLevel.WARNING: "WARNING",
    AlertLevel.CRITICAL: "CRITICAL",
}

_EXCHANGE_LABEL = {
    "HOSE": "HoSE",
    "HNX": "HNX",
    "UPCOM": "UPCoM",
}

# Dùng ASCII thuần trong <pre> để tránh lỗi alignment trên mobile
# (ký tự Unicode tiếng Việt render rộng hơn 1 char trên nhiều font mobile)
_LW = 12   # label column
_VW = 14   # value column

_TOP  = "+" + "-" * (_LW + 2) + "+" + "-" * (_VW + 2) + "+"
_HSEP = "+" + "-" * (_LW + 2) + "+" + "-" * (_VW + 2) + "+"
_BOT  = "+" + "-" * (_LW + 2) + "+" + "-" * (_VW + 2) + "+"
_HDR_TOP = "+" + "-" * (_LW + _VW + 5) + "+"
_HDR_SEP = "+" + "-" * (_LW + _VW + 5) + "+"


def _hdr(text: str) -> str:
    inner = _LW + _VW + 5
    return f"| {text:<{inner - 2}} |"


def _row(label: str, value: str) -> str:
    return f"| {label:<{_LW}} | {value:>{_VW}} |"


def format_alert(
    symbol: str,
    vol_today: int,
    vma9: float,
    ratio: float,
    elapsed: int,
    level: AlertLevel,
    session_name: str,
    current_price: float | None,    # don vi: nghin dong (DNSE API)
    check_time: datetime,
    exchange: str = "HOSE",
) -> str:
    emoji = _EMOJI[level]
    label = _LABEL[level]
    session_label = _SESSION_LABEL.get(session_name, session_name)
    exchange_label = _EXCHANGE_LABEL.get(exchange, exchange)
    expected = int(vma9 * elapsed / TOTAL_TRADING_MINUTES)

    # Gia: DNSE tra nghin dong -> x1000 de hien thi VND thuc
    price_vnd = current_price * 1000 if current_price else None
    price_str = f"{price_vnd:,.0f} d" if price_vnd else "N/A"

    # Gia tri giao dich: vol(cp) x price(nghin d) / 1,000,000 = ty dong
    gt_str      = f"{vol_today * current_price / 1_000_000:.2f} ty" if current_price else "N/A"
    vma9_gt_str = f"{vma9 * current_price / 1_000_000:.2f} ty"      if current_price else "N/A"

    hdr_txt = f"{check_time.strftime('%H:%M')}  {session_label}  {elapsed}/{TOTAL_TRADING_MINUTES}p"

    return (
        f"{emoji} <b>[{exchange_label}] {symbol}</b> — {emoji} {label}\n"
        f"<pre>"
        f"{_HDR_TOP}\n"
        f"{_hdr(hdr_txt)}\n"
        f"{_HDR_SEP}\n"
        f"{_row('Vol hom nay',  f'{vol_today:,} cp')}\n"
        f"{_row('Vol ky vong',  f'{expected:,} cp')}\n"
        f"{_row('VMA9/phien',   f'{int(vma9):,} cp')}\n"
        f"{_HSEP}\n"
        f"{_row('Ti le',        f'{ratio:.2f}x')}\n"
        f"{_row('Gia',          price_str)}\n"
        f"{_row('GT hom nay',   gt_str)}\n"
        f"{_row('GT VMA9',      vma9_gt_str)}\n"
        f"{_BOT}"
        f"</pre>"
    )
