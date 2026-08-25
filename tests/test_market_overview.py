from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.market_overview import (
    MarketOverviewStateStore,
    format_interval,
    parse_interval_minutes,
)
from src.models import CoinValue
from src.telegram_bot import TelegramDashboardBot


class MarketOverviewHelpersTests(unittest.TestCase):
    def test_interval_parser_supports_minutes_hours_and_days(self) -> None:
        self.assertEqual(30, parse_interval_minutes("30m"))
        self.assertEqual(120, parse_interval_minutes("2h"))
        self.assertEqual(1_440, parse_interval_minutes("1 day"))
        self.assertEqual("1d 2h 5m", format_interval(1_565))
        with self.assertRaises(ValueError):
            parse_interval_minutes("2m")

    def test_state_is_persisted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = MarketOverviewStateStore(path)
            store.state["pending"] = {"id": "proposal"}
            store.save()

            loaded = MarketOverviewStateStore(path)

        self.assertEqual("proposal", loaded.state["pending"]["id"])


class MarketOverviewFormattingTests(unittest.TestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key in (
            "market_overview_message",
            "market_overview_price_row",
            "market_overview_cap_row",
        ):
            setattr(bot, key, bot.PUBLIC_MESSAGE_DEFAULTS[key])
        return bot

    def test_overview_contains_all_nine_linked_coins(self) -> None:
        bot = self.make_bot()
        values = [
            CoinValue(
                ticker="UTYA",
                price=0.04665,
                change_24h=4.5,
                market_cap=46_650_000,
            ),
            CoinValue(
                ticker="REDO",
                price=0.1142,
                change_24h=-2.25,
                market_cap=11_420_000,
            ),
        ]

        message = bot.format_market_overview(values)

        self.assertEqual(2, message.count("$UTYA"))
        self.assertEqual(2, message.count("$GRM"))
        self.assertIn('href="https://t.me/utyaprices"', message)
        self.assertIn('href="https://t.me/GRM_prices"', message)
        self.assertIn("$0.04665", message)
        self.assertIn("$46.65M", message)
        self.assertIn("(+4.50%)", message)
        self.assertIn("(-2.25%)", message)
        self.assertNotIn("·", message)
        self.assertNotIn("<b>(+4.50%)</b>", message)
        self.assertIn("$REDO", message)
        self.assertIn("—", message)

    def test_custom_templates_are_used(self) -> None:
        bot = self.make_bot()
        bot.market_overview_message = "Prices\n[PRICES]\nCaps\n[MARKET_CAPS]"
        bot.market_overview_price_row = "[TICKER]=[PRICE] ([CHANNEL_URL])"
        bot.market_overview_cap_row = "[TICKER]=[MARKET_CAP] [CHANGE_24H]"

        message = bot.format_market_overview(
            [CoinValue(ticker="UTYA", price=1.25, market_cap=1_250_000)]
        )

        self.assertIn("UTYA=$1.25", message)
        self.assertIn("UTYA=$1.25M", message)


class MarketOverviewApprovalTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, directory: str) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {100, 200}
        bot.owner_id = 100
        bot.market_overview_channel = "@memeprice"
        bot.market_overview_interval_minutes = 60
        bot.market_overview_lock = asyncio.Lock()
        bot.market_overview_store = MarketOverviewStateStore(Path(directory) / "overview.json")
        bot.answer_callback = AsyncMock()
        bot.send_message_checked = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
        bot.edit_message = AsyncMock()
        bot.delete_message_safely = AsyncMock()
        return bot

    async def test_two_admin_approvals_post_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_overview_store.state["pending"] = {
                "id": "abc123",
                "text": "overview",
                "channel": "@destination",
                "created_at": 1,
                "admin_messages": {"100": 11, "200": 22},
            }
            bot.market_overview_store.save()

            await asyncio.gather(
                bot.handle_market_overview_action(
                    user_id=100,
                    callback_id="first",
                    action="approve",
                    proposal_id="abc123",
                    chat_id=100,
                    message_id=11,
                ),
                bot.handle_market_overview_action(
                    user_id=200,
                    callback_id="second",
                    action="approve",
                    proposal_id="abc123",
                    chat_id=200,
                    message_id=22,
                ),
            )

            self.assertEqual(1, bot.send_message_checked.await_count)
            self.assertIsNone(bot.market_overview_store.state["pending"])
            self.assertEqual(100, bot.market_overview_store.state["last_approved_by"])
            self.assertEqual(2, bot.delete_message_safely.await_count)
            bot.edit_message.assert_not_awaited()

    async def test_proposal_uses_live_price_service_and_notifies_every_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            for key in (
                "market_overview_message",
                "market_overview_price_row",
                "market_overview_cap_row",
            ):
                setattr(bot, key, bot.PUBLIC_MESSAGE_DEFAULTS[key])
            bot.price_service = MagicMock()
            bot.price_service.fetch_prices = AsyncMock(
                return_value=[CoinValue(ticker="UTYA", price=0.05, market_cap=50_000_000)]
            )
            bot.send_market_overview_control = AsyncMock(side_effect=[11, 22])

            created = await bot.create_market_overview_proposal()

            pending = bot.market_overview_store.state["pending"]
            self.assertTrue(created)
            self.assertEqual(2, bot.send_market_overview_control.await_count)
            self.assertEqual({"100": [11], "200": [22]}, pending["admin_messages"])
            self.assertEqual("@memeprice", pending["channel"])
            self.assertIn("$UTYA", pending["text"])

    async def test_skip_deletes_every_resent_preview_for_both_admins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_overview_store.state["pending"] = {
                "id": "abc123",
                "text": "overview",
                "channel": "@destination",
                "created_at": 1,
                "admin_messages": {"100": [11, 12], "200": 22},
            }
            bot.market_overview_store.save()

            await bot.handle_market_overview_action(
                user_id=100,
                callback_id="skip",
                action="skip",
                proposal_id="abc123",
                chat_id=100,
                message_id=12,
            )

            self.assertIsNone(bot.market_overview_store.state["pending"])
            self.assertEqual(3, bot.delete_message_safely.await_count)
            deleted = {call.args for call in bot.delete_message_safely.await_args_list}
            self.assertEqual({(100, 11), (100, 12), (200, 22)}, deleted)

    async def test_resending_preview_keeps_older_copy_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_overview_store.state["pending"] = {
                "id": "abc123",
                "text": "overview",
                "channel": "@destination",
                "created_at": 1,
                "admin_messages": {"100": 11},
            }
            bot.market_overview_store.save()
            bot.send_market_overview_control = AsyncMock(return_value=33)

            resent = await bot.resend_pending_market_overview(100)

            self.assertTrue(resent)
            self.assertEqual(
                [11, 33],
                bot.market_overview_store.state["pending"]["admin_messages"]["100"],
            )

    async def test_unauthorized_callback_cannot_approve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.pending_text_edits = {}
            bot.pending_broadcasts = {}

            await bot.handle_callback(
                {
                    "id": "blocked",
                    "from": {"id": 999},
                    "data": "overview:approve:abc123",
                    "message": {
                        "message_id": 8,
                        "chat": {"id": 999, "type": "private"},
                    },
                }
            )

            bot.answer_callback.assert_awaited_once_with("blocked", "Not authorized.")
            bot.send_message_checked.assert_not_awaited()

    def test_menu_exposes_overview_controls_and_editors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_overview_enabled = True

            main_callbacks = {
                button["callback_data"]
                for row in json.loads(bot.menu_markup())["inline_keyboard"]
                for button in row
            }
            overview_callbacks = {
                button["callback_data"]
                for row in json.loads(bot.market_overview_settings_markup())["inline_keyboard"]
                for button in row
            }

        self.assertIn("market_overview_settings", main_callbacks)
        self.assertIn("generate_market_overview", overview_callbacks)
        self.assertIn("edit_market_overview_channel", overview_callbacks)
        self.assertIn("edit_market_overview_interval", overview_callbacks)
        self.assertIn("edit_market_overview_message", overview_callbacks)


if __name__ == "__main__":
    unittest.main()
