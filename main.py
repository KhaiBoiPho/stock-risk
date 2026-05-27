import asyncio
import logging
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.alerts.telegram import send_message
from src.config.constants import VN_TIMEZONE
from src.config.settings import get_settings
from src.core.vma9 import update_vma9_all
from src.data.dnse_client import fetch_hose_symbols
from src.data.redis_store import RedisStore
from src.jobs.check import check_volume
from src.jobs.reset import reset_daily
from src.jobs.vma9_update import update_vma9

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())

    symbols = await fetch_hose_symbols()
    logger.info("Loaded %d HoSE symbols", len(symbols))

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
        args=[symbols, store],
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
    weekday = ["Thứ 2","Thứ 3","Thứ 4","Thứ 5","Thứ 6","Thứ 7","Chủ nhật"][now.weekday()]
    await send_message(
        f"🤖 <b>Volume Spike Bot — {weekday} {now.strftime('%d/%m/%Y')}</b>\n"
        f"   Theo dõi: {len(symbols)} mã HoSE\n"
        f"   Khởi động lúc: {now.strftime('%H:%M:%S')}\n"
        f"   VMA9 sẵn sàng — chờ phiên 09:15"
    )

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
