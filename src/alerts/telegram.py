import asyncio
import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=get_settings().telegram_bot_token)
    return _bot


async def send_message(text: str) -> bool:
    for attempt in range(3):
        try:
            await _get_bot().send_message(
                chat_id=get_settings().telegram_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            return True
        except RetryAfter as exc:
            wait = int(exc.retry_after) + 1
            logger.warning("Telegram flood control, waiting %ds", wait)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("Telegram timeout, attempt %d", attempt + 1)
            await asyncio.sleep(2)
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False
    return False
