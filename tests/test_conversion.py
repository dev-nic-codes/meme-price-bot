from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from src.conversion_renderer import (
    ConversionCardData,
    ConversionRenderer,
    format_amount,
    format_percentage,
    percentage_color,
)
from src.telegram_bot import TelegramDashboardBot, calculate_token_amount, parse_conversion_args


class ConversionParsingTests(unittest.TestCase):
    def test_parses_single_word_token_and_amount(self) -> None:
        query, amount = parse_conversion_args("utya 1,250.50")
        self.assertEqual(query, "utya")
        self.assertEqual(amount, Decimal("1250.50"))

    def test_parses_multi_word_token(self) -> None:
        query, amount = parse_conversion_args("resistance dog 25")
        self.assertEqual(query, "resistance dog")
        self.assertEqual(amount, Decimal("25"))

    def test_rejects_missing_or_invalid_amounts(self) -> None:
        for raw in ("", "utya", "utya zero", "utya 0", "utya -1", "utya NaN", "utya Infinity"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_conversion_args(raw)

    def test_enforces_ten_million_gram_maximum(self) -> None:
        _, maximum = parse_conversion_args("utya 10,000,000")
        self.assertEqual(maximum, Decimal("10000000"))
        with self.assertRaisesRegex(ValueError, "too large"):
            parse_conversion_args("utya 10,000,000.01")


class ConversionCalculationTests(unittest.TestCase):
    def test_calculates_token_quantity_from_usd_prices(self) -> None:
        result = calculate_token_amount(Decimal("100"), 1.50, 0.025)
        self.assertEqual(result, Decimal("6000"))

    def test_rejects_missing_market_prices(self) -> None:
        with self.assertRaises(ValueError):
            calculate_token_amount(Decimal("100"), 0, 0.025)
        with self.assertRaises(ValueError):
            calculate_token_amount(Decimal("100"), 1.5, 0)

    def test_amount_formatter_is_readable(self) -> None:
        self.assertEqual(format_amount(Decimal("75000")), "75,000")
        self.assertEqual(format_amount(Decimal("1234.56789")), "1,234.5679")
        self.assertEqual(format_amount(Decimal("0.00123456789")), "0.0012345679")

    def test_percentage_formatter_and_colors(self) -> None:
        self.assertEqual(format_percentage(2.4), "+2.40%")
        self.assertEqual(format_percentage(-1.25), "-1.25%")
        self.assertIsNone(format_percentage(None))
        self.assertEqual(percentage_color(2.4), (46, 218, 145))
        self.assertEqual(percentage_color(-1.25), (255, 85, 105))
        self.assertEqual(percentage_color(0), (184, 193, 211))


class ConversionRendererTests(unittest.TestCase):
    def test_renders_on_the_provided_template(self) -> None:
        renderer = ConversionRenderer()
        image = renderer.render(
            ConversionCardData(
                gram_amount=Decimal("75000"),
                token_symbol="UTYA",
                token_amount=Decimal("4530541.25"),
                gram_price_usd=1.51,
                gram_change_24h=2.4,
            )
        )
        self.assertEqual(image.size, (1280, 800))
        self.assertEqual(image.mode, "RGB")

    def test_renders_gram_to_usd_on_the_same_template(self) -> None:
        renderer = ConversionRenderer()
        image = renderer.render(
            ConversionCardData(
                gram_amount=Decimal("10000000"),
                token_symbol="USD",
                token_amount=Decimal("15123456.789"),
                gram_price_usd=1.512345,
                gram_change_24h=-1.25,
            )
        )
        self.assertEqual(image.size, (1280, 800))
        self.assertEqual(image.mode, "RGB")


class ConversionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_swap_works_in_group_chats(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_conversion = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": -100999, "type": "supergroup"},
                "text": "/swap@memesbot utya 100",
            }
        )

        bot.ensure_subscribed.assert_awaited_once_with(12345, -100999, "supergroup")
        bot.send_conversion.assert_awaited_once_with(-100999, 12345, "utya 100")

    async def test_convert_is_no_longer_a_command(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.admin_ids = set()
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_conversion = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": -100999, "type": "supergroup"},
                "text": "/convert@memesbot utya 100",
            }
        )

        bot.send_conversion.assert_not_awaited()

    async def test_private_conversion_requires_subscription(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=False)
        bot.send_conversion = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": 12345, "type": "private"},
                "text": "/swap utya 100",
            }
        )

        bot.send_conversion.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
