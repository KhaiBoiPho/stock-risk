from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.config.constants import SESSIONS, TradingSession, VN_TIMEZONE

_TZ = ZoneInfo(VN_TIMEZONE)


def now_vn() -> datetime:
    return datetime.now(_TZ)


def get_session(t: time) -> TradingSession | None:
    for session in SESSIONS:
        if session.start <= t < session.end:
            return session
    return None


def is_trading(t: time) -> bool:
    return get_session(t) is not None


def elapsed_minutes(t: time) -> int:
    """Trading minutes elapsed since 09:00 (excludes lunch break)."""
    if t < time(9, 0):
        return 0

    m = t.hour * 60 + t.minute

    if t < time(9, 15):
        return m - 540            # 09:00 = 540 → 0–15

    if t < time(11, 30):
        return 15 + (m - 555)    # 09:15 = 555 → 15–150

    if t < time(13, 0):
        return 150                # lunch break

    if t < time(14, 30):
        return 150 + (m - 780)   # 13:00 = 780 → 150–240

    if t < time(14, 46):
        return 240 + (m - 870)   # 14:30 = 870 → 240–255

    return 255
