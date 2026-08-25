from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.telegram_bot import TelegramDashboardBot


class UsageStatsTests(unittest.TestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {100}
        bot.subscriber_ids = {1, 2}
        bot.usage_stats = bot.default_usage_stats()
        bot.schedule_usage_stats_save = MagicMock()
        return bot

    def test_legacy_private_flag_migrates_to_audience_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage_stats.json"
            path.write_text(
                json.dumps(
                    {
                        "users": {
                            "1": {"private": True},
                            "2": {"private": False},
                        }
                    }
                ),
                encoding="utf-8",
            )
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.usage_stats_path = path

            stats = bot.load_usage_stats()

        self.assertTrue(stats["users"]["1"]["private_chat"])
        self.assertFalse(stats["users"]["1"]["public_chat"])
        self.assertFalse(stats["users"]["1"]["inline_mode"])
        self.assertFalse(stats["users"]["2"]["private_chat"])
        self.assertTrue(stats["users"]["2"]["public_chat"])

    def test_one_user_can_be_recorded_in_all_modes_without_duplication(self) -> None:
        bot = self.make_bot()

        bot.touch_usage_user(7, private=True)
        bot.touch_usage_user(7, public_chat=True)
        bot.touch_usage_user(7, inline_mode=True)

        self.assertEqual(1, len(bot.usage_stats["users"]))
        record = bot.usage_stats["users"]["7"]
        self.assertTrue(record["private_chat"])
        self.assertTrue(record["public_chat"])
        self.assertTrue(record["inline_mode"])

    def test_inline_query_records_unique_inline_user(self) -> None:
        bot = self.make_bot()

        bot.record_inline_usage({"from": {"id": 9, "is_bot": False}})
        bot.record_inline_usage({"from": {"id": 9, "is_bot": False}})

        self.assertEqual(1, len(bot.usage_stats["users"]))
        record = bot.usage_stats["users"]["9"]
        self.assertTrue(record["inline_mode"])
        self.assertFalse(record["private_chat"])
        self.assertFalse(record["public_chat"])
        self.assertEqual(2, bot.schedule_usage_stats_save.call_count)

    def test_statistics_text_shows_each_unique_audience_mode(self) -> None:
        bot = self.make_bot()
        bot.usage_stats["users"] = {
            "1": {"private_chat": True, "public_chat": False, "inline_mode": False},
            "2": {"private_chat": False, "public_chat": True, "inline_mode": False},
            "3": {"private_chat": True, "public_chat": True, "inline_mode": True},
            "4": {"private_chat": False, "public_chat": False, "inline_mode": True},
        }

        text = bot.usage_stats_text()

        self.assertIn("All unique users: <b>4</b>", text)
        self.assertIn("Private-message users: <b>2</b>", text)
        self.assertIn("Public/group-chat users: <b>2</b>", text)
        self.assertIn("Inline-mode users: <b>2</b>", text)
        self.assertIn("Users active in multiple modes: <b>1</b>", text)


if __name__ == "__main__":
    unittest.main()
