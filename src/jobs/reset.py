import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from src.alerts.telegram import send_message
from src.config.constants import VN_TIMEZONE
from src.data.redis_store import RedisStore

logger = logging.getLogger(__name__)

_WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]


async def reset_daily(store: RedisStore) -> None:
    logger.info("Running daily reset")
    await store.reset_daily()

    now = datetime.now(ZoneInfo(VN_TIMEZONE))
    weekday = _WEEKDAYS[now.weekday()]
    await send_message(
        f"📈 <b>Bắt đầu phiên — {weekday} {now.strftime('%d/%m/%Y')}</b>\n"
        f"   Bot đang hoạt động ✅\n"
        f"   Bắt đầu quét từ 09:15"
    )
