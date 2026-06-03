from dataclasses import dataclass
from datetime import time

VN_TIMEZONE = "Asia/Ho_Chi_Minh"
TOTAL_TRADING_MINUTES = 255
ALERT_TTL_SECONDS = 600  # 10 minutes — ensures re-alert at next 15-min check cycle

DNSE_OHLCV_URL = "https://services.entrade.com.vn/chart-api/v2/ohlcs/stock"
VNDIRECT_STOCKS_URL = "https://finfo-api.vndirect.com.vn/v4/stocks"


@dataclass(frozen=True)
class TradingSession:
    name: str
    start: time
    end: time          # exclusive, except ATC which includes 14:45
    is_auction: bool   # True for ATO / ATC


# Order matters: sessions are checked top-to-bottom in get_session()
SESSIONS: list[TradingSession] = [
    TradingSession("ATO",       time(9,  0), time(9,  15), True),
    TradingSession("MORNING",   time(9, 15), time(11, 30), False),
    TradingSession("AFTERNOON", time(13, 0), time(14, 30), False),
    TradingSession("ATC",       time(14,30), time(14, 46), True),   # end=14:46 to include 14:45
]
