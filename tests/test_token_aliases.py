from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot


class RemovedTokenAliasCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_bot() -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_alert_inputs = {}
        bot.pending_text_edits = {}
        bot.admin_ids = set()
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_token_report = AsyncMock()
        bot.send_meme = AsyncMock()
        return bot

    async def test_removed_aliases_do_not_dispatch_token_reports(self) -> None:
        for command in ("/utya", "/redo"):
            with self.subTest(command=command):
                bot = self.make_bot()

                await bot.handle_message(
                    {
                        "from": {"id": 100},
                        "chat": {"id": -1001, "type": "supergroup"},
                        "text": command,
                    }
                )

                bot.ensure_subscribed.assert_not_awaited()
                bot.send_token_report.assert_not_awaited()
                bot.send_meme.assert_not_awaited()

    async def test_removed_aliases_are_absent_from_every_command_scope(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {100}
        bot.api = AsyncMock(return_value={"ok": True})

        await bot.setup_commands()

        self.assertEqual(bot.api.await_count, 3)
        for call in bot.api.await_args_list:
            commands = {
                item["command"]
                for item in json.loads(call.args[1]["commands"])
            }
            self.assertNotIn("utya", commands)
            self.assertNotIn("redo", commands)

    async def test_tokens_remain_available_through_meme_command(self) -> None:
        for query in ("utya", "redo"):
            with self.subTest(query=query):
                bot = self.make_bot()

                await bot.handle_message(
                    {
                        "from": {"id": 100},
                        "chat": {"id": -1001, "type": "supergroup"},
                        "text": f"/meme {query}",
                    }
                )

                bot.ensure_subscribed.assert_awaited_once_with(100, -1001, "supergroup")
                bot.send_token_report.assert_awaited_once_with(-1001, query)
                bot.send_meme.assert_not_awaited()

    async def test_report_prewarm_still_refreshes_both_detailed_reports(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        pairs = {
            "utya": object(),
            "redo": object(),
        }
        bot.resolve_token_pair_for_query = AsyncMock(
            side_effect=lambda query: (pairs[query], None)
        )
        bot.refresh_token_report_image = AsyncMock()

        await bot.prewarm_token_alias_images()

        self.assertEqual(
            [call.args[0] for call in bot.resolve_token_pair_for_query.await_args_list],
            ["utya", "redo"],
        )
        self.assertEqual(
            [call.args[0] for call in bot.refresh_token_report_image.await_args_list],
            [pairs["utya"], pairs["redo"]],
        )


if __name__ == "__main__":
    unittest.main()
