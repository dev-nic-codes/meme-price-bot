from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot
from src.token_report import (
    TokenAth,
    TokenChoice,
    TokenPair,
    TokenReportResult,
    parse_coingecko_coin_ath,
)


def sample_pair() -> TokenPair:
    return TokenPair(
        token_address="EQBaCgUwOoc6gHCNln_oJzb0mVs79YG7wYoavh-o1ItaneLA",
        pair_address="pool",
        name="Utya",
        symbol="utya",
        dex_id="stonfi",
        url="https://dexscreener.com/ton/pool",
        price_usd=0.04,
        market_cap=40_000_000,
        fdv=40_000_000,
        liquidity_usd=1_000_000,
        price_change={"h24": 1.0},
    )


def bare_bot() -> TelegramDashboardBot:
    bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
    for key, default in bot.PUBLIC_MESSAGE_DEFAULTS.items():
        setattr(bot, key, default)
    bot.public_button_texts = dict(bot.PUBLIC_BUTTON_DEFAULTS)
    bot.public_button_icons = {}
    bot.token_report_semaphore = asyncio.Semaphore(2)
    bot.send_message = AsyncMock()
    bot.edit_message = AsyncMock()
    return bot


class AthCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_ath_is_documented_in_every_command_overview(self) -> None:
        self.assertIn("/ath", TelegramDashboardBot.DEFAULT_WELCOME_MESSAGE)
        self.assertIn("/ath", TelegramDashboardBot.DEFAULT_HELP_MESSAGE)
        self.assertIn("/ath", TelegramDashboardBot.DEFAULT_PRIVATE_HELP_MESSAGE)
        self.assertIn("/ath", TelegramDashboardBot.DEFAULT_GUIDE_COMMANDS_MESSAGE)

    async def test_ath_result_is_short_and_uses_provider_lifetime_high(self) -> None:
        bot = bare_bot()
        pair = sample_pair()
        bot.resolve_token_pair_for_query = AsyncMock(return_value=(pair, None))
        bot.token_report_service = type(
            "Service",
            (),
            {
                "fetch_ath": AsyncMock(
                    return_value=TokenAth(
                        price_usd=0.05984,
                        reached_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
                    )
                )
            },
        )()

        await bot.send_ath(123, "UTYA")

        bot.resolve_token_pair_for_query.assert_awaited_once_with("UTYA")
        bot.send_message.assert_awaited_once_with(
            123,
            "<b>UTYA ATH</b> - <b>$0.05984</b>.",
        )

    async def test_native_ton_aliases_use_coin_ath_and_display_gram(self) -> None:
        native_ath = TokenAth(
            price_usd=8.25,
            reached_at=datetime(2021, 11, 12, tzinfo=timezone.utc),
        )
        for query in ("gram", "TON", "$toncoin", "the open network"):
            with self.subTest(query=query):
                bot = bare_bot()
                bot.resolve_token_pair_for_query = AsyncMock()
                bot.token_report_service = type(
                    "Service",
                    (),
                    {"fetch_coin_ath": AsyncMock(return_value=native_ath)},
                )()

                await bot.send_ath(123, query)

                bot.resolve_token_pair_for_query.assert_not_awaited()
                bot.token_report_service.fetch_coin_ath.assert_awaited_once_with("the-open-network")
                bot.send_message.assert_awaited_once_with(
                    123,
                    "<b>GRAM ATH</b> - <b>$8.25</b>.",
                )

    def test_native_coin_ath_parser_validates_coin_identity(self) -> None:
        payload = {
            "id": "the-open-network",
            "market_data": {
                "ath": {"usd": 8.25},
                "ath_date": {"usd": "2021-11-12T06:50:02.476Z"},
            },
        }

        parsed = parse_coingecko_coin_ath(payload, "the-open-network")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.price_usd, 8.25)
        self.assertIsNone(parse_coingecko_coin_ath(payload, "wrong-coin"))

    async def test_missing_provider_ath_is_not_estimated(self) -> None:
        bot = bare_bot()
        pair = sample_pair()
        bot.resolve_token_pair_for_query = AsyncMock(return_value=(pair, None))
        bot.token_report_service = type(
            "Service",
            (),
            {"fetch_ath": AsyncMock(return_value=None)},
        )()

        await bot.send_ath(123, "utya")

        bot.send_message.assert_awaited_once_with(
            123,
            "<b>UTYA ATH</b> - unavailable.",
        )

    async def test_missing_token_argument_shows_usage_without_api_lookup(self) -> None:
        bot = bare_bot()
        bot.resolve_token_pair_for_query = AsyncMock()

        await bot.send_ath(123, "")

        bot.resolve_token_pair_for_query.assert_not_awaited()
        bot.send_message.assert_awaited_once_with(123, bot.DEFAULT_ATH_USAGE_MESSAGE)

    async def test_ambiguous_symbol_uses_ath_callbacks(self) -> None:
        bot = bare_bot()
        choice = TokenChoice(
            token_address=sample_pair().token_address,
            name="Utya",
            symbol="UTYA",
            liquidity_usd=1_000_000,
            price_usd=0.04,
        )
        bot.resolve_token_pair_for_query = AsyncMock(
            return_value=(None, TokenReportResult(choices=[choice], query="utya"))
        )

        await bot.send_ath(123, "utya")

        _chat_id, _text = bot.send_message.await_args.args
        markup = json.loads(bot.send_message.await_args.kwargs["reply_markup"])
        self.assertEqual(
            markup["inline_keyboard"][0][0]["callback_data"],
            f"ath:{choice.token_address}",
        )


if __name__ == "__main__":
    unittest.main()
