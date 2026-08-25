from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot


class PublicButtonCatalogTests(unittest.TestCase):
    def test_every_public_button_is_labeled_grouped_and_valid(self) -> None:
        grouped = [
            key
            for keys in TelegramDashboardBot.PUBLIC_BUTTON_GROUPS.values()
            for key in keys
        ]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(TelegramDashboardBot.PUBLIC_BUTTON_DEFAULTS), set(grouped))

        for key, default in TelegramDashboardBot.PUBLIC_BUTTON_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIn(key, TelegramDashboardBot.PUBLIC_BUTTON_LABELS)
                self.assertTrue(default.strip())
                self.assertLessEqual(len(default), 64)
                for placeholder in TelegramDashboardBot.PUBLIC_BUTTON_REQUIRED_PLACEHOLDERS.get(key, ()):
                    self.assertIn(placeholder, default)

        for group in TelegramDashboardBot.PUBLIC_BUTTON_GROUPS:
            markup = json.loads(TelegramDashboardBot.public_button_group_markup(group))
            callbacks = [
                button["callback_data"]
                for row in markup["inline_keyboard"]
                for button in row
                if button.get("callback_data", "").startswith("edit_public_button:")
            ]
            self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in callbacks))

    def test_button_factory_changes_visuals_without_changing_action(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.public_button_texts = {"guide_start": "Begin here"}
        bot.public_button_icons = {"guide_start": "123456789"}

        button = bot.public_button("guide_start", callback_data="guide:111:start")

        self.assertEqual(button["text"], "Begin here")
        self.assertEqual(button["icon_custom_emoji_id"], "123456789")
        self.assertEqual(button["callback_data"], "guide:111:start")

    def test_dynamic_button_templates_keep_runtime_values(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.public_button_texts = {
            "subscription_join": "Join [CHANNEL] now",
            "alert_item": "[STATUS] [TOKEN_SYMBOL] | [TARGET]",
        }
        bot.public_button_icons = {}

        join = bot.public_button(
            "subscription_join",
            replacements={"[CHANNEL]": "@memeprice"},
            url="https://t.me/memeprice",
        )
        alert = bot.public_button(
            "alert_item",
            replacements={
                "[STATUS]": "🟢",
                "[TOKEN_SYMBOL]": "UTYA",
                "[METRIC]": "Price",
                "[TARGET]": "$0.03",
            },
            callback_data="alert:view:1",
        )

        self.assertEqual(join["text"], "Join @memeprice now")
        self.assertEqual(join["url"], "https://t.me/memeprice")
        self.assertEqual(alert["text"], "🟢 UTYA | $0.03")
        self.assertEqual(alert["callback_data"], "alert:view:1")

    def test_public_keyboard_functions_do_not_keep_hardcoded_button_labels(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "src" / "telegram_bot.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        public_keyboard_functions = {
            "send_alert_trigger_notification",
            "guide_private_only_markup",
            "guide_markup",
            "alert_home_markup",
            "alert_cancel_markup",
            "alert_metric_markup",
            "alert_direction_markup",
            "alert_confirm_markup",
            "handle_alert_text_input",
            "handle_alert_callback",
            "alert_list_page",
            "alert_detail_markup",
            "send_subscription_required",
            "token_choices_markup",
            "conversion_choices_markup",
        }
        violations: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in public_keyboard_functions:
                continue
            for dictionary in (item for item in ast.walk(node) if isinstance(item, ast.Dict)):
                for key_node, value_node in zip(dictionary.keys, dictionary.values):
                    if (
                        isinstance(key_node, ast.Constant)
                        and key_node.value == "text"
                        and isinstance(value_node, ast.Constant)
                        and isinstance(value_node.value, str)
                    ):
                        violations.append((node.name, dictionary.lineno, value_node.value))
        self.assertEqual([], violations)


class PublicButtonInputTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_emoji_is_extracted_as_button_icon(self) -> None:
        label, icon_id = TelegramDashboardBot.parse_public_button_input(
            "🔥 Start here",
            [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "987654321",
                }
            ],
        )
        self.assertEqual(label, "Start here")
        self.assertEqual(icon_id, "987654321")

    async def test_button_edit_is_previewed_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {}
            bot.pending_text_edits = {100: "public_button:guide_start"}
            bot.public_button_texts = {}
            bot.public_button_icons = {}
            bot.api = AsyncMock(return_value={"ok": True})
            bot.send_message = AsyncMock()

            await bot.save_pending_public_button_edit(
                100,
                100,
                "🔥 Begin",
                [
                    {
                        "type": "custom_emoji",
                        "offset": 0,
                        "length": 2,
                        "custom_emoji_id": "987654321",
                    }
                ],
            )

            stored = json.loads(bot.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["public_button_text:guide_start"], "Begin")
            self.assertEqual(stored["public_button_icon:guide_start"], "987654321")
            preview_markup = json.loads(bot.api.await_args.args[1]["reply_markup"])
            preview_button = preview_markup["inline_keyboard"][0][0]
            self.assertEqual(preview_button["text"], "Begin")
            self.assertEqual(preview_button["icon_custom_emoji_id"], "987654321")
            self.assertNotIn(100, bot.pending_text_edits)

    async def test_missing_dynamic_placeholder_is_rejected_before_preview(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_text_edits = {100: "public_button:subscription_join"}
        bot.public_button_texts = {}
        bot.public_button_icons = {}
        bot.api = AsyncMock(return_value={"ok": True})
        bot.send_message = AsyncMock()

        await bot.save_pending_public_button_edit(100, 100, "Join now", [])

        bot.api.assert_not_awaited()
        self.assertIn(100, bot.pending_text_edits)
        self.assertIn("Missing required placeholder", bot.send_message.await_args.args[1])


class PublicButtonAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_admin_can_open_button_editor_and_select_button(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {386839171}
        bot.pending_text_edits = {}
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()

        await bot.handle_callback(self.callback("public_button_settings"))
        bot.edit_message.assert_awaited_once()

        bot.edit_message.reset_mock()
        await bot.handle_callback(self.callback("edit_public_button:guide_start"))
        self.assertEqual(bot.pending_text_edits[386839171], "public_button:guide_start")
        bot.send_message.assert_awaited_once()

    @staticmethod
    def callback(data: str) -> dict:
        return {
            "id": "callback-id",
            "from": {"id": 386839171},
            "data": data,
            "message": {
                "message_id": 77,
                "chat": {"id": 386839171, "type": "private"},
            },
        }


if __name__ == "__main__":
    unittest.main()
