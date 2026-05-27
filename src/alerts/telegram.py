import logging

from telegram import Bot
from telegram.constants import ParseMode

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=get_settings().telegram_bot_token)
    return _bot


async def send_message(text: str) -> bool:
    try:
        await _get_bot().send_message(
            chat_id=get_settings().telegram_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False
