from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot
from src.trending_service import TrendingCoin, TrendingService, TrendingSnapshot


def trending_payload() -> dict:
    return {
        "included": [
            {
                "type": "token",
                "id": "ton_ton-address",
                "attributes": {"address": "ton-address", "name": "Toncoin", "symbol": "TON"},
            },
            {
                "type": "token",
                "id": "ton_utya-address",
                "attributes": {"address": "utya-address", "name": "UTYABASE", "symbol": "UTYA"},
            },
            {
                "type": "token",
                "id": "ton_redo-address",
                "attributes": {"address": "redo-address", "name": "Resistance Dog", "symbol": "REDO"},
            },
            {
                "type": "token",
                "id": "ton_low-address",
                "attributes": {"address": "low-address", "name": "Low Liquidity", "symbol": "LOW"},
            },
        ],
        "data": [
            _pool("ton_ton-address", "1.00", "5000000"),
            _pool("ton_utya-address", "12.345", "500000"),
            _pool("ton_utya-address", "99.00", "900000"),
            _pool("ton_low-address", "20.00", "100"),
            _pool("ton_redo-address", "-4.567", "300000"),
        ],
    }


def _pool(base_id: str, change: str, liquidity: str) -> dict:
    return {
        "type": "pool",
        "attributes": {
            "price_change_percentage": {"h24": change},
            "reserve_in_usd": liquidity,
        },
        "relationships": {"base_token": {"data": {"type": "token", "id": base_id}}},
    }


class TrendingServiceTests(unittest.TestCase):
    def test_filters_non_meme_assets_duplicates_and_low_liquidity(self) -> None:
        coins = TrendingService.parse_payload(trending_payload(), minimum_liquidity_usd=10_000, result_limit=10)

        self.assertEqual([coin.symbol for coin in coins], ["UTYA", "REDO"])
        self.assertEqual(coins[0].change_24h, 12.345)
        self.assertEqual(coins[1].change_24h, -4.567)

    def test_persisted_snapshot_is_reused_for_the_full_refresh_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "trending.json"
            now = datetime.now(timezone.utc)
            cache_path.write_text(
                json.dumps(
                    {
                        "updated_at": now.isoformat(),
                        "coins": [
                            {
                                "token_address": "utya-address",
                                "name": "UTYABASE",
                                "symbol": "UTYA",
                                "change_24h": 4.25,
                                "liquidity_usd": 500000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            provider = AsyncMock()
            service = TrendingService(provider, cache_path, refresh_seconds=86_400)

            self.assertIsNotNone(service.current())
            self.assertFalse(service.refresh_due(now + timedelta(hours=23, minutes=59)))
            self.assertTrue(service.refresh_due(now + timedelta(hours=24, seconds=1)))


class TrendingFormatTests(unittest.TestCase):
    def test_formats_owner_templates_and_escapes_provider_text(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.trending_title = "🔥 <b>Trends</b>"
        bot.trending_message = "[TITLE]\n[LIST]\nUpdated [UPDATED_AT]"
        bot.trending_row = "[RANK]. <b>$[TICKER]</b> — [NAME] — [CHANGE_24H]"
        bot.trending_service = SimpleNamespace(
            minimum_liquidity_usd=0,
            result_limit=10,
            is_blocked=lambda _coin: False,
        )
        snapshot = TrendingSnapshot(
            updated_at=datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc),
            coins=(
                TrendingCoin("one", "Cat & Dog", "cat<dog", 3.456, 100000),
                TrendingCoin("two", "Resistance Dog", "redo", -1.234, 200000),
            ),
        )

        text = bot.format_trending_message(snapshot)

        self.assertIn("🔥 <b>Trends</b>", text)
        self.assertIn(
            '1. <b><a href="https://www.geckoterminal.com/ton/tokens/one">'
            "$CAT&lt;DOG</a></b> — Cat &amp; Dog — <code>+3.46%</code>",
            text,
        )
        self.assertIn(
            '2. <b><a href="https://www.geckoterminal.com/ton/tokens/two">'
            "$REDO</a></b> — Resistance Dog — <code>-1.23%</code>",
            text,
        )
        self.assertIn("20/07/2026 08:30 UTC", text)

    def test_validates_required_placeholders(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.trending_title = "🔥 <b>Trends</b>"
        bot.trending_message = "[TITLE]\n[LIST]"
        bot.trending_row = "[RANK] [TICKER] [CHANGE_24H]"

        self.assertIsNone(bot.validate_message_template("trending_message", "[TITLE]\n[LIST]"))
        self.assertIsNotNone(bot.validate_message_template("trending_message", "[LIST]"))
        self.assertIsNone(bot.validate_message_template("trending_row", "[RANK] [TICKER] [CHANGE_24H]"))
        self.assertIsNotNone(bot.validate_message_template("trending_row", "[RANK] [TICKER]"))

    def test_custom_emoji_entity_is_preserved_with_placeholders(self) -> None:
        rendered = TelegramDashboardBot.rich_text_to_html(
            "🔥 [LIST]",
            [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "123456789"}],
        )

        self.assertIn('<tg-emoji emoji-id="123456789">🔥</tg-emoji>', rendered)
        self.assertIn("[LIST]", rendered)


class TrendingCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_trending_works_in_group_chats(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_trending = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": -100999, "type": "supergroup"},
                "text": "/trending@memepricesbot",
            }
        )

        bot.ensure_subscribed.assert_awaited_once_with(12345, -100999, "supergroup")
        bot.send_trending.assert_awaited_once_with(-100999)

    async def test_private_trending_requires_subscription(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=False)
        bot.send_trending = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": 12345, "type": "private"},
                "text": "/trending",
            }
        )

        bot.send_trending.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
