from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .config import OUTPUT_DIR
from .token_report import KNOWN_TOKEN_ADDRESSES
from .tonapi_client import TONAPI_REQUEST_PACER


TONAPI_JETTON_URL = "https://tonapi.io/v2/jettons/{jetton_master}"
HOLDER_CACHE_PATH = OUTPUT_DIR / "holder_counts.json"


@dataclass(frozen=True)
class HolderCount:
    ticker: str
    jetton_master: str
    count: int | None
    updated_at: float | None
    stale: bool = False


class HolderService:
    """Fetch and cache authoritative TON jetton holder counts.

    TonAPI's jetton metadata endpoint exposes ``holders_count`` for a jetton
    master. A stale verified count is safer than inventing a value when the
    provider is temporarily unavailable, so successful values are persisted.
    """

    def __init__(
        self,
        timeout_seconds: int = 12,
        cache_seconds: int | None = None,
        concurrency: int = 3,
    ) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.cache_seconds = max(
            300,
            cache_seconds
            if cache_seconds is not None
            else int(os.getenv("HOLDER_CACHE_SECONDS", "1800")),
        )
        self.concurrency = max(1, min(5, concurrency))
        self.headers = {"User-Agent": "meme-price-dashboard/2.0"}
        token = os.getenv("TONAPI_KEY", "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def fetch_counts(self, tickers: list[str]) -> dict[str, HolderCount]:
        now = time.time()
        cached = self._load_cache()
        requested = [ticker.upper() for ticker in tickers]
        result: dict[str, HolderCount] = {}
        stale_tickers: list[str] = []

        for ticker in requested:
            item = cached.get(ticker)
            if (
                item is not None
                and item.count is not None
                and item.updated_at is not None
                and now - item.updated_at < self.cache_seconds
            ):
                result[ticker] = item
            else:
                stale_tickers.append(ticker)

        if stale_tickers:
            semaphore = asyncio.Semaphore(self.concurrency)
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                headers=self.headers,
            ) as session:
                fetched = await asyncio.gather(
                    *(
                        self._fetch_one(session, semaphore, ticker)
                        for ticker in stale_tickers
                    ),
                    return_exceptions=True,
                )

            for ticker, item in zip(stale_tickers, fetched):
                if isinstance(item, HolderCount) and item.count is not None:
                    result[ticker] = item
                    cached[ticker] = item
                    continue
                previous = cached.get(ticker)
                if previous is not None and previous.count is not None:
                    result[ticker] = HolderCount(
                        ticker=previous.ticker,
                        jetton_master=previous.jetton_master,
                        count=previous.count,
                        updated_at=previous.updated_at,
                        stale=True,
                    )
                else:
                    result[ticker] = HolderCount(
                        ticker=ticker,
                        jetton_master=self._jetton_master(ticker) or "",
                        count=None,
                        updated_at=None,
                        stale=True,
                    )

        if any(item.count is not None for item in cached.values()):
            self._save_cache(cached)
        return result

    def cached_counts(self, tickers: list[str]) -> dict[str, HolderCount]:
        """Return persisted holder counts immediately, marking old values stale."""
        now = time.time()
        cached = self._load_cache()
        result: dict[str, HolderCount] = {}
        for raw_ticker in tickers:
            ticker = raw_ticker.upper()
            item = cached.get(ticker)
            if item is None:
                result[ticker] = HolderCount(
                    ticker=ticker,
                    jetton_master=self._jetton_master(ticker) or "",
                    count=None,
                    updated_at=None,
                    stale=True,
                )
                continue
            result[ticker] = HolderCount(
                ticker=item.ticker,
                jetton_master=item.jetton_master,
                count=item.count,
                updated_at=item.updated_at,
                stale=(
                    item.updated_at is None
                    or now - item.updated_at >= self.cache_seconds
                ),
            )
        return result

    async def _fetch_one(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        ticker: str,
    ) -> HolderCount:
        jetton_master = self._jetton_master(ticker)
        if not jetton_master:
            return HolderCount(ticker, "", None, None, stale=True)

        async with semaphore:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    await TONAPI_REQUEST_PACER.wait()
                    async with session.get(
                        TONAPI_JETTON_URL.format(jetton_master=jetton_master)
                    ) as response:
                        if response.status == 429:
                            retry_after = self._retry_after(response.headers)
                            await asyncio.sleep(max(retry_after, 1 + attempt * 2))
                            continue
                        response.raise_for_status()
                        payload = await response.json()
                    count = self._positive_int(payload.get("holders_count"))
                    return HolderCount(
                        ticker=ticker,
                        jetton_master=jetton_master,
                        count=count,
                        updated_at=time.time() if count is not None else None,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt * 2)
            if last_error is not None:
                print(
                    f"Holder count fetch failed for {ticker}: "
                    f"{type(last_error).__name__}: {last_error}",
                    flush=True,
                )
        return HolderCount(ticker, jetton_master, None, None, stale=True)

    @staticmethod
    def _jetton_master(ticker: str) -> str | None:
        return KNOWN_TOKEN_ADDRESSES.get(ticker.lower())

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _retry_after(headers: Any) -> int:
        try:
            return max(0, int(headers.get("Retry-After") or 0))
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _load_cache(path: Path = HOLDER_CACHE_PATH) -> dict[str, HolderCount]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, HolderCount] = {}
        for ticker, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            key = str(ticker).upper()
            result[key] = HolderCount(
                ticker=key,
                jetton_master=str(raw.get("jetton_master") or ""),
                count=HolderService._positive_int(raw.get("count")),
                updated_at=HolderService._float_or_none(raw.get("updated_at")),
            )
        return result

    @staticmethod
    def _save_cache(
        values: dict[str, HolderCount],
        path: Path = HOLDER_CACHE_PATH,
    ) -> None:
        payload = {
            ticker: {
                "jetton_master": item.jetton_master,
                "count": item.count,
                "updated_at": item.updated_at,
            }
            for ticker, item in values.items()
            if item.count is not None
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            print(
                f"Holder count cache save failed: {type(exc).__name__}: {exc}",
                flush=True,
            )

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
