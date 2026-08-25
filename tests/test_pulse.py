from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.holder_service import HolderCount
from src.models import CoinValue
from src.pulse_market import (
    PulseMarketObservation,
    PulseMarketSnapshot,
    parse_market_pairs,
    parse_recent_buys,
    parse_recent_largest_buy,
)
from src.pulse_service import (
    DEFAULT_FEED_WINDOW_SECONDS,
    PulseBuy,
    PulseCoinValue,
    PulseEvent,
    PulseResult,
    PulseService,
)
from src.telegram_bot import KNOWN_TOKEN_ADDRESSES, TelegramDashboardBot


NOW = 1_800_000_000.0


def coin(
    ticker: str,
    price: float | None,
    *,
    change_24h: float | None = None,
    market_cap: float | None = None,
    holders: int | None = None,
) -> CoinValue:
    return CoinValue(
        ticker=ticker,
        price=price,
        change_24h=change_24h,
        market_cap=market_cap,
        holders=holders,
    )


class PulseServiceTests(unittest.TestCase):
    def make_service(self, directory: str, **overrides) -> PulseService:
        return PulseService(
            Path(directory) / "pulse_history.json",
            cache_seconds=120,
            minimum_record_interval=30,
            supported_tickers=("UTYA", "REDO", "SCAT"),
            **overrides,
        )

    def test_default_feed_window_is_one_hour(self) -> None:
        self.assertEqual(60 * 60, DEFAULT_FEED_WINDOW_SECONDS)

        with tempfile.TemporaryDirectory() as directory:
            service = PulseService(Path(directory) / "pulse_history.json")

        self.assertEqual(60 * 60, service.feed_window_seconds)

    def test_no_significant_events_returns_normal_market(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0, change_24h=1.0)], now=NOW)
            service.record([coin("UTYA", 1.02, change_24h=1.0)], now=NOW + 300)

            result = service.current(now=NOW + 300)

            self.assertTrue(result.available)
            self.assertEqual((), result.events)

    def test_exact_price_threshold_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0)], now=NOW)
            service.record([coin("UTYA", 1.04)], now=NOW + 300)

            result = service.current(now=NOW + 300)

            self.assertEqual(1, len(result.events))
            self.assertEqual("UTYA", result.events[0].ticker)
            self.assertIn("Price spike: +4.0% in 5M", result.events[0].details)

    def test_related_signals_are_grouped_into_one_token_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [coin("UTYA", 1.0, market_cap=990_000)],
                now=NOW,
            )
            service.record(
                [coin("UTYA", 1.08, market_cap=1_010_000)],
                now=NOW + 300,
            )

            result = service.current(now=NOW + 300)

            self.assertEqual(1, len(result.events))
            self.assertEqual("UTYA", result.events[0].ticker)
            self.assertIn("price", result.events[0].signal_types)
            self.assertIn("market_cap", result.events[0].signal_types)
            self.assertEqual(2, len(result.events[0].details))

    def test_strongest_event_ranks_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0), coin("REDO", 1.0)], now=NOW)
            service.record([coin("UTYA", 1.04), coin("REDO", 1.10)], now=NOW + 300)

            result = service.current(now=NOW + 300)

            self.assertEqual(("REDO", "UTYA"), tuple(event.ticker for event in result.events))
            self.assertIn("Strongest recorded move", result.events[0].details)
            self.assertNotIn("Strongest recorded move", result.events[1].details)

    def test_selected_events_are_displayed_newest_to_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.0,
                        change_m5=10.0,
                        market_observed_at=NOW - 3_600,
                    ),
                    PulseCoinValue(
                        "REDO",
                        price=1.0,
                        change_m5=2.6,
                        market_observed_at=NOW - 120,
                    ),
                    PulseCoinValue(
                        "SCAT",
                        price=1.0,
                        change_m5=3.0,
                        market_observed_at=NOW - 600,
                    ),
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertEqual(
                ("REDO", "SCAT", "UTYA"),
                tuple(event.ticker for event in result.events),
            )
            self.assertIn("Strongest recorded move", result.events[-1].details)

    def test_market_cap_requires_an_actual_crossing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0, market_cap=1_100_000)], now=NOW)
            service.record([coin("UTYA", 1.0, market_cap=1_200_000)], now=NOW + 60)
            self.assertEqual((), service.current(now=NOW + 60).events)

            service.record([coin("UTYA", 1.0, market_cap=4_990_000)], now=NOW + 120)
            service.record([coin("UTYA", 1.0, market_cap=5_010_000)], now=NOW + 180)
            result = service.current(now=NOW + 180)

            self.assertEqual(1, len(result.events))
            self.assertIn("Crossed above $5M market cap", result.events[0].details)

    def test_holder_growth_uses_persisted_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0, holders=1_000)], now=NOW)
            service.record([coin("UTYA", 1.0, holders=1_020)], now=NOW + 3_600)

            result = service.current(now=NOW + 3_600)

            self.assertEqual(1, len(result.events))
            self.assertIn("Holder growth: +20 (+2.0%) in 1H", result.events[0].details)

    def test_live_five_minute_price_spike_works_without_local_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.05,
                        change_m5=5.0,
                        market_observed_at=NOW,
                        chart_url="https://www.geckoterminal.com/ton/pools/EQPool",
                    )
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertIn("Price spike: +5.0% in 5M", result.events[0].details)
            self.assertEqual(
                "https://www.geckoterminal.com/ton/pools/EQPool",
                result.events[0].chart_url,
            )

    def test_gram_allows_only_price_change_signals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PulseService(Path(directory) / "pulse_history.json")
            previous = {
                "price": 1.0,
                "market_cap": 900_000,
                "holders": 1_000,
                "liquidity": 100_000,
                "volume_m5": 200,
                "volume_h1": 20_000,
                "buys_m5": 2,
                "sells_m5": 2,
                "market_observed_at": NOW - 900,
            }
            current = {
                "price": 1.05,
                "change_m5": 5.0,
                "market_cap": 1_100_000,
                "holders": 1_200,
                "liquidity": 100_000,
                "volume_m5": 5_000,
                "volume_h1": 20_000,
                "buys_m5": 20,
                "sells_m5": 2,
                "largest_buy_usd": 3_000,
                "largest_buy_at": NOW - 60,
                "market_observed_at": NOW,
            }
            series = [(NOW - 900, previous), (NOW, current)]

            normal_kinds = {
                signal.kind for signal in service._signals_for_coin("UTYA", current, series)
            }
            gram_kinds = [
                signal.kind for signal in service._signals_for_coin("GRAM", current, series)
            ]

            self.assertIn("GRAM", service.supported_tickers)
            self.assertIn("large_buy", normal_kinds)
            self.assertEqual(["price"], gram_kinds)

    def test_untrusted_chart_url_is_not_carried_into_pulse_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.05,
                        change_m5=5.0,
                        market_observed_at=NOW,
                        chart_url='javascript:alert("unsafe")',
                    )
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertEqual("", result.events[0].chart_url)

    def test_large_recent_buy_is_detected_with_dynamic_liquidity_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.0,
                        liquidity=100_000,
                        volume_h1=20_000,
                        largest_buy_usd=2_300,
                        largest_buy_at=NOW - 120,
                        market_observed_at=NOW,
                    )
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertIn("large_buy", result.events[0].signal_types)
            self.assertIn("Large buy: $2,300", result.events[0].details)
            self.assertEqual(NOW - 120, result.events[0].observed_at)

    def test_volume_spike_compares_five_minutes_with_recent_hourly_pace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.0,
                        liquidity=100_000,
                        volume_m5=5_000,
                        volume_h1=15_000,
                        market_observed_at=NOW - 120,
                    )
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertIn("volume", result.events[0].signal_types)
            self.assertTrue(result.events[0].details[0].startswith("Volume spike: $5K in 5M"))
            self.assertEqual(NOW - 120, result.events[0].observed_at)

    def test_buy_pressure_requires_meaningful_volume_and_imbalance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.0,
                        volume_m5=2_000,
                        buys_m5=18,
                        sells_m5=5,
                        market_observed_at=NOW,
                    )
                ],
                now=NOW,
            )

            result = service.current(now=NOW)

            self.assertIn("buy_pressure", result.events[0].signal_types)
            self.assertIn("Buy pressure: 18 buys vs 5 sells in 5M", result.events[0].details)

    def test_stale_snapshot_is_not_presented_as_current_pulse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, max_snapshot_age=600)
            service.record([coin("UTYA", 1.0)], now=NOW)

            result = service.current(now=NOW + 601)

            self.assertFalse(result.available)

    def test_new_24h_high_requires_a_full_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            for hour in range(24):
                service.record([coin("UTYA", 1.0)], now=NOW + hour * 3_600)
            service.record([coin("UTYA", 1.01)], now=NOW + 24 * 3_600)

            result = service.current(now=NOW + 24 * 3_600)

            self.assertEqual(1, len(result.events))
            self.assertIn("New 24H high", result.events[0].details)

    def test_feed_keeps_a_significant_event_after_live_market_cools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [PulseCoinValue("UTYA", price=1.05, change_m5=5.0, market_observed_at=NOW)],
                now=NOW,
                source_complete=True,
            )
            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.01,
                        change_m5=0.2,
                        market_observed_at=NOW + 1_800,
                    )
                ],
                now=NOW + 1_800,
                source_complete=True,
            )

            result = service.current(now=NOW + 1_800)

            self.assertEqual(1, len(result.events))
            self.assertIn("Price spike: +5.0% in 5M", result.events[0].details)

    def test_feed_deduplicates_trades_and_summarizes_multiple_large_buys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            buys = (
                PulseBuy("trade-1", 2_000, NOW - 300, "pool", "tx-1"),
                PulseBuy("trade-2", 3_000, NOW - 120, "pool", "tx-2"),
            )
            value = PulseCoinValue(
                "UTYA",
                price=1.0,
                liquidity=100_000,
                volume_h1=10_000,
                market_observed_at=NOW,
                recent_buys=buys,
            )
            service.record([value], now=NOW, source_complete=True)
            service.record([value], now=NOW + 60, source_complete=True)

            result = service.current(now=NOW + 60)

            self.assertEqual(2, len(service._recent_buys))
            self.assertIn("2 large buys: $5,000 total · largest $3,000", result.events[0].details)

    def test_background_only_snapshot_does_not_replace_latest_live_market(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record(
                [PulseCoinValue("UTYA", price=1.0, change_m5=5.0, market_observed_at=NOW)],
                now=NOW,
                source_complete=True,
            )
            service.record([coin("UTYA", 1.01)], now=NOW + 60)

            result = service.current(now=NOW + 60)

            self.assertTrue(result.available)
            self.assertEqual(NOW, result.updated_at)
            self.assertIn("Price spike", result.events[0].details[0])

    def test_feed_reports_complete_only_after_an_unbroken_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            for minute in range(121):
                observed_at = NOW + minute * 60
                service.record(
                    [
                        PulseCoinValue(
                            "UTYA",
                            price=1.0,
                            change_m5=0.0,
                            market_observed_at=observed_at,
                        )
                    ],
                    now=observed_at,
                    source_complete=True,
                )

            complete = service.current(now=NOW + 7_200)
            self.assertTrue(complete.coverage_complete)
            self.assertEqual(60, complete.coverage_minutes)

            service.record(
                [
                    PulseCoinValue(
                        "UTYA",
                        price=1.0,
                        change_m5=0.0,
                        market_observed_at=NOW + 7_260,
                    )
                ],
                now=NOW + 7_260,
                source_complete=False,
            )
            partial = service.current(now=NOW + 7_260)
            self.assertFalse(partial.coverage_complete)

    def test_history_is_persisted_and_cached_result_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0)], now=NOW)
            service.record([coin("UTYA", 1.05)], now=NOW + 300)

            first = service.current(now=NOW + 300)
            second = service.current(now=NOW + 300)
            reloaded = self.make_service(directory).current(now=NOW + 300)

            self.assertIs(first, second)
            self.assertEqual(first.events, reloaded.events)

    def test_history_checkpoints_without_rewriting_on_every_minute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pulse_history.json"
            service = self.make_service(directory)
            service.record([coin("UTYA", 1.0)], now=NOW)
            service.record([coin("UTYA", 1.01)], now=NOW + 60)
            first_checkpoint = json.loads(path.read_text(encoding="utf-8"))

            service.record([coin("UTYA", 1.05)], now=NOW + 300)
            second_checkpoint = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(1, len(first_checkpoint["snapshots"]))
            self.assertEqual(3, len(second_checkpoint["snapshots"]))

    def test_missing_or_malformed_coin_data_does_not_break_market_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            self.assertFalse(service.record([coin("UTYA", None)], now=NOW))
            self.assertFalse(service.current(now=NOW).available)

            service.record([coin("UTYA", 1.0), coin("UNKNOWN", 5.0)], now=NOW)
            result = service.current(now=NOW)

            self.assertTrue(result.available)
            self.assertEqual((), result.events)


class PulseTelegramTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_pulse = AsyncMock()
        return bot

    async def test_pulse_command_uses_existing_subscription_gate_and_handler(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(
            {
                "from": {"id": 123},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "/pulse@memepricesbot",
            }
        )

        bot.ensure_subscribed.assert_awaited_once_with(123, -1001, "supergroup")
        bot.send_pulse.assert_awaited_once_with(-1001)

    async def test_background_pulse_refresh_combines_live_dex_activity_and_holder_counts(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pulse_lock = asyncio.Lock()
        observation = PulseMarketObservation(
            ticker="UTYA",
            token_address="EQToken",
            pair_address="EQPool",
            observed_at=NOW,
            price_usd=0.04,
            market_cap_usd=1_000_000,
            liquidity_usd=100_000,
            change_m5=5.0,
            change_h1=8.0,
            change_h6=12.0,
            change_h24=20.0,
            volume_m5_usd=3_000,
            volume_h1_usd=10_000,
            volume_h24_usd=100_000,
            buys_m5=12,
            sells_m5=3,
            buys_h1=50,
            sells_h1=20,
            largest_buy_usd=2_000,
            largest_buy_at=NOW - 60,
        )
        bot.pulse_market_service = MagicMock()
        bot.pulse_market_service.current = AsyncMock(
            return_value=PulseMarketSnapshot(NOW, (observation,))
        )
        bot.holder_service = MagicMock()
        bot.holder_service.fetch_counts = AsyncMock()
        bot.holder_service.cached_counts.return_value = {
            "UTYA": HolderCount("UTYA", "EQToken", 1_234, NOW)
        }
        bot.price_service = MagicMock()
        bot.price_service.cached_prices.return_value = [
            CoinValue("GRAM", price=1.82, change_24h=3.1, market_cap=4_900_000_000)
        ]
        bot.pulse_service = MagicMock()
        bot.pulse_service.current.return_value = PulseResult(NOW, ())
        bot.process_utya_movement_alert = AsyncMock()
        bot.format_pulse_message = MagicMock(return_value="formatted pulse")
        bot.send_message = AsyncMock()
        bot.pulse_unavailable_message = "unavailable"

        await bot.refresh_pulse_cache(force=True)

        values = bot.pulse_service.record.call_args.args[0]
        self.assertEqual(2, len(values))
        self.assertEqual(1_234, values[0].holders)
        self.assertEqual(3_000, values[0].volume_m5)
        self.assertEqual(2_000, values[0].largest_buy_usd)
        self.assertEqual(
            "https://www.geckoterminal.com/ton/pools/EQPool",
            values[0].chart_url,
        )
        gram = values[1]
        self.assertEqual("GRAM", gram.ticker)
        self.assertEqual(1.82, gram.price)
        self.assertEqual(3.1, gram.change_24h)
        self.assertIsNone(gram.market_cap)
        self.assertIsNone(gram.holders)
        self.assertIsNone(gram.volume_m5)
        self.assertIsNone(gram.largest_buy_usd)
        bot.holder_service.cached_counts.assert_called_once_with(
            list(KNOWN_TOKEN_ADDRESSES)
        )
        bot.holder_service.fetch_counts.assert_not_awaited()
        bot.pulse_market_service.current.assert_awaited_once_with(force=True)
        self.assertTrue(bot.pulse_service.record.call_args.kwargs["source_complete"])
        bot.process_utya_movement_alert.assert_awaited_once_with(values)

    async def test_send_pulse_reads_the_shared_feed_without_a_provider_request(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pulse_service = MagicMock()
        bot.pulse_service.current.return_value = PulseResult(NOW, ())
        bot.pulse_market_service = MagicMock()
        bot.pulse_market_service.current = AsyncMock()
        bot.format_pulse_message = MagicMock(return_value="shared feed")
        bot.send_message = AsyncMock()
        bot.pulse_unavailable_message = "unavailable"

        await bot.send_pulse(123)

        bot.pulse_market_service.current.assert_not_awaited()
        bot.send_message.assert_awaited_once_with(123, "shared feed")

    def test_pulse_formatter_renders_events_and_normal_market(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key in (
            "pulse_title",
            "pulse_message",
            "pulse_event",
            "pulse_normal_message",
            "pulse_unavailable_message",
        ):
            setattr(bot, key, bot.PUBLIC_MESSAGE_DEFAULTS[key])

        event_result = PulseResult(
            updated_at=NOW,
            events=(
                PulseEvent(
                    ticker="UTYA",
                    emoji="🔥",
                    score=80.0,
                    details=("+8.4% in 30M", "Strongest recorded move"),
                    signal_types=("price",),
                    chart_url="https://www.geckoterminal.com/ton/pools/EQPool",
                ),
            ),
            summary=("Market breadth: 4 rising · 5 falling · 1 flat",),
            coverage_complete=False,
            coverage_minutes=42,
        )
        event_text = bot.format_pulse_message(event_result)
        normal_text = bot.format_pulse_message(PulseResult(updated_at=NOW, events=()))

        self.assertIn(
            '<b><a href="https://www.geckoterminal.com/ton/pools/EQPool">$UTYA</a></b>',
            event_text,
        )
        self.assertIn("+8.4% in 30M", event_text)
        self.assertIn("just now", event_text)
        self.assertIn("Updated:", event_text)
        self.assertNotIn("Market breadth", event_text)
        self.assertNotIn("Verified continuous data", event_text)
        self.assertNotIn("rebuilding full coverage", event_text)
        self.assertIn("No major movements are dominating", normal_text)

    def test_market_snapshot_is_not_rendered(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key, default in bot.PUBLIC_MESSAGE_DEFAULTS.items():
            setattr(bot, key, default)

        message = bot.format_pulse_message(
            PulseResult(
                updated_at=NOW,
                events=(PulseEvent("UTYA", "🔥", 80, ("Price spike",), ("price",)),),
                summary=("Market breadth: 6 rising · 3 falling",),
                coverage_complete=False,
                coverage_minutes=7,
            )
        )

        self.assertNotIn("Market snapshot", message)
        self.assertNotIn("Market breadth: 6 rising", message)
        self.assertNotIn("Verified continuous data", message)

    def test_pulse_uses_a_different_configured_emoji_for_each_coin(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key in (
            "pulse_title",
            "pulse_message",
            "pulse_event",
            "pulse_normal_message",
            "pulse_unavailable_message",
        ):
            setattr(bot, key, bot.PUBLIC_MESSAGE_DEFAULTS[key])
        bot.pulse_coin_emojis = dict(bot.PULSE_COIN_EMOJI_DEFAULTS)
        bot.pulse_coin_emojis["UTYA"] = "🦆"
        bot.pulse_coin_emojis["REDO"] = "🐕"
        result = PulseResult(
            updated_at=NOW,
            events=(
                PulseEvent(
                    ticker="UTYA",
                    emoji="🐋",
                    score=90,
                    details=("Large buy: $2K",),
                    signal_types=("large_buy",),
                    direction="up",
                ),
                PulseEvent(
                    ticker="REDO",
                    emoji="📉",
                    score=80,
                    details=("Sudden drop: -8.0% in 5M",),
                    signal_types=("price",),
                    direction="down",
                ),
            ),
        )

        message = bot.format_pulse_message(result)

        self.assertIn("🦆 <b>$UTYA</b>", message)
        self.assertIn("🐕 <b>$REDO</b>", message)

    async def test_pulse_is_registered_in_every_command_scope(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.api = AsyncMock(return_value={"ok": True})

        await bot.setup_commands()

        for call in bot.api.await_args_list:
            commands = json.loads(call.args[1]["commands"])
            self.assertIn("pulse", {command["command"] for command in commands})

    async def test_admin_can_open_pulse_message_editor(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.pending_broadcasts = {}
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()

        await bot.handle_callback(
            {
                "id": "callback-id",
                "from": {"id": 386839171},
                "data": "pulse_message_settings",
                "message": {
                    "message_id": 77,
                    "chat": {"id": 386839171, "type": "private"},
                },
            }
        )

        bot.answer_callback.assert_awaited_once()
        text, markup = bot.edit_message.await_args.args[2:]
        self.assertIn("Pulse Messages", text)
        callbacks = {
            button["callback_data"]
            for row in json.loads(markup)["inline_keyboard"]
            for button in row
        }
        self.assertIn("edit_pulse_event", callbacks)
        self.assertNotIn("edit_pulse_market_snapshot", callbacks)
        self.assertIn("preview_pulse_format", callbacks)
        self.assertIn("pulse_emoji_settings", callbacks)
        self.assertIn("pulse_coin_emoji_settings", callbacks)

    async def test_admin_can_select_one_signal_emoji_to_edit(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.pulse_emojis = dict(bot.PULSE_EMOJI_DEFAULTS)
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        callback = {
            "id": "callback-id",
            "from": {"id": 386839171},
            "data": "edit_pulse_emoji:volume",
            "message": {
                "message_id": 77,
                "chat": {"id": 386839171, "type": "private"},
            },
        }

        await bot.handle_callback(callback)

        self.assertEqual("pulse_emoji:volume", bot.pending_text_edits[386839171])
        self.assertIn("Edit Volume spike emoji", bot.send_message.await_args.args[1])

    async def test_admin_can_select_and_save_one_coin_emoji(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.admin_ids = {386839171}
            bot.owner_id = 386839171
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {}
            bot.pending_text_edits = {}
            bot.pulse_coin_emojis = dict(bot.PULSE_COIN_EMOJI_DEFAULTS)
            bot.answer_callback = AsyncMock()
            bot.edit_message = AsyncMock()
            bot.send_message = AsyncMock()
            bot.api = AsyncMock(return_value={"ok": True})

            await bot.handle_callback(
                {
                    "id": "callback-id",
                    "from": {"id": 386839171},
                    "data": "edit_pulse_coin_emoji:UTYA",
                    "message": {
                        "message_id": 77,
                        "chat": {"id": 386839171, "type": "private"},
                    },
                }
            )
            self.assertEqual(
                "pulse_coin_emoji:UTYA",
                bot.pending_text_edits[386839171],
            )

            await bot.save_pending_pulse_emoji_edit(
                386839171,
                386839171,
                "🦆",
                [],
            )

            self.assertEqual("🦆", bot.settings["pulse_coin_emoji:UTYA"])
            self.assertEqual("🦆", bot.pulse_coin_emojis["UTYA"])

    def test_new_and_pulse_editors_explain_custom_emojis_and_offer_previews(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key, default in bot.PUBLIC_MESSAGE_DEFAULTS.items():
            setattr(bot, key, default)

        self.assertIn("Edit coin emojis", bot.pulse_message_settings_text())
        self.assertIn("captured automatically", bot.new_message_settings_text())
        new_callbacks = {
            button["callback_data"]
            for row in json.loads(bot.new_message_settings_markup())["inline_keyboard"]
            for button in row
        }
        self.assertIn("preview_new_format", new_callbacks)

    async def test_pulse_and_new_templates_preserve_custom_emoji_entities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {}
            bot.pending_text_edits = {}
            bot.api = AsyncMock(return_value={"ok": True})
            bot.send_message = AsyncMock()
            for key, default in bot.PUBLIC_MESSAGE_DEFAULTS.items():
                setattr(bot, key, default)

            examples = {
                "pulse_event": "😀 [TICKER]\n[DETAILS]\n[AGE]",
                "new_message": "😀 [LIST]\n[UPDATED_AT]",
            }
            for key, text in examples.items():
                with self.subTest(key=key):
                    bot.pending_text_edits[123] = key
                    await bot.save_pending_text_edit(
                        123,
                        123,
                        text,
                        [
                            {
                                "type": "custom_emoji",
                                "offset": 0,
                                "length": 2,
                                "custom_emoji_id": "6264560244777557925",
                            }
                        ],
                    )
                    self.assertIn(
                        '<tg-emoji emoji-id="6264560244777557925">😀</tg-emoji>',
                        bot.settings[key],
                    )

    async def test_signal_emoji_editor_saves_and_renders_a_custom_emoji(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {}
            bot.pending_text_edits = {123: "pulse_emoji:large_buy"}
            bot.pulse_emojis = dict(bot.PULSE_EMOJI_DEFAULTS)
            bot.api = AsyncMock(return_value={"ok": True})
            bot.send_message = AsyncMock()
            for key in (
                "pulse_title",
                "pulse_message",
                "pulse_event",
                "pulse_normal_message",
                "pulse_unavailable_message",
            ):
                setattr(bot, key, bot.PUBLIC_MESSAGE_DEFAULTS[key])
            bot.pulse_event = "[SIGNAL_EMOJI] <b>$[TICKER]</b>\n[DETAILS] · [AGE]"

            await bot.save_pending_pulse_emoji_edit(
                123,
                123,
                "😀",
                [
                    {
                        "type": "custom_emoji",
                        "offset": 0,
                        "length": 2,
                        "custom_emoji_id": "6264560244777557925",
                    }
                ],
            )

            stored = '<tg-emoji emoji-id="6264560244777557925">😀</tg-emoji>'
            self.assertEqual(stored, bot.settings["pulse_emoji:large_buy"])
            self.assertEqual(stored, bot.pulse_emojis["large_buy"])
            message = bot.format_pulse_message(
                PulseResult(
                    updated_at=NOW,
                    events=(
                        PulseEvent(
                            ticker="UTYA",
                            emoji="🐋",
                            score=90,
                            details=("Large buy: $2K",),
                            signal_types=("large_buy",),
                            direction="up",
                        ),
                    ),
                )
            )
            self.assertIn(stored, message)
            self.assertNotIn("&lt;tg-emoji", message)


class PulseMarketParsingTests(unittest.TestCase):
    def test_pair_parser_selects_strongest_pool_and_carries_live_activity(self) -> None:
        addresses = {"UTYA": "EQToken"}
        payload = [
            self._pair("small", 1_000),
            self._pair("large", 50_000),
        ]

        observations = parse_market_pairs(payload, addresses, observed_at=NOW)

        self.assertEqual(1, len(observations))
        item = observations[0]
        self.assertEqual("large", item.pair_address)
        self.assertEqual(4.5, item.change_m5)
        self.assertEqual(3_000, item.volume_m5_usd)
        self.assertEqual(12, item.buys_m5)
        self.assertEqual(3, item.sells_m5)
        self.assertEqual(
            "https://www.geckoterminal.com/ton/pools/large",
            item.chart_url,
        )

    def test_pair_parser_aggregates_a_second_meaningful_pool(self) -> None:
        addresses = {"UTYA": "EQToken"}
        payload = [
            self._pair("primary", 50_000),
            self._pair("secondary", 10_000),
        ]

        item = parse_market_pairs(payload, addresses, observed_at=NOW)[0]

        self.assertEqual(("primary", "secondary"), item.pool_addresses)
        self.assertEqual(60_000, item.liquidity_usd)
        self.assertEqual(6_000, item.volume_m5_usd)
        self.assertEqual(24, item.buys_m5)

    def test_trade_parser_uses_only_recent_buys(self) -> None:
        def trade(kind: str, amount: float, age: int) -> dict:
            timestamp = datetime.fromtimestamp(NOW - age, timezone.utc).isoformat()
            return {
                "attributes": {
                    "kind": kind,
                    "volume_in_usd": str(amount),
                    "block_timestamp": timestamp,
                }
            }

        amount, timestamp = parse_recent_largest_buy(
            {"data": [trade("sell", 50_000, 60), trade("buy", 2_000, 60), trade("buy", 9_000, 1_900)]},
            now=NOW,
        )

        self.assertEqual(2_000, amount)
        self.assertEqual(NOW - 60, timestamp)

    def test_trade_parser_preserves_ids_for_deduplication(self) -> None:
        timestamp = datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat()
        payload = {
            "data": [
                {
                    "id": "trade-id",
                    "attributes": {
                        "kind": "buy",
                        "volume_in_usd": "2500",
                        "block_timestamp": timestamp,
                        "tx_hash": "tx-hash",
                    },
                }
            ]
        }

        trades = parse_recent_buys(payload, now=NOW, pool_address="pool")

        self.assertEqual(1, len(trades))
        self.assertEqual("trade-id", trades[0].trade_id)
        self.assertEqual("tx-hash", trades[0].tx_hash)
        self.assertEqual("pool", trades[0].pool_address)

    @staticmethod
    def _pair(pair_address: str, liquidity: float) -> dict:
        return {
            "chainId": "ton",
            "pairAddress": pair_address,
            "baseToken": {"address": "EQToken", "name": "UTYA", "symbol": "UTYA"},
            "priceUsd": "0.04",
            "marketCap": 1_000_000,
            "liquidity": {"usd": liquidity},
            "priceChange": {"m5": 4.5, "h1": 8.0, "h6": 12.0, "h24": 20.0},
            "volume": {"m5": 3_000, "h1": 10_000, "h24": 100_000},
            "txns": {
                "m5": {"buys": 12, "sells": 3},
                "h1": {"buys": 50, "sells": 20},
            },
        }


if __name__ == "__main__":
    unittest.main()
