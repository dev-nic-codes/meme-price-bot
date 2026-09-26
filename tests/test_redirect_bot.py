from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from src.redirect_bot import TelegramRedirectBot


def make_bot() -> TelegramRedirectBot:
    bot = TelegramRedirectBot.__new__(TelegramRedirectBot)
    bot.bot_token = "test"
    bot.source_username = "memepricesbot"
    bot.target_username = "memesbot"
    bot.admin_ids = {386839171, 6422556848}
    bot.cooldown_seconds = 30
    bot.offset = 0
    bot.bot_id = 8945392927
    bot.cooldowns = {}
    bot.session = None
    return bot


class RedirectMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_private_messages_and_explicit_group_usage_are_redirected(self) -> None:
        bot = make_bot()
        self.assertTrue(bot.message_needs_redirect({"chat": {"type": "private"}}))
        self.assertTrue(
            bot.message_needs_redirect(
                {
                    "chat": {"type": "supergroup"},
                    "text": "/meme@MemePricesBot UTYA",
                }
            )
        )
        self.assertTrue(
            bot.message_needs_redirect(
                {
                    "chat": {"type": "group"},
                    "reply_to_message": {"from": {"id": 8945392927}},
                }
            )
        )

    def test_ordinary_group_and_channel_messages_are_ignored(self) -> None:
        bot = make_bot()
        self.assertFalse(
            bot.message_needs_redirect(
                {"chat": {"type": "supergroup"}, "text": "normal conversation"}
            )
        )
        self.assertFalse(
            bot.message_needs_redirect(
                {"chat": {"type": "channel"}, "text": "@memepricesbot"}
            )
        )

    async def test_private_redirect_is_rate_limited_per_user_and_chat(self) -> None:
        bot = make_bot()
        bot.api = AsyncMock(return_value={"ok": True, "result": {}})
        message = {
            "message_id": 5,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 100},
            "text": "/meme",
        }
        await bot.handle_message(message)
        await bot.handle_message(message)
        self.assertEqual(1, bot.api.await_count)
        method, payload = bot.api.await_args.args
        self.assertEqual("sendMessage", method)
        self.assertIn("@memesbot", payload["text"])
        markup = json.loads(payload["reply_markup"])
        self.assertEqual("https://t.me/memesbot", markup["inline_keyboard"][0][0]["url"])

    async def test_inline_query_returns_only_the_new_bot_redirect(self) -> None:
        bot = make_bot()
        bot.api = AsyncMock(return_value={"ok": True, "result": {}})
        await bot.handle_inline_query({"id": "inline-1", "query": "utya"})
        method, payload = bot.api.await_args.args
        self.assertEqual("answerInlineQuery", method)
        results = json.loads(payload["results"])
        self.assertEqual(1, len(results))
        self.assertEqual("https://t.me/memesbot", results[0]["reply_markup"]["inline_keyboard"][0][0]["url"])

    async def test_callback_is_acknowledged_and_redirect_message_is_sent(self) -> None:
        bot = make_bot()
        bot.api = AsyncMock(return_value={"ok": True, "result": {}})
        await bot.handle_callback(
            {
                "id": "callback-1",
                "from": {"id": 100},
                "message": {"chat": {"id": 100, "type": "private"}},
            }
        )
        self.assertEqual(["answerCallbackQuery", "sendMessage"], [call.args[0] for call in bot.api.await_args_list])


class RedirectStateTests(unittest.TestCase):
    def test_offset_is_persisted_atomically(self) -> None:
        bot = make_bot()
        with TemporaryDirectory() as directory:
            bot.state_path = Path(directory) / "redirect.json"
            bot.offset = 12345
            bot._save_offset()
            self.assertEqual({"offset": 12345}, json.loads(bot.state_path.read_text()))


class RedirectMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_bot_metadata_points_only_to_new_bot(self) -> None:
        bot = make_bot()
        bot.api = AsyncMock(return_value={"ok": True, "result": {}})
        await bot.configure_bot()
        calls = bot.api.await_args_list
        methods = [call.args[0] for call in calls]
        self.assertIn("setMyName", methods)
        self.assertIn("setMyDescription", methods)
        self.assertIn("setMyShortDescription", methods)
        self.assertIn("setMyCommands", methods)
        self.assertIn("setChatMenuButton", methods)
        serialized = json.dumps([call.args for call in calls])
        self.assertIn("memesbot", serialized)


if __name__ == "__main__":
    unittest.main()
