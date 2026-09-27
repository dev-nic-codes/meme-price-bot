from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import aiohttp

from .config import OUTPUT_DIR
from .models import CoinValue
from .token_report import DEX_TOKEN_PAIRS_URL, KNOWN_TOKEN_ADDRESSES, pair_quality_score, parse_pair


COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
GECKO_POOL_URL = "https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool}"

COINGECKO_COINS = {
    "utya": "UTYA",
    "resistance-dog": "REDO",
    "hot-cherry": "CHERRY",
    "make-ton-great-again": "MTONGA",
    "groyper-3": "GROYP",
    "gramming": "GRAMMING",
    "grm": "GRM",
    "the-open-network": "GRAM",
}

GECKO_POOLS = {
    "SCAT": "EQAIJOBF6evx5DgOE2DygfwDzRbzK5sdiWzdp1duWCjtgAuP",
    "YODA": "EQBjBklMBO8hh8cFSeyVNB0GEimYOO9IZ6WDLXjuL45Dsbxu",
    "CHERRY": "EQB-2nOKHN3EHTd82iASHdSbW3QIrjGGPdPYgCtX0okWsd_2",
}

PRICE_CACHE_PATH = OUTPUT_DIR / "price_cache.json"
DISPLAY_TICKERS = [
    "UTYA",
    "REDO",
    "SCAT",
    "YODA",
    "CHERRY",
    "BCHERRY",
    "MTONGA",
    "GROYP",
    "GRAMMING",
    "GRM",
    "GRAM",
]
PRICE_BOT_CACHE_DIRS = {
    "UTYA": "utya-price-bot",
    "REDO": "redo-price-bot",
    "SCAT": "scat-price-bot",
    "YODA": "yoda-price-bot",
    "CHERRY": "cherry-price-bot",
    "MTONGA": "mtonga-price-bot",
    "GROYP": "groyp-price-bot",
    "GRAMMING": "gramming-price-bot",
    "GRM": "grm-price-bot",
}


class PriceService:
    """Fetches live prices for the dashboard.

    Later, this is the only layer that needs changes if you decide to move all
    coins to DexScreener, CoinGecko Pro, or direct pool contracts.
    """

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.headers = {"User-Agent": "meme-price-dashboard/1.0"}
        self.price_bot_root = Path(os.getenv("PRICE_BOT_ROOT", "/srv/projects"))
        self.price_bot_cache_max_age_seconds = max(
            60,
            int(os.getenv("PRICE_BOT_CACHE_MAX_AGE_SECONDS", "900")),
        )
        self.external_refresh_seconds = max(60, int(os.getenv("EXTERNAL_PRICE_REFRESH_SECONDS", "300")))
        self.last_external_attempt = 0.0
        self.coingecko_min_backoff_seconds = max(
            60,
            int(os.getenv("COINGECKO_RATE_LIMIT_BACKOFF_SECONDS", "300")),
        )
        self.coingecko_max_backoff_seconds = max(
            self.coingecko_min_backoff_seconds,
            int(os.getenv("COINGECKO_RATE_LIMIT_MAX_BACKOFF_SECONDS", "3600")),
        )
        self.coingecko_backoff_until = 0.0
        self.coingecko_next_backoff_seconds = self.coingecko_min_backoff_seconds
        self.geckoterminal_min_backoff_seconds = max(
            60,
            int(os.getenv("GECKOTERMINAL_RATE_LIMIT_BACKOFF_SECONDS", "300")),
        )
        self.geckoterminal_max_backoff_seconds = max(
            self.geckoterminal_min_backoff_seconds,
            int(os.getenv("GECKOTERMINAL_RATE_LIMIT_MAX_BACKOFF_SECONDS", "3600")),
        )
        self.geckoterminal_backoff_until = 0.0
        self.geckoterminal_next_backoff_seconds = self.geckoterminal_min_backoff_seconds

    async def fetch_prices(self) -> list[CoinValue]:
        cached_values = self._load_cache()
        primary_pool_values = self._load_price_bot_caches(cached_values)
        values: dict[str, CoinValue] = dict(cached_values)
        values.update(primary_pool_values)

        now = time.monotonic()
        has_all_display_prices = all(
            values.get(ticker) is not None and values[ticker].price is not None
            for ticker in DISPLAY_TICKERS
        )
        should_fetch_external = not has_all_display_prices or now - self.last_external_attempt >= self.external_refresh_seconds
        if should_fetch_external:
            self.last_external_attempt = now
            source_tasks: list[tuple[str, Any]] = []
            if now >= self.coingecko_backoff_until:
                source_tasks.append(("CoinGecko", self._fetch_coingecko_markets()))
            if now >= self.geckoterminal_backoff_until:
                source_tasks.extend(
                    (f"GeckoTerminal {ticker}", self._fetch_gecko_pool(ticker, pool))
                    for ticker, pool in GECKO_POOLS.items()
                )
            source_tasks.append(("DexScreener", self._fetch_dex_known_tokens()))
            results = await asyncio.gather(
                *(task for _source, task in source_tasks),
                return_exceptions=True,
            )

            geckoterminal_attempted = any(
                source.startswith("GeckoTerminal ") for source, _task in source_tasks
            )
            geckoterminal_failed = False
            for (source, _task), result in zip(source_tasks, results):
                if isinstance(result, Exception):
                    if source.startswith("GeckoTerminal "):
                        geckoterminal_failed = True
                    self._handle_source_failure(source, result, now)
                    continue
                if source == "CoinGecko":
                    self.coingecko_backoff_until = 0.0
                    self.coingecko_next_backoff_seconds = self.coingecko_min_backoff_seconds
                if isinstance(result, list):
                    for value in result:
                        ticker = value.ticker.upper()
                        values[ticker] = self._merge_value(values.get(ticker), value)
                elif isinstance(result, CoinValue):
                    ticker = result.ticker.upper()
                    values[ticker] = self._merge_value(values.get(ticker), result)
            if geckoterminal_attempted and not geckoterminal_failed:
                self.geckoterminal_backoff_until = 0.0
                self.geckoterminal_next_backoff_seconds = self.geckoterminal_min_backoff_seconds

        # Price-channel bots track a deliberately configured primary pool. Keep
        # that pool authoritative for price/change while external providers
        # enrich market cap and ATH or act as fallback when its cache is stale.
        for ticker, primary in primary_pool_values.items():
            values[ticker] = self._prefer_primary_pool(values.get(ticker), primary)

        for ticker in DISPLAY_TICKERS:
            current = values.get(ticker)
            if current is not None and not self._valid_value(current):
                values.pop(ticker, None)

        for ticker, cached in cached_values.items():
            current = values.get(ticker)
            if current is None or current.price is None:
                values[ticker] = cached

        ordered = [values.get(ticker, CoinValue(ticker)) for ticker in DISPLAY_TICKERS]
        if any(value.price is not None for value in ordered):
            self._save_cache(ordered)
        return ordered

    def cached_prices(self) -> list[CoinValue]:
        """Return the best persisted snapshot without waiting on a provider."""
        cached_values = self._load_cache()
        values: dict[str, CoinValue] = dict(cached_values)
        values.update(self._load_price_bot_caches(cached_values))
        for ticker in DISPLAY_TICKERS:
            current = values.get(ticker)
            if current is not None and not self._valid_value(current):
                values.pop(ticker, None)
        return [values.get(ticker, CoinValue(ticker)) for ticker in DISPLAY_TICKERS]

    def _handle_source_failure(self, source: str, error: Exception, now: float) -> None:
        if (
            source == "CoinGecko"
            and isinstance(error, aiohttp.ClientResponseError)
            and int(error.status or 0) == 429
        ):
            retry_after = 0
            try:
                retry_after = int((error.headers or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            wait_seconds = min(
                self.coingecko_max_backoff_seconds,
                max(self.coingecko_next_backoff_seconds, retry_after),
            )
            self.coingecko_backoff_until = now + wait_seconds
            self.coingecko_next_backoff_seconds = min(
                self.coingecko_max_backoff_seconds,
                max(self.coingecko_min_backoff_seconds, wait_seconds * 2),
            )
            print(
                "CoinGecko rate limited; keeping the last valid values and "
                f"retrying in {wait_seconds}s",
                flush=True,
            )
            return
        if (
            source.startswith("GeckoTerminal ")
            and isinstance(error, aiohttp.ClientResponseError)
            and int(error.status or 0) == 429
        ):
            # The pool requests share one provider quota. One 429 pauses the
            # complete provider, while DexScreener and local caches continue.
            if now < self.geckoterminal_backoff_until:
                return
            retry_after = 0
            try:
                retry_after = int((error.headers or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            wait_seconds = min(
                self.geckoterminal_max_backoff_seconds,
                max(self.geckoterminal_next_backoff_seconds, retry_after),
            )
            self.geckoterminal_backoff_until = now + wait_seconds
            self.geckoterminal_next_backoff_seconds = min(
                self.geckoterminal_max_backoff_seconds,
                max(self.geckoterminal_min_backoff_seconds, wait_seconds * 2),
            )
            print(
                "GeckoTerminal rate limited; keeping the last valid values and "
                f"retrying in {wait_seconds}s",
                flush=True,
            )
            return
        print(f"{source} price source failed: {type(error).__name__}: {error}", flush=True)

    def _load_price_bot_caches(self, cached_values: dict[str, CoinValue]) -> dict[str, CoinValue]:
        values: dict[str, CoinValue] = {}
        for ticker, dirname in PRICE_BOT_CACHE_DIRS.items():
            cache_path = self.price_bot_root / dirname / "price_cache.json"
            try:
                cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
                if cache_age > self.price_bot_cache_max_age_seconds:
                    continue
                payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            price = self._float_or_none(payload.get("price_usd"))
            if price is None:
                continue
            previous = cached_values.get(ticker, CoinValue(ticker))
            values[ticker] = CoinValue(
                ticker=ticker,
                price=price,
                change_24h=self._float_or_none(payload.get("change_24h_percent")),
                market_cap=previous.market_cap,
                ath_price=previous.ath_price,
            )
        return values

    async def _fetch_coingecko_markets(self) -> list[CoinValue]:
        params = {
            "vs_currency": "usd",
            "ids": ",".join(COINGECKO_COINS),
            "price_change_percentage": "24h",
            "precision": "full",
        }
        async with aiohttp.ClientSession(timeout=self.timeout, headers=self.headers) as session:
            async with session.get(COINGECKO_MARKETS_URL, params=params) as response:
                response.raise_for_status()
                payload = await response.json()

        values: list[CoinValue] = []
        if not isinstance(payload, list):
            return values

        for coin in payload:
            if not isinstance(coin, dict):
                continue
            ticker = COINGECKO_COINS.get(str(coin.get("id") or ""))
            if not ticker:
                continue
            values.append(
                CoinValue(
                    ticker=ticker,
                    price=self._float_or_none(coin.get("current_price")),
                    change_24h=self._float_or_none(
                        coin.get("price_change_percentage_24h_in_currency")
                        if coin.get("price_change_percentage_24h_in_currency") is not None
                        else coin.get("price_change_percentage_24h")
                    ),
                    market_cap=self._float_or_none(coin.get("market_cap") or coin.get("fully_diluted_valuation")),
                    ath_price=self._float_or_none(coin.get("ath")),
                )
            )
        return values

    async def _fetch_gecko_pool(self, ticker: str, pool: str) -> CoinValue:
        url = GECKO_POOL_URL.format(pool=pool)
        async with aiohttp.ClientSession(timeout=self.timeout, headers=self.headers) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                payload = await response.json()

        attributes: dict[str, Any] = payload.get("data", {}).get("attributes", {}) if isinstance(payload, dict) else {}
        changes = attributes.get("price_change_percentage") or {}
        return CoinValue(
            ticker=ticker,
            price=self._float_or_none(attributes.get("base_token_price_usd")),
            change_24h=self._float_or_none(changes.get("h24") if isinstance(changes, dict) else None),
            market_cap=self._float_or_none(attributes.get("market_cap_usd") or attributes.get("fdv_usd")),
        )

    async def _fetch_dex_known_tokens(self) -> list[CoinValue]:
        """Use the exact token-pair source shared with `/meme <token>`.

        This keeps the dashboard cards and individual token reports aligned for
        the dashboard meme coins. CoinGecko/GeckoTerminal remain fallback sources.
        """
        async with aiohttp.ClientSession(timeout=self.timeout, headers=self.headers) as session:
            results = await asyncio.gather(
                *(
                    self._fetch_dex_token(session, ticker.upper(), address)
                    for ticker, address in KNOWN_TOKEN_ADDRESSES.items()
                ),
                return_exceptions=True,
            )

        values: list[CoinValue] = []
        for result in results:
            if isinstance(result, Exception):
                print(f"Dex token source failed: {type(result).__name__}: {result}", flush=True)
                continue
            if result is not None:
                values.append(result)
        return values

    async def _fetch_dex_token(self, session: aiohttp.ClientSession, ticker: str, address: str) -> CoinValue | None:
        async with session.get(DEX_TOKEN_PAIRS_URL.format(address=address)) as response:
            response.raise_for_status()
            payload = await response.json()
        if not isinstance(payload, list):
            return None
        pairs = [
            pair
            for item in payload
            if isinstance(item, dict)
            and (pair := parse_pair(item, expected_token_address=address)) is not None
            and pair.price_usd is not None
            and pair.price_usd > 0
        ]
        if not pairs:
            return None
        best = sorted(pairs, key=pair_quality_score, reverse=True)[0]
        return CoinValue(
            ticker=ticker,
            price=best.price_usd,
            change_24h=best.price_change.get("h24"),
            market_cap=best.market_cap if best.market_cap is not None else best.fdv,
        )

    @staticmethod
    def _merge_value(previous: CoinValue | None, current: CoinValue) -> CoinValue:
        if previous is None or current.ath_price is not None:
            return current
        return replace(current, ath_price=previous.ath_price)

    @staticmethod
    def _prefer_primary_pool(external: CoinValue | None, primary: CoinValue) -> CoinValue:
        if external is None:
            return primary
        return replace(
            external,
            price=primary.price if primary.price is not None else external.price,
            change_24h=(
                primary.change_24h
                if primary.change_24h is not None
                else external.change_24h
            ),
        )

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_value(value: CoinValue) -> bool:
        if value.price is None or value.price <= 0:
            return False
        if value.market_cap is not None and value.market_cap <= 0:
            return False
        if value.change_24h is not None and abs(value.change_24h) > 10_000:
            return False
        return True

    @staticmethod
    def _load_cache() -> dict[str, CoinValue]:
        try:
            payload = json.loads(PRICE_CACHE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        values: dict[str, CoinValue] = {}
        if not isinstance(payload, list):
            return values
        for item in payload:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").upper()
            if not ticker:
                continue
            values[ticker] = CoinValue(
                ticker=ticker,
                price=PriceService._float_or_none(item.get("price")),
                change_24h=PriceService._float_or_none(item.get("change_24h")),
                market_cap=PriceService._float_or_none(item.get("market_cap")),
                ath_price=PriceService._float_or_none(item.get("ath_price")),
            )
        return values

    @staticmethod
    def _save_cache(values: list[CoinValue]) -> None:
        try:
            PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = [asdict(value) for value in values if value.price is not None]
            PRICE_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Price cache save failed: {type(exc).__name__}: {exc}", flush=True)


def example_values() -> list[CoinValue]:
    return [
        CoinValue("UTYA", price=0.025, change_24h=38.15, market_cap=25_160_000),
        CoinValue("REDO", price=0.18, change_24h=-2.18, market_cap=18_400_000),
        CoinValue("SCAT", price=0.0013, change_24h=18.88, market_cap=130_891),
        CoinValue("YODA", price=0.0016, change_24h=65.12, market_cap=1_591_645),
        CoinValue("CHERRY", price=0.0000149, change_24h=47.51, market_cap=1_490_490),
        CoinValue("BCHERRY", price=0.0002827, change_24h=-4.17, market_cap=28_273),
        CoinValue("MTONGA", price=0.0047, change_24h=0.5, market_cap=501_015),
        CoinValue("GROYP", price=0.02684, change_24h=0.13, market_cap=1_342_191),
        CoinValue("GRAMMING", price=0.0002546, change_24h=-15.01, market_cap=254_650),
        CoinValue("GRM", price=0.001046, change_24h=-1.56, market_cap=2_573_843),
        CoinValue("GRAM", price=1.82, change_24h=3.1, market_cap=4_900_000_000),
    ]
