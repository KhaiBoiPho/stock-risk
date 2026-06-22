"""
One-time script to pre-populate VMA9 for all HoSE symbols.
Run this before starting the main scheduler for the first time.

Usage:
    python scripts/bootstrap_vma9.py
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from src.core.vma9 import update_vma9_all
from src.data.dnse_client import fetch_hose_symbols
from src.data.redis_store import RedisStore


async def main() -> None:
    symbols = await fetch_hose_symbols()
    store = await RedisStore.create()
    count = await update_vma9_all(symbols, store)
    await store.close()
    print(f"Bootstrap complete: VMA9 set for {count}/{len(symbols)} symbols")


if __name__ == "__main__":
    asyncio.run(main())

