from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot


class AdminMenuTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171, 6422556848}
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.pending_broadcasts = {}
        bot.subscriber_ids = set()
        bot.broadcast_lock = asyncio.Lock()
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        return bot

    async def test_nookie_can_open_message_settings(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(6422556848, "message_settings"))

        bot.answer_callback.assert_awaited_once()
        bot.edit_message.assert_awaited_once()

    async def test_admin_can_open_inline_message_settings(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(386839171, "inline_message_settings"))

        bot.answer_callback.assert_awaited_once()
        text, markup = bot.edit_message.await_args.args[2:]
        self.assertIn("Inline-Mode Messages", text)
        callbacks = {
            button["callback_data"]
            for row in json.loads(markup)["inline_keyboard"]
            for button in row
        }
        self.assertIn("edit_public_message:inline_coin_message", callbacks)

    async def test_nookie_can_start_trending_title_edit(self) -> None:
        bot = self.make_bot()
        bot.trending_title = "🔥 <b>TON Trends</b>"

        await bot.handle_callback(self.callback(6422556848, "edit_trending_title"))

        self.assertEqual(bot.pending_text_edits[6422556848], "trending_title")
        bot.send_message.assert_awaited_once()

    async def test_nookie_can_open_broadcast_composer(self) -> None:
        bot = self.make_bot()
        bot.subscriber_ids = {111, 222}

        await bot.handle_callback(self.callback(6422556848, "compose_broadcast"))

        self.assertEqual(bot.pending_text_edits[6422556848], "broadcast_message")
        bot.send_message.assert_awaited_once()

    async def test_not_modified_refresh_does_not_send_duplicate_menu(self) -> None:
        bot = self.make_bot()
        bot.api = AsyncMock(
            return_value={"ok": False, "description": "Bad Request: message is not modified"}
        )

        await TelegramDashboardBot.edit_message(bot, 123, 456, "Same text")

        bot.send_message.assert_not_awaited()

    @staticmethod
    def callback(user_id: int, data: str) -> dict:
        return {
            "id": "callback-id",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 77, "chat": {"id": user_id, "type": "private"}},
        }


class PersistentSettingTests(unittest.TestCase):
    def make_bot(self, settings_path: Path) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.settings_path = settings_path
        bot.settings = {}
        bot.channel = "@memeprice"
        bot.required_channel = "@memeprice"
        bot.membership_cache = {123: (True, 999.0)}
        bot.trending_service = SimpleNamespace(result_limit=10, minimum_liquidity_usd=10_000.0)
        bot.background_refresh_seconds = 60
        bot.image_cache_seconds = 30
        return bot

    def test_runtime_values_validate_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            bot = self.make_bot(path)

            stored, _ = bot.apply_value_setting("background_refresh_seconds", "120")
            bot.settings[bot.persisted_key_for_value_edit("background_refresh_seconds")] = stored
            bot.save_settings()

            self.assertEqual(bot.background_refresh_seconds, 120)
            self.assertEqual(json.loads(path.read_text())["background_refresh_seconds"], "120")

    def test_channel_and_trending_controls_change_live_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(Path(directory) / "settings.json")

            bot.apply_value_setting("channel", "@newchannel")
            bot.apply_value_setting("required_channel", "@joinchannel")
            bot.apply_value_setting("trending_result_limit", "15")
            bot.apply_value_setting("trending_min_liquidity_usd", "$25,000")

            self.assertEqual(bot.channel, "@newchannel")
            self.assertEqual(bot.required_channel, "@joinchannel")
            self.assertEqual(bot.membership_cache, {})
            self.assertEqual(bot.trending_service.result_limit, 15)
            self.assertEqual(bot.trending_service.minimum_liquidity_usd, 25_000)

    def test_invalid_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(Path(directory) / "settings.json")

            invalid_values = (
                ("channel", "not a channel"),
                ("required_channel", "-1001234567890"),
                ("trending_result_limit", "50"),
                ("background_refresh_seconds", "5"),
            )
            for key, value in invalid_values:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    bot.apply_value_setting(key, value)


class MenuLayoutTests(unittest.TestCase):
    def test_main_menu_contains_every_settings_section(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        callbacks = {
            button["callback_data"]
            for row in json.loads(bot.menu_markup())["inline_keyboard"]
            for button in row
        }

        self.assertTrue(
            {
                "preview",
                "post",
                "publishing_settings",
                "trending_settings",
                "message_settings",
                "access_settings",
                "system_settings",
                "status",
            }.issubset(callbacks)
        )

    def test_message_types_are_separate_categories(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        message_callbacks = {
            button["callback_data"]
            for row in json.loads(bot.message_settings_markup())["inline_keyboard"]
            for button in row
        }
        trending_callbacks = {
            button["callback_data"]
            for row in json.loads(bot.trending_settings_markup())["inline_keyboard"]
            for button in row
        }

        self.assertTrue(
            {
                "subscription_message_settings",
                "inline_message_settings",
                "trending_message_settings",
                "public_button_settings",
                "broadcast_settings",
            }.issubset(
                message_callbacks
            )
        )
        self.assertNotIn("edit_trending_title", trending_callbacks)
        self.assertNotIn("edit_trending_row", trending_callbacks)


class SubscriberRegistryTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, path: Path) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171, 6422556848}
        bot.subscribers_path = path
        bot.subscriber_ids = set()
        bot.broadcast_lock = asyncio.Lock()
        return bot

    async def test_registry_persists_users_and_excludes_admins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subscribers.json"
            bot = self.make_bot(path)

            bot.remember_subscriber(111)
            bot.remember_subscriber(111)
            bot.remember_subscriber(386839171)

            self.assertEqual(bot.subscriber_ids, {111})
            self.assertEqual(json.loads(path.read_text())["users"], [111])
            reloaded = self.make_bot(path)
            self.assertEqual(reloaded.load_subscribers(), {111})

    async def test_broadcast_preserves_custom_emoji_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(Path(directory) / "subscribers.json")
            bot.pending_text_edits = {386839171: "broadcast_message"}
            bot.pending_broadcasts = {}
            bot.subscriber_ids = {111}
            bot.api = AsyncMock(return_value={"ok": True})
            bot.send_message = AsyncMock()

            await bot.save_pending_broadcast(
                386839171,
                386839171,
                "🔥 Update",
                [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "123456"}],
            )

            self.assertIn('<tg-emoji emoji-id="123456">🔥</tg-emoji>', bot.pending_broadcasts[386839171])
            bot.send_message.assert_awaited_once()

    async def test_broadcast_removes_only_unreachable_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(Path(directory) / "subscribers.json")
            bot.subscriber_ids = {111, 222, 333}

            async def api(_method, payload, timeout=20):
                user_id = int(payload["chat_id"])
                if user_id == 111:
                    return {"ok": True}
                if user_id == 222:
                    return {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"}
                return {"ok": False, "error_code": 400, "description": "Bad Request"}

            bot.api = api
            sent, failed, removed = await bot.broadcast_message("<b>Update</b>")

            self.assertEqual((sent, failed, removed), (1, 2, 1))
            self.assertEqual(bot.subscriber_ids, {111, 333})


if __name__ == "__main__":
    unittest.main()
