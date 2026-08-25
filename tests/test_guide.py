from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot


class GuideCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.pending_alert_inputs = {}
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_message = AsyncMock()
        bot.welcome_message = "Welcome to Meme Prices."
        return bot

    async def test_private_guide_reuses_subscription_check_and_opens_home(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(self.message(111, "private", "/guide"))

        bot.ensure_subscribed.assert_awaited_once_with(111, 111, "private")
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], 111)
        self.assertIn("TON Meme Coin Guide", args[1])
        callbacks = self.callback_values(kwargs["reply_markup"])
        self.assertIn("guide:111:start", callbacks)
        self.assertIn("guide:111:inline", callbacks)
        self.assertIn("guide:111:live_channels", callbacks)
        self.assertIn("guide:111:close", callbacks)

    async def test_private_guide_stays_closed_when_subscription_fails(self) -> None:
        bot = self.make_bot()
        bot.ensure_subscribed.return_value = False

        await bot.handle_message(self.message(111, "private", "/guide"))

        bot.ensure_subscribed.assert_awaited_once_with(111, 111, "private")
        bot.send_message.assert_not_awaited()

    async def test_group_guide_sends_only_private_handoff(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(self.message(111, "supergroup", "/guide", chat_id=-100123))

        bot.ensure_subscribed.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], -100123)
        self.assertIn("only in private messages", args[1])
        self.assertNotIn("Choose a topic below", args[1])
        markup = json.loads(kwargs["reply_markup"])
        self.assertEqual(
            markup["inline_keyboard"][0][0]["url"],
            "https://t.me/memepricesbot?start=guide",
        )

    async def test_private_start_deep_link_opens_guide(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(self.message(111, "private", "/start guide"))

        bot.ensure_subscribed.assert_awaited_once_with(111, 111, "private")
        bot.send_message.assert_awaited_once()
        self.assertIn("TON Meme Coin Guide", bot.send_message.await_args.args[1])

    async def test_inline_header_deep_link_uses_normal_start_welcome(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(self.message(111, "private", "/start inline_start"))

        bot.ensure_subscribed.assert_awaited_once_with(111, 111, "private")
        bot.send_message.assert_awaited_once_with(111, bot.welcome_message)

    async def test_private_help_includes_guide_but_group_help_does_not(self) -> None:
        bot = self.make_bot()

        await bot.handle_message(self.message(111, "private", "/help"))
        private_help = bot.send_message.await_args.args[1]
        bot.send_message.reset_mock()
        await bot.handle_message(self.message(111, "group", "/help", chat_id=-55))
        group_help = bot.send_message.await_args.args[1]

        self.assertIn("/guide", private_help)
        self.assertNotIn("/guide", group_help)
        for help_text in (private_help, group_help):
            self.assertIn("Inline mode", help_text)
            self.assertIn("@memepricesbot", help_text)
            self.assertIn("100 USD to GRAM", help_text)

    @staticmethod
    def message(user_id: int, chat_type: str, text: str, *, chat_id: int | None = None) -> dict:
        return {
            "from": {"id": user_id},
            "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
            "text": text,
        }

    @staticmethod
    def callback_values(raw_markup: str) -> set[str]:
        return {
            button["callback_data"]
            for row in json.loads(raw_markup)["inline_keyboard"]
            for button in row
            if "callback_data" in button
        }


class GuideCallbackTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.require_private_subscription = True
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.answer_callback = AsyncMock()
        bot.edit_guide_message = AsyncMock()
        bot.delete_guide_message = AsyncMock()
        bot.send_trending = AsyncMock()
        return bot

    async def test_section_and_back_navigation_edit_the_same_message(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "private", "guide:111:research"))
        bot.answer_callback.assert_awaited_once_with("callback-id")
        bot.edit_guide_message.assert_awaited_once()
        args = bot.edit_guide_message.await_args.args
        self.assertEqual(args[:2], (111, 77))
        self.assertIn("Researching a Token", args[2])

        bot.answer_callback.reset_mock()
        bot.edit_guide_message.reset_mock()
        await bot.handle_callback(self.callback(111, "private", "guide:111:home"))
        self.assertIn("TON Meme Coin Guide", bot.edit_guide_message.await_args.args[2])

    async def test_non_private_callback_is_acknowledged_and_rejected(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "supergroup", "guide:111:start", chat_id=-1001))

        bot.answer_callback.assert_awaited_once_with(
            "callback-id",
            "The guide works only in private messages.",
        )
        bot.ensure_subscribed.assert_not_awaited()
        bot.edit_guide_message.assert_not_awaited()

    async def test_another_user_cannot_control_the_guide(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(222, "private", "guide:111:start", chat_id=222))

        bot.answer_callback.assert_awaited_once_with(
            "callback-id",
            "Open your own guide with /guide.",
        )
        bot.edit_guide_message.assert_not_awaited()

    async def test_callback_rechecks_subscription(self) -> None:
        bot = self.make_bot()
        bot.ensure_subscribed.return_value = False

        await bot.handle_callback(self.callback(111, "private", "guide:111:start"))

        bot.answer_callback.assert_awaited_once_with("callback-id")
        bot.ensure_subscribed.assert_awaited_once_with(111, 111, "private")
        bot.edit_guide_message.assert_not_awaited()

    async def test_close_deletes_the_guide_message(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "private", "guide:111:close"))

        bot.answer_callback.assert_awaited_once_with("callback-id")
        bot.delete_guide_message.assert_awaited_once_with(111, 77)
        bot.edit_guide_message.assert_not_awaited()

    async def test_trending_button_reuses_existing_trending_function(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "private", "guide:111:trending"))

        bot.send_trending.assert_awaited_once_with(111)
        bot.edit_guide_message.assert_not_awaited()

    async def test_live_channels_page_uses_folder_link_and_existing_message(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "private", "guide:111:live_channels"))

        bot.answer_callback.assert_awaited_once_with("callback-id")
        bot.edit_guide_message.assert_awaited_once()
        chat_id, message_id, text, raw_markup = bot.edit_guide_message.await_args.args
        self.assertEqual((chat_id, message_id), (111, 77))
        self.assertIn("Live Price Channels", text)
        markup = json.loads(raw_markup)
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertIn(
            "https://t.me/addlist/njrPpQOLzgAzM2U1",
            {button.get("url") for button in buttons},
        )
        callbacks = {button.get("callback_data") for button in buttons}
        self.assertIn("guide:111:home", callbacks)
        self.assertIn("guide:111:close", callbacks)

    async def test_inline_page_documents_every_inline_function(self) -> None:
        bot = self.make_bot()

        await bot.handle_callback(self.callback(111, "private", "guide:111:inline"))

        bot.answer_callback.assert_awaited_once_with("callback-id")
        bot.edit_guide_message.assert_awaited_once()
        chat_id, message_id, text, raw_markup = bot.edit_guide_message.await_args.args
        self.assertEqual((chat_id, message_id), (111, 77))
        self.assertIn("Inline Mode", text)
        self.assertIn("even when the bot is not a member", text)
        self.assertIn("USD price", text)
        self.assertIn("ATH", text)
        self.assertIn("holder count", text)
        self.assertIn("market cap", text)
        self.assertIn("BCHERRY", text)
        self.assertIn("100 USD to GRAM", text)
        self.assertIn("1000 GRM to UTYA", text)
        self.assertIn("TON or TONCOIN", text)
        callbacks = {
            button["callback_data"]
            for row in json.loads(raw_markup)["inline_keyboard"]
            for button in row
            if "callback_data" in button
        }
        self.assertIn("guide:111:converter", callbacks)
        self.assertIn("guide:111:home", callbacks)
        self.assertIn("guide:111:close", callbacks)

    async def test_expired_message_api_errors_do_not_raise_or_send_replacements(self) -> None:
        bot = self.make_bot()
        bot.api = AsyncMock(
            side_effect=[
                {"ok": False, "description": "Bad Request: message to edit not found"},
                {"ok": False, "description": "Bad Request: message to delete not found"},
            ]
        )

        await TelegramDashboardBot.edit_guide_message(bot, 111, 77, "Text", "{}")
        await TelegramDashboardBot.delete_guide_message(bot, 111, 77)

        self.assertEqual(bot.api.await_count, 2)

    @staticmethod
    def callback(user_id: int, chat_type: str, data: str, *, chat_id: int | None = None) -> dict:
        return {
            "id": "callback-id",
            "from": {"id": user_id},
            "data": data,
            "message": {
                "message_id": 77,
                "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
            },
        }


class GuideConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_guide_is_registered_only_in_private_and_admin_command_scopes(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.api = AsyncMock(return_value={"ok": True})

        await bot.setup_commands()

        calls = bot.api.await_args_list
        default_commands = json.loads(calls[0].args[1]["commands"])
        private_commands = json.loads(calls[1].args[1]["commands"])
        admin_commands = json.loads(calls[2].args[1]["commands"])
        self.assertNotIn("guide", {item["command"] for item in default_commands})
        self.assertIn("guide", {item["command"] for item in private_commands})
        self.assertIn("guide", {item["command"] for item in admin_commands})

    def test_all_guide_callback_data_stays_within_telegram_limit(self) -> None:
        for page in (
            "home",
            "start",
            "research",
            "research_search",
            "safety",
            "trading",
            "converter",
            "inline",
            "commands",
            "terms",
            "live_channels",
            "trending",
            "close",
        ):
            value = TelegramDashboardBot.guide_callback_data(9_999_999_999, page)
            self.assertLessEqual(len(value.encode("utf-8")), 64)

    def test_guide_editor_is_present_in_message_settings(self) -> None:
        callbacks = {
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.message_settings_markup())["inline_keyboard"]
            for button in row
        }
        self.assertIn("guide_message_settings", callbacks)

        guide_callbacks = {
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.guide_message_settings_markup())["inline_keyboard"]
            for button in row
        }
        self.assertIn("edit_guide_message:guide_inline_message", guide_callbacks)


if __name__ == "__main__":
    unittest.main()
