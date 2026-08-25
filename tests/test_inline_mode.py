from __future__ import annotations

import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

from src.holder_service import HolderCount
from src.inline_mode import (
    FEATURED_INLINE_SYMBOLS,
    INLINE_LOGO_URLS,
    MP_LOGO_URL,
    InlineCoin,
    InlineMessageTemplates,
    build_inline_results,
    help_result,
    parse_inline_conversion,
)
from src.models import CoinValue
from src.telegram_bot import TelegramDashboardBot


def sample_coins() -> tuple[InlineCoin, ...]:
    return (
        InlineCoin(
            symbol="GRAM",
            name="Gram",
            price_usd=2.0,
            change_24h=3.25,
            market_cap=5_000_000_000,
            ath_price=8.25,
            holders=None,
            logo_url=INLINE_LOGO_URLS["GRAM"],
            native=True,
        ),
        InlineCoin(
            symbol="UTYA",
            name="Utya",
            price_usd=0.02,
            change_24h=8.5,
            market_cap=20_000_000,
            ath_price=0.05984,
            holders=9_386,
            logo_url=INLINE_LOGO_URLS["UTYA"],
        ),
        InlineCoin(
            symbol="REDO",
            name="Resistance Dog",
            price_usd=0.2,
            change_24h=-4.25,
            market_cap=20_000_000,
            ath_price=1.4,
            holders=27_063,
            logo_url=INLINE_LOGO_URLS["REDO"],
        ),
        InlineCoin(
            symbol="SCAT",
            name="Scared Cats",
            price_usd=0.001,
            change_24h=4.0,
            market_cap=1_000_000,
            ath_price=0.01,
            holders=1_001,
            logo_url=INLINE_LOGO_URLS["SCAT"],
        ),
        InlineCoin(
            symbol="YODA",
            name="Baby Yoda",
            price_usd=0.002,
            change_24h=-3.0,
            market_cap=2_000_000,
            ath_price=0.01,
            holders=2_002,
            logo_url=INLINE_LOGO_URLS["YODA"],
        ),
        InlineCoin(
            symbol="CHERRY",
            name="Hot Cherry",
            price_usd=0.00001,
            change_24h=2.0,
            market_cap=3_000_000,
            ath_price=0.00002768,
            holders=3_003,
            logo_url=INLINE_LOGO_URLS["CHERRY"],
        ),
        InlineCoin(
            symbol="BCHERRY",
            name="black cherry",
            price_usd=0.00028,
            change_24h=-4.0,
            market_cap=28_000,
            ath_price=None,
            holders=404,
            logo_url=INLINE_LOGO_URLS["BCHERRY"],
        ),
        InlineCoin(
            symbol="MTONGA",
            name="Make TON Great Again",
            price_usd=0.004,
            change_24h=-1.0,
            market_cap=4_000_000,
            ath_price=0.01,
            holders=4_004,
            logo_url=INLINE_LOGO_URLS["MTONGA"],
        ),
        InlineCoin(
            symbol="GROYP",
            name="Groyper",
            price_usd=0.05,
            change_24h=1.5,
            market_cap=2_500_000,
            ath_price=0.05066,
            holders=1_603,
            logo_url=INLINE_LOGO_URLS["GROYP"],
        ),
        InlineCoin(
            symbol="GRAMMING",
            name="gramming",
            price_usd=0.0002,
            change_24h=0.5,
            market_cap=5_000_000,
            ath_price=0.0003844,
            holders=5_005,
            logo_url=INLINE_LOGO_URLS["GRAMMING"],
        ),
        InlineCoin(
            symbol="GRM",
            name="Grm",
            price_usd=0.001,
            change_24h=-0.5,
            market_cap=6_000_000,
            ath_price=0.0015,
            holders=6_006,
            logo_url=INLINE_LOGO_URLS["GRM"],
        ),
    )


class InlineModeResultTests(unittest.TestCase):
    def test_empty_query_returns_every_supported_coin_with_gram_first(self) -> None:
        results = build_inline_results("", sample_coins())
        self.assertEqual(
            [str(result["id"]) for result in results],
            [
                "coin:gram",
                "coin:utya",
                "coin:redo",
                "coin:scat",
                "coin:yoda",
                "coin:cherry",
                "coin:bcherry",
                "coin:mtonga",
                "coin:groyp",
                "coin:gramming",
                "coin:grm",
            ],
        )
        self.assertEqual(
            FEATURED_INLINE_SYMBOLS,
            (
                "GRAM",
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
            ),
        )

    def test_coin_results_show_relevant_stats_and_official_logo_thumbnail(self) -> None:
        results = build_inline_results("", sample_coins())
        gram_message = results[0]["input_message_content"]["message_text"]
        utya_message = results[1]["input_message_content"]["message_text"]

        self.assertIn("Price:", gram_message)
        self.assertIn("24h:", gram_message)
        self.assertIn("ATH: <b>$8.25</b>", gram_message)
        self.assertNotIn("Holders:", gram_message)
        self.assertNotIn("Market cap:", gram_message)
        self.assertNotIn("Holders", str(results[0]["description"]))
        self.assertNotIn("MCAP", str(results[0]["description"]))
        self.assertIn("ATH $8.25", str(results[0]["description"]))
        self.assertIn("Holders: <b>9,386</b>", utya_message)
        self.assertIn("Market cap: <b>$20M</b>", utya_message)
        self.assertIn("ATH: <b>$0.05984</b>", utya_message)
        self.assertIn("Holders 9,386", str(results[1]["description"]))
        self.assertIn("MCAP $20M", str(results[1]["description"]))
        self.assertEqual(results[0]["thumbnail_url"], INLINE_LOGO_URLS["GRAM"])
        self.assertEqual(results[1]["thumbnail_url"], INLINE_LOGO_URLS["UTYA"])
        self.assertEqual(results[2]["thumbnail_url"], INLINE_LOGO_URLS["REDO"])
        bcherry = next(result for result in results if result["id"] == "coin:bcherry")
        self.assertNotIn("ATH", str(bcherry["input_message_content"]["message_text"]))
        self.assertNotIn("ATH", str(bcherry["description"]))
        for result, symbol in zip(results, FEATURED_INLINE_SYMBOLS):
            with self.subTest(symbol=symbol):
                self.assertEqual(result["thumbnail_url"], INLINE_LOGO_URLS[symbol])
                self.assertTrue(str(result["thumbnail_url"]).startswith("https://"))

    def test_change_direction_is_clear_in_shared_message(self) -> None:
        results = build_inline_results("", sample_coins())
        positive = results[1]["input_message_content"]["message_text"]
        negative = results[2]["input_message_content"]["message_text"]
        self.assertIn("🟢 ▲ +8.50%", positive)
        self.assertIn("🔴 ▼ -4.25%", negative)

    def test_coin_query_filters_to_one_result(self) -> None:
        result = build_inline_results("resistance dog", sample_coins())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "coin:redo")

        groyp = build_inline_results("groyp", sample_coins())
        self.assertEqual(len(groyp), 1)
        self.assertEqual(groyp[0]["id"], "coin:groyp")

        for query, symbol in {
            "scared cats": "scat",
            "baby yoda": "yoda",
            "hot cherry": "cherry",
            "black cherry": "bcherry",
            "bcherry": "bcherry",
            "make ton great again": "mtonga",
            "gramming": "gramming",
            "grm": "grm",
        }.items():
            with self.subTest(query=query):
                result = build_inline_results(query, sample_coins())
                self.assertEqual(result[0]["id"], f"coin:{symbol}")

    def test_gram_to_utya_conversion_uses_cross_usd_rates(self) -> None:
        result = build_inline_results("100 gram to utya", sample_coins())[0]
        message = result["input_message_content"]["message_text"]
        self.assertIn("<code>100 GRAM</code> = <code>10,000 UTYA</code>", message)
        self.assertEqual(result["thumbnail_url"], INLINE_LOGO_URLS["UTYA"])

    def test_utya_to_redo_conversion_works_in_reverse_direction(self) -> None:
        result = build_inline_results("1000 utya to redo", sample_coins())[0]
        message = result["input_message_content"]["message_text"]
        self.assertIn("<code>1,000 UTYA</code> = <code>100 REDO</code>", message)

    def test_utya_to_gram_conversion_works(self) -> None:
        result = build_inline_results("1000 utya to gram", sample_coins())[0]
        message = result["input_message_content"]["message_text"]
        self.assertIn("<code>1,000 UTYA</code> = <code>10 GRAM</code>", message)

    def test_usd_is_supported_in_every_requested_direction(self) -> None:
        expected = {
            "5 usd to utya": "<code>5 USD</code> = <code>250 UTYA</code>",
            "5 utya to usd": "<code>5 UTYA</code> = <code>0.1 USD</code>",
            "5 usd to gram": "<code>5 USD</code> = <code>2.5 GRAM</code>",
            "5 gram to usd": "<code>5 GRAM</code> = <code>10 USD</code>",
        }
        for query, rendered in expected.items():
            with self.subTest(query=query):
                result = build_inline_results(query, sample_coins())[0]
                message = result["input_message_content"]["message_text"]
                self.assertIn(rendered, message)

    def test_complete_usd_gram_and_token_conversion_matrix(self) -> None:
        directions = [("USD", "GRAM"), ("GRAM", "USD")]
        for token in (
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
        ):
            directions.extend(
                [
                    ("GRAM", token),
                    (token, "GRAM"),
                    ("USD", token),
                    (token, "USD"),
                ]
            )

        self.assertEqual(len(directions), 42)
        for source, target in directions:
            with self.subTest(source=source, target=target):
                result = build_inline_results(
                    f"5 {source} to {target}",
                    sample_coins(),
                )[0]
                message = str(result["input_message_content"]["message_text"])
                self.assertTrue(str(result["id"]).startswith("convert:"))
                self.assertIn(f"<code>5 {source}</code> = <code>", message)
                self.assertIn(f" {target}</code>", message)

    def test_groyp_is_supported_by_converter(self) -> None:
        result = build_inline_results("10 groyp to usd", sample_coins())[0]
        message = result["input_message_content"]["message_text"]
        self.assertIn("<code>10 GROYP</code> = <code>0.5 USD</code>", message)

    def test_ton_alias_maps_to_gram(self) -> None:
        conversion = parse_inline_conversion("5 ton to redo")
        self.assertEqual(
            conversion,
            type(conversion)(amount=Decimal("5"), source="GRAM", target="REDO"),
        )
        result = build_inline_results("TON", sample_coins())
        self.assertEqual(result[0]["id"], "coin:gram")
        ton_to_usd = build_inline_results("5 ton to usd", sample_coins())[0]
        message = ton_to_usd["input_message_content"]["message_text"]
        self.assertIn("<code>5 GRAM</code> = <code>10 USD</code>", message)

    def test_converter_has_no_live_estimate_wording(self) -> None:
        result = build_inline_results("5 gram to usd", sample_coins())[0]
        message = result["input_message_content"]["message_text"]
        rendered = " ".join((str(result["title"]), str(result["description"]), str(message))).casefold()
        self.assertNotIn("estimate", rendered)
        self.assertNotIn("live conversion", rendered)

    def test_default_converter_omits_rate_and_via(self) -> None:
        result = build_inline_results("5 gram to usd", sample_coins())[0]
        message = result["input_message_content"]["message_text"]

        self.assertNotIn("Rate:", message)
        self.assertNotIn("via", message.casefold())
        self.assertEqual("Convert GRAM to USD", result["description"])

    def test_malformed_or_unsupported_conversion_returns_help_result(self) -> None:
        malformed = build_inline_results("100 gram to", sample_coins())[0]
        unsupported = build_inline_results("100 dogs to gram", sample_coins())[0]
        self.assertTrue(str(malformed["id"]).startswith("help:"))
        self.assertTrue(str(unsupported["id"]).startswith("help:"))
        self.assertIn("Conversions support", unsupported["description"])

    def test_result_ids_stay_unique_and_within_telegram_limit(self) -> None:
        results = build_inline_results("", sample_coins())
        ids = [str(result["id"]) for result in results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(1 <= len(result_id.encode("utf-8")) <= 64 for result_id in ids))

    def test_every_selected_inline_message_uses_its_custom_template(self) -> None:
        templates = InlineMessageTemplates(
            coin="Coin [TOKEN_SYMBOL] | [PRICE] | [CHANGE_24H] | [ATH_PRICE] | [HOLDERS] | [MARKET_CAP] | [TOKEN_NAME]",
            conversion="Swap [SOURCE_AMOUNT] [SOURCE_SYMBOL] → [TARGET_AMOUNT] [TARGET_SYMBOL] | [UNIT_RATE]",
            help="Custom help: [NOTE]",
        )

        coin = build_inline_results("utya", sample_coins(), templates)[0]
        conversion = build_inline_results("100 gram to utya", sample_coins(), templates)[0]
        help_message = build_inline_results("unknown", sample_coins(), templates)[0]

        self.assertEqual(
            coin["input_message_content"]["message_text"],
            "Coin UTYA | $0.02 | 🟢 ▲ +8.50% | $0.05984 | 9,386 | $20M | Utya",
        )
        self.assertEqual(
            conversion["input_message_content"]["message_text"],
            "Swap 100 GRAM → 10,000 UTYA | 100",
        )
        self.assertEqual(
            help_message["input_message_content"]["message_text"],
            "Custom help: Search a supported coin or enter a conversion.",
        )

    def test_native_gram_omits_holder_and_market_cap_lines_from_custom_template(self) -> None:
        templates = InlineMessageTemplates(
            coin=(
                "Coin [TOKEN_NAME] ([TOKEN_SYMBOL])\n"
                "Price [PRICE]\n"
                "Change [CHANGE_24H]\n"
                "ATH [ATH_PRICE]\n"
                "Holders [HOLDERS]\n"
                "MCAP [MARKET_CAP]\n"
                "Custom footer"
            ),
        )

        gram = build_inline_results("gram", sample_coins(), templates)[0]
        utya = build_inline_results("utya", sample_coins(), templates)[0]
        gram_message = str(gram["input_message_content"]["message_text"])
        utya_message = str(utya["input_message_content"]["message_text"])

        self.assertEqual(
            gram_message,
            "Coin Gram (GRAM)\nPrice $2\nChange 🟢 ▲ +3.25%\nATH $8.25\nCustom footer",
        )
        self.assertIn("Holders 9,386", utya_message)
        self.assertIn("MCAP $20M", utya_message)

    def test_native_gram_preserves_other_fields_in_one_line_custom_template(self) -> None:
        templates = InlineMessageTemplates(
            coin=(
                "Coin [TOKEN_SYMBOL] | Price [PRICE] | Change [CHANGE_24H] | "
                "ATH [ATH_PRICE] | "
                "Holders [HOLDERS] | MCAP [MARKET_CAP]"
            ),
        )

        gram = build_inline_results("gram", sample_coins(), templates)[0]
        gram_message = str(gram["input_message_content"]["message_text"])

        self.assertEqual(
            gram_message,
            "Coin GRAM | Price $2 | Change 🟢 ▲ +3.25% | ATH $8.25",
        )

    def test_bcherry_omits_ath_from_custom_inline_template(self) -> None:
        templates = InlineMessageTemplates(
            coin=(
                "Coin [TOKEN_SYMBOL] | Price [PRICE] | Change [CHANGE_24H] | "
                "ATH <b>[ATH_PRICE]</b> | Holders [HOLDERS] | MCAP [MARKET_CAP]"
            ),
        )

        bcherry = build_inline_results("bcherry", sample_coins(), templates)[0]
        utya = build_inline_results("utya", sample_coins(), templates)[0]
        bcherry_message = str(bcherry["input_message_content"]["message_text"])
        utya_message = str(utya["input_message_content"]["message_text"])

        self.assertEqual(
            bcherry_message,
            "Coin BCHERRY | Price $0.00028 | Change 🔴 ▼ -4.00% | "
            "Holders 404 | MCAP $28K",
        )
        self.assertNotIn("ATH", str(bcherry["description"]))
        self.assertIn("ATH <b>$0.05984</b>", utya_message)

    def test_generic_results_use_mp_logo_instead_of_gram(self) -> None:
        generic_items = {
            "direct help": help_result("Try again"),
            "numeric unknown search": build_inline_results("123", sample_coins())[0],
            "malformed conversion": build_inline_results(
                "100 gram to",
                sample_coins(),
            )[0],
            "USD-only conversion": build_inline_results(
                "5 usd to usd",
                sample_coins(),
            )[0],
            "unavailable prices": build_inline_results("", ())[0],
        }

        self.assertEqual(
            MP_LOGO_URL,
            "https://raw.githubusercontent.com/dev-nic-codes/meme-price-bot/"
            "main/assets/mp-logo-square-v2.png",
        )
        for result_type, result in generic_items.items():
            with self.subTest(result_type=result_type):
                self.assertEqual(result["thumbnail_url"], MP_LOGO_URL)
                self.assertEqual(result["thumbnail_width"], 96)
                self.assertEqual(result["thumbnail_height"], 96)
                result_id = str(result["id"])
                self.assertTrue(
                    result_id.startswith("help:mp5:")
                    or result_id.startswith("convert:mp5:")
                )
        self.assertNotEqual(MP_LOGO_URL, INLINE_LOGO_URLS["GRAM"])

    def test_custom_templates_escape_live_values_but_preserve_template_html(self) -> None:
        unsafe_coin = InlineCoin(
            symbol="BAD&",
            name="<Unsafe>",
            price_usd=1,
            change_24h=0,
            market_cap=1,
            ath_price=2,
            holders=1,
            logo_url="",
        )
        templates = InlineMessageTemplates(
            coin="<b>[TOKEN_NAME]</b> [TOKEN_SYMBOL] [PRICE] [CHANGE_24H] [ATH_PRICE] [HOLDERS] [MARKET_CAP]",
        )

        result = build_inline_results("bad&", (unsafe_coin,), templates)[0]
        message = result["input_message_content"]["message_text"]

        self.assertIn("<b>&lt;Unsafe&gt;</b>", message)
        self.assertIn("BAD&amp;", message)
        self.assertNotIn("<Unsafe>", message)


class InlineModeTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_updates_are_routed(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.handle_inline_query = AsyncMock()
        inline_query = {"id": "query-1", "query": ""}
        await bot.handle_update({"inline_query": inline_query})
        bot.handle_inline_query.assert_awaited_once_with(inline_query)

    async def test_handler_answers_with_built_results(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.inline_query_semaphore = asyncio.Semaphore(1)
        bot.inline_cached_coin_snapshot = Mock(return_value=sample_coins())
        bot.inline_coin_snapshot = AsyncMock(side_effect=asyncio.TimeoutError)
        bot.answer_inline_query = AsyncMock()
        bot.inline_coin_message = "Custom [TOKEN_SYMBOL] [TOKEN_NAME] [PRICE] [CHANGE_24H] [ATH_PRICE] [HOLDERS] [MARKET_CAP]"
        bot.inline_conversion_message = "[SOURCE_AMOUNT] [SOURCE_SYMBOL] [TARGET_AMOUNT] [TARGET_SYMBOL] [UNIT_RATE]"
        bot.inline_help_message = "Help: [NOTE]"

        await bot.handle_inline_query({"id": "query-2", "query": ""})

        query_id, results = bot.answer_inline_query.await_args.args
        self.assertEqual(query_id, "query-2")
        self.assertEqual(
            [result["id"] for result in results],
            [f"coin:{symbol.casefold()}" for symbol in FEATURED_INLINE_SYMBOLS],
        )
        self.assertTrue(
            all(
                str(result["input_message_content"]["message_text"]).startswith("Custom ")
                for result in results
            )
        )
        bot.inline_coin_snapshot.assert_not_awaited()

    async def test_answer_inline_query_uses_short_shared_cache(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.api = AsyncMock(return_value={"ok": True})
        results = build_inline_results("", sample_coins())

        await bot.answer_inline_query("query-3", results)

        method, payload = bot.api.await_args.args
        self.assertEqual(method, "answerInlineQuery")
        self.assertEqual(payload["inline_query_id"], "query-3")
        self.assertEqual(payload["cache_time"], "15")
        self.assertEqual(payload["is_personal"], "false")
        self.assertEqual(
            json.loads(payload["button"]),
            {
                "text": "What can this bot do?",
                "start_parameter": "inline_start",
            },
        )
        self.assertEqual(json.loads(payload["results"])[0]["id"], "coin:gram")

    async def test_snapshot_reuses_price_and_holder_services(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.inline_snapshot_cache = None
        bot.inline_snapshot_cache_seconds = 20
        bot.inline_snapshot_lock = asyncio.Lock()
        bot.price_service = unittest.mock.Mock()
        values = [
            CoinValue(
                coin.symbol,
                price=coin.price_usd,
                change_24h=coin.change_24h,
                market_cap=coin.market_cap,
                ath_price=coin.ath_price,
            )
            for coin in sample_coins()
        ]
        bot.price_service.fetch_prices = AsyncMock(
            return_value=values
        )
        bot.holder_service = unittest.mock.Mock()
        holder_counts = {
            coin.symbol: HolderCount(
                coin.symbol,
                f"{coin.symbol.casefold()}-address",
                coin.holders,
                1.0,
            )
            for coin in sample_coins()
            if not coin.native
        }
        bot.holder_service.fetch_counts = AsyncMock(
            return_value=holder_counts
        )

        first = await bot.inline_coin_snapshot()
        second = await bot.inline_coin_snapshot()

        self.assertEqual([coin.symbol for coin in first], list(FEATURED_INLINE_SYMBOLS))
        self.assertTrue(first[0].native)
        self.assertEqual(first[1].holders, 9_386)
        self.assertEqual(next(coin for coin in first if coin.symbol == "GROYP").holders, 1_603)
        self.assertEqual(first[1].ath_price, 0.05984)
        self.assertIs(first, second)
        bot.price_service.fetch_prices.assert_awaited_once()
        bot.holder_service.fetch_counts.assert_awaited_once_with(
            [symbol for symbol in FEATURED_INLINE_SYMBOLS if symbol != "GRAM"]
        )

    def test_persisted_snapshot_builds_without_network_calls(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.inline_snapshot_cache = None
        bot.inline_snapshot_cache_seconds = 20
        bot.price_service = Mock()
        bot.holder_service = Mock()
        bot.price_service.cached_prices.return_value = [
            CoinValue(
                coin.symbol,
                price=coin.price_usd,
                change_24h=coin.change_24h,
                market_cap=coin.market_cap,
                ath_price=coin.ath_price,
            )
            for coin in sample_coins()
        ]
        bot.holder_service.cached_counts.return_value = {
            coin.symbol: HolderCount(
                coin.symbol,
                f"{coin.symbol.casefold()}-address",
                coin.holders,
                1.0,
            )
            for coin in sample_coins()
            if not coin.native
        }

        snapshot = bot.inline_cached_coin_snapshot()

        self.assertEqual([coin.symbol for coin in snapshot], list(FEATURED_INLINE_SYMBOLS))
        self.assertTrue(all(coin.price_usd is not None for coin in snapshot))
        bot.price_service.fetch_prices.assert_not_called()
        bot.holder_service.fetch_counts.assert_not_called()

    def test_polling_requests_inline_updates(self) -> None:
        self.assertIn("inline_query", TelegramDashboardBot.ALLOWED_UPDATES)


if __name__ == "__main__":
    unittest.main()
