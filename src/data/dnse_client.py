import asyncio
import logging
import os
from pathlib import Path

import httpx

from src.config.constants import DNSE_OHLCV_URL, VNDIRECT_STOCKS_URL
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_SYMBOLS_CACHE_PATH = Path("data/symbols.txt")
_CAFEF_URL = "https://banggia.cafef.vn/stockhandler.ashx"


async def _get(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    params: dict,
    max_retries: int,
) -> dict | None:
    async with semaphore:
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                logger.warning("Timeout %s attempt %d", url, attempt + 1)
            except httpx.HTTPStatusError as exc:
                logger.warning("HTTP %d %s attempt %d", exc.response.status_code, url, attempt + 1)
            except Exception as exc:
                logger.error("Fetch error %s: %s", url, exc)

            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)

    return None


async def fetch_ohlcv_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    from_ts: int,
    to_ts: int,
    resolution: str,
) -> tuple[str, dict | None]:
    settings = get_settings()
    data = await _get(
        client,
        semaphore,
        DNSE_OHLCV_URL,
        {"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts},
        settings.dnse_max_retries,
    )
    return symbol, data


async def fetch_all_ohlcv(
    symbols: list[str],
    from_ts: int,
    to_ts: int,
    resolution: str = "15",
) -> dict[str, dict | None]:
    settings = get_settings()
    semaphore = asyncio.Semaphore(settings.dnse_concurrency)

    async with httpx.AsyncClient(timeout=settings.dnse_timeout) as client:
        results = await asyncio.gather(
            *[fetch_ohlcv_one(client, semaphore, sym, from_ts, to_ts, resolution) for sym in symbols]
        )

    return dict(results)


async def fetch_hose_symbols() -> list[str]:
    """Fetch HoSE stock symbols. Tries CafeF → VNDirect → local cache."""
    for fetch_fn in (_fetch_symbols_cafef, _fetch_symbols_vndirect):
        try:
            symbols = await fetch_fn()
            _save_symbols_to_file(symbols)
            return symbols
        except Exception as exc:
            logger.warning("Symbol fetch %s failed: %s", fetch_fn.__name__, exc)

    if _SYMBOLS_CACHE_PATH.exists():
        logger.warning("Using cached symbols from %s", _SYMBOLS_CACHE_PATH)
        return _load_symbols_from_file(_SYMBOLS_CACHE_PATH)

    raise RuntimeError(
        "Cannot fetch HoSE symbols: all sources failed and no cache at data/symbols.txt"
    )


async def _fetch_symbols_cafef() -> list[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_CAFEF_URL, params={"index": "HOSE"})
        resp.raise_for_status()
        data = resp.json()
        symbols = sorted(item["a"] for item in data if item.get("a"))

    if not symbols:
        raise ValueError("Empty symbol list from CafeF")

    logger.info("Fetched %d HoSE symbols from CafeF", len(symbols))
    return symbols


async def _fetch_symbols_vndirect() -> list[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            VNDIRECT_STOCKS_URL,
            params={"sort": "code", "size": "500", "page": "1", "q": "floor:HOSE~type:STOCK"},
        )
        resp.raise_for_status()
        data = resp.json()
        symbols = sorted(item["code"] for item in data.get("data", []) if item.get("code"))

    if not symbols:
        raise ValueError("Empty symbol list from VNDirect")

    logger.info("Fetched %d HoSE symbols from VNDirect", len(symbols))
    return symbols


def _load_symbols_from_file(path: Path) -> list[str]:
    symbols = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    logger.info("Loaded %d symbols from %s", len(symbols), path)
    return symbols


def _save_symbols_to_file(symbols: list[str]) -> None:
    try:
        _SYMBOLS_CACHE_PATH.parent.mkdir(exist_ok=True)
        _SYMBOLS_CACHE_PATH.write_text("\n".join(sorted(symbols)))
    except Exception as exc:
        logger.warning("Could not save symbols cache: %s", exc)
