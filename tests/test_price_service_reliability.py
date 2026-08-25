from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from src.models import CoinValue
from src.price_service import DISPLAY_TICKERS, PriceService


def cached_display_values() -> dict[str, CoinValue]:
    return {
        ticker: CoinValue(ticker=ticker, price=float(index + 1))
        for index, ticker in enumerate(DISPLAY_TICKERS)
    }


class PriceServiceReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_cached_prices_never_contacts_external_providers(self) -> None:
        service = PriceService()
        coingecko = AsyncMock()
        dex = AsyncMock()
        gecko_pool = AsyncMock()

        with patch.object(service, "_load_cache", return_value=cached_display_values()), patch.object(
            service,
            "_load_price_bot_caches",
            return_value={"UTYA": CoinValue("UTYA", price=99)},
        ), patch.object(
            service, "_fetch_coingecko_markets", coingecko
        ), patch.object(
            service, "_fetch_dex_known_tokens", dex
        ), patch.object(
            service, "_fetch_gecko_pool", gecko_pool
        ):
            values = service.cached_prices()

        self.assertEqual([value.ticker for value in values], DISPLAY_TICKERS)
        self.assertEqual(values[0].price, 99)
        coingecko.assert_not_called()
        dex.assert_not_called()
        gecko_pool.assert_not_called()

    def test_newer_market_values_keep_cached_provider_ath(self) -> None:
        previous = CoinValue("UTYA", price=1, ath_price=5)
        current = CoinValue("UTYA", price=2, change_24h=3, market_cap=4)

        merged = PriceService._merge_value(previous, current)

        self.assertEqual(merged.price, 2)
        self.assertEqual(merged.ath_price, 5)

    async def test_coingecko_429_uses_cache_and_enters_backoff(self) -> None:
        service = PriceService()
        service.external_refresh_seconds = 60
        rate_limit_error = aiohttp.ClientResponseError(
            None,
            (),
            status=429,
            headers={"Retry-After": "120"},
        )
        coingecko = AsyncMock(side_effect=rate_limit_error)
        dex = AsyncMock(return_value=[])

        with patch.object(service, "_load_cache", return_value=cached_display_values()), patch.object(
            service, "_load_price_bot_caches", return_value={}
        ), patch("src.price_service.GECKO_POOLS", {}), patch.object(
            service, "_fetch_coingecko_markets", coingecko
        ), patch.object(
            service, "_fetch_dex_known_tokens", dex
        ), patch.object(
            service, "_save_cache"
        ):
            first = await service.fetch_prices()
            service.last_external_attempt -= service.external_refresh_seconds + 1
            second = await service.fetch_prices()

        self.assertTrue(all(value.price is not None for value in first))
        self.assertTrue(all(value.price is not None for value in second))
        self.assertEqual(coingecko.await_count, 1)
        self.assertEqual(dex.await_count, 2)
        self.assertGreater(service.coingecko_backoff_until, time.monotonic())

    async def test_successful_coingecko_request_resets_backoff(self) -> None:
        service = PriceService()
        service.coingecko_backoff_until = 0.0
        service.coingecko_next_backoff_seconds = 1_200
        coingecko = AsyncMock(return_value=[CoinValue("GRAM", price=1.5)])

        with patch.object(service, "_load_cache", return_value=cached_display_values()), patch.object(
            service, "_load_price_bot_caches", return_value={}
        ), patch("src.price_service.GECKO_POOLS", {}), patch.object(
            service, "_fetch_coingecko_markets", coingecko
        ), patch.object(
            service, "_fetch_dex_known_tokens", AsyncMock(return_value=[])
        ), patch.object(
            service, "_save_cache"
        ):
            await service.fetch_prices()

        self.assertEqual(service.coingecko_backoff_until, 0.0)
        self.assertEqual(
            service.coingecko_next_backoff_seconds,
            service.coingecko_min_backoff_seconds,
        )

    async def test_geckoterminal_429_pauses_all_pool_requests(self) -> None:
        service = PriceService()
        service.external_refresh_seconds = 60
        rate_limit_error = aiohttp.ClientResponseError(
            None,
            (),
            status=429,
            headers={"Retry-After": "120"},
        )
        gecko_pool = AsyncMock(side_effect=rate_limit_error)
        pools = {"ONE": "pool-one", "TWO": "pool-two"}

        with patch.object(service, "_load_cache", return_value=cached_display_values()), patch.object(
            service, "_load_price_bot_caches", return_value={}
        ), patch("src.price_service.GECKO_POOLS", pools), patch.object(
            service, "_fetch_coingecko_markets", AsyncMock(return_value=[])
        ), patch.object(
            service, "_fetch_gecko_pool", gecko_pool
        ), patch.object(
            service, "_fetch_dex_known_tokens", AsyncMock(return_value=[])
        ), patch.object(
            service, "_save_cache"
        ):
            await service.fetch_prices()
            service.last_external_attempt -= service.external_refresh_seconds + 1
            await service.fetch_prices()

        self.assertEqual(gecko_pool.await_count, len(pools))
        self.assertGreater(service.geckoterminal_backoff_until, time.monotonic())


if __name__ == "__main__":
    unittest.main()
