import asyncio
import logging
import os
from pathlib import Path

import httpx

from src.config.constants import DNSE_OHLCV_URL, VNDIRECT_STOCKS_URL
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_SYMBOLS_HOSE_CACHE_PATH = Path("data/symbols_hose.txt")
_SYMBOLS_HNX_CACHE_PATH = Path("data/symbols_hnx.txt")
_SYMBOLS_UPCOM_CACHE_PATH = Path("data/symbols_upcom.txt")
# backward-compat alias kept for any external references
_SYMBOLS_CACHE_PATH = _SYMBOLS_HOSE_CACHE_PATH
_CAFEF_URL = "https://banggia.cafef.vn/stockhandler.ashx"
_VPS_ALL_STOCKS_URL = "https://bgapidatafeed.vps.com.vn/getlistallstock"


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
                status = exc.response.status_code
                if status == 400 or status == 404:
                    # No data for this symbol — retrying won't help
                    return None
                if status == 429:
                    retry_after = int(exc.response.headers.get("Retry-After", 10))
                    logger.warning("Rate limited (429), sleeping %ds", retry_after)
                    await asyncio.sleep(retry_after)
                else:
                    logger.warning("HTTP %d attempt %d", status, attempt + 1)
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
    """Fetch HoSE stock symbols. Tries CafeF → VPS → local cache."""
    for fetch_fn in (_fetch_symbols_cafef_hose, lambda: _fetch_symbols_vps("HOSE")):
        try:
            symbols = await fetch_fn()
            _save_symbols_to_file(symbols, _SYMBOLS_HOSE_CACHE_PATH)
            return symbols
        except Exception as exc:
            logger.warning("Symbol fetch failed: %s", exc)

    if _SYMBOLS_HOSE_CACHE_PATH.exists():
        logger.warning("Using cached symbols from %s", _SYMBOLS_HOSE_CACHE_PATH)
        return _load_symbols_from_file(_SYMBOLS_HOSE_CACHE_PATH)

    raise RuntimeError(
        "Cannot fetch HoSE symbols: all sources failed and no cache at data/symbols_hose.txt"
    )


async def fetch_hnx_symbols() -> list[str]:
    """Fetch HNX stock symbols. Tries VPS → local cache."""
    try:
        symbols = await _fetch_symbols_vps("HNX")
        _save_symbols_to_file(symbols, _SYMBOLS_HNX_CACHE_PATH)
        return symbols
    except Exception as exc:
        logger.warning("VPS HNX symbol fetch failed: %s", exc)

    if _SYMBOLS_HNX_CACHE_PATH.exists():
        logger.warning("Using cached symbols from %s", _SYMBOLS_HNX_CACHE_PATH)
        return _load_symbols_from_file(_SYMBOLS_HNX_CACHE_PATH)

    raise RuntimeError(
        "Cannot fetch HNX symbols: VPS failed and no cache at data/symbols_hnx.txt"
    )


async def fetch_upcom_symbols() -> list[str]:
    """Fetch UPCOM stock symbols. Tries VPS → local cache."""
    try:
        symbols = await _fetch_symbols_vps("UPCOM")
        _save_symbols_to_file(symbols, _SYMBOLS_UPCOM_CACHE_PATH)
        return symbols
    except Exception as exc:
        logger.warning("VPS UPCOM symbol fetch failed: %s", exc)

    if _SYMBOLS_UPCOM_CACHE_PATH.exists():
        logger.warning("Using cached symbols from %s", _SYMBOLS_UPCOM_CACHE_PATH)
        return _load_symbols_from_file(_SYMBOLS_UPCOM_CACHE_PATH)

    raise RuntimeError(
        "Cannot fetch UPCOM symbols: VPS failed and no cache at data/symbols_upcom.txt"
    )


async def _fetch_symbols_cafef_hose() -> list[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_CAFEF_URL, params={"index": "HOSE"})
        resp.raise_for_status()
        data = resp.json()
        symbols = sorted(item["a"] for item in data if item.get("a"))

    if not symbols:
        raise ValueError("Empty symbol list from CafeF (HOSE)")

    logger.info("Fetched %d HoSE symbols from CafeF", len(symbols))
    return symbols


async def _fetch_symbols_vps(exchange: str) -> list[str]:
    """Fetch stock symbols from VPS broker API, filtered by exchange (HOSE or HNX)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_VPS_ALL_STOCKS_URL, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()

    symbols = sorted(
        item["stock_code"]
        for item in data
        if item.get("post_to") == exchange and item.get("type") == "S" and item.get("stock_code")
    )

    if not symbols:
        raise ValueError(f"Empty symbol list from VPS ({exchange})")

    logger.info("Fetched %d %s symbols from VPS", len(symbols), exchange)
    return symbols


def _load_symbols_from_file(path: Path) -> list[str]:
    symbols = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    logger.info("Loaded %d symbols from %s", len(symbols), path)
    return symbols


def _save_symbols_to_file(symbols: list[str], path: Path) -> None:
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(sorted(symbols)))
    except Exception as exc:
        logger.warning("Could not save symbols cache: %s", exc)
