import asyncio
import logging
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.alerts.formatter import format_startup
from src.alerts.telegram import send_message
from src.config.constants import VN_TIMEZONE
from src.config.settings import get_settings
from src.core.vma9 import update_vma9_all
from src.data.dnse_client import fetch_hose_symbols, fetch_hnx_symbols, fetch_upcom_symbols
from src.data.redis_store import RedisStore
from src.jobs.check import check_volume
from src.jobs.reset import reset_daily
from src.jobs.vma9_update import update_vma9

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Tắt log từng HTTP request của httpx — quá ồn với 1500+ symbols/lần fetch
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID chưa được cấu hình — alerts sẽ không gửi được!")

    hose_symbols = await fetch_hose_symbols()
    logger.info("Loaded %d HoSE symbols", len(hose_symbols))

    hnx_symbols = await fetch_hnx_symbols()
    logger.info("Loaded %d HNX symbols", len(hnx_symbols))

    upcom_symbols = await fetch_upcom_symbols()
    logger.info("Loaded %d UPCoM symbols", len(upcom_symbols))

    exchange_map: dict[str, str] = {s: "HOSE" for s in hose_symbols}
    exchange_map.update({s: "HNX" for s in hnx_symbols})
    exchange_map.update({s: "UPCOM" for s in upcom_symbols})
    symbols = sorted(exchange_map.keys())
    logger.info("Total symbols to monitor: %d", len(symbols))

    store = await RedisStore.create()
    logger.info("Redis connected")

    # Bootstrap VMA9 if not already populated
    logger.info("Running startup VMA9 update …")
    await update_vma9_all(symbols, store)

    scheduler = AsyncIOScheduler(timezone=VN_TIMEZONE)

    # 09:00 — reset vol_today / alerted keys
    scheduler.add_job(
        reset_daily,
        CronTrigger(hour=9, minute=0, timezone=VN_TIMEZONE),
        args=[store],
        id="reset_daily",
        name="Daily reset",
        misfire_grace_time=60,
    )

    # Every 15 min from 09:00–14:45 (session guard inside the job)
    scheduler.add_job(
        check_volume,
        CronTrigger(hour="9,10,11,13,14", minute="0,15,30,45", timezone=VN_TIMEZONE),
        args=[symbols, store, exchange_map],
        id="check_volume",
        name="Volume check",
        misfire_grace_time=120,
        max_instances=1,
    )

    # 14:50 — recalculate VMA9 after market close
    scheduler.add_job(
        update_vma9,
        CronTrigger(hour=14, minute=50, timezone=VN_TIMEZONE),
        args=[symbols, store],
        id="update_vma9",
        name="VMA9 update",
        misfire_grace_time=300,
    )

    scheduler.start()
    logger.info("Scheduler started — waiting for jobs")

    now = datetime.now(ZoneInfo(VN_TIMEZONE))
    if now.weekday() < 5:  # Thứ 2–6 mới báo
        weekday = ["Thu 2", "Thu 3", "Thu 4", "Thu 5", "Thu 6"][now.weekday()]
        await send_message(format_startup(
            weekday, now, len(hose_symbols), len(hnx_symbols), len(upcom_symbols)
        ))

    stop_event = asyncio.Event()

    def _on_signal(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    await stop_event.wait()

    logger.info("Shutting down …")
    scheduler.shutdown(wait=False)
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
