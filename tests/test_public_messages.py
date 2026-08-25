from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from src.telegram_bot import TelegramDashboardBot


class PublicMessageCatalogTests(unittest.TestCase):
    def test_every_default_is_labeled_editable_and_valid(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        for key, default in bot.PUBLIC_MESSAGE_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIn(key, bot.PUBLIC_MESSAGE_LABELS)
                self.assertIn(key, bot.TEXT_EDIT_KEYS)
                self.assertIsNone(bot.validate_message_template(key, default))

    def test_every_alert_message_is_reachable_from_grouped_editor(self) -> None:
        alert_keys = {
            key
            for key in TelegramDashboardBot.PUBLIC_MESSAGE_DEFAULTS
            if key.startswith("alert_")
        }
        alert_keys.add("private_alerts_message")
        grouped_keys = {
            key
            for keys in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS.values()
            for key in keys
        }
        self.assertEqual(alert_keys, grouped_keys)

        for group in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS:
            markup = json.loads(TelegramDashboardBot.public_message_group_markup(group))
            callbacks = [
                button["callback_data"]
                for row in markup["inline_keyboard"]
                for button in row
                if button.get("callback_data", "").startswith("edit_public_message:")
            ]
            self.assertEqual(
                list(TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS[group]),
                [callback.removeprefix("edit_public_message:") for callback in callbacks],
            )
            self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in callbacks))

    def test_non_alert_public_messages_are_reachable_from_existing_editors(self) -> None:
        alert_keys = {
            key
            for keys in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS.values()
            for key in keys
        }
        guide_keys = set(TelegramDashboardBot.GUIDE_MESSAGE_KEYS)
        inline_keys = set(TelegramDashboardBot.INLINE_MESSAGE_KEYS)
        standard_keys = set(TelegramDashboardBot.MESSAGE_EDIT_CALLBACK_KEYS.values())
        covered = alert_keys | guide_keys | inline_keys | standard_keys
        self.assertEqual(
            set(TelegramDashboardBot.PUBLIC_MESSAGE_DEFAULTS),
            covered & set(TelegramDashboardBot.PUBLIC_MESSAGE_DEFAULTS),
        )

        guide_callbacks = [
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.guide_message_settings_markup())["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("edit_guide_message:")
        ]
        self.assertEqual(
            list(TelegramDashboardBot.GUIDE_MESSAGE_KEYS),
            [callback.removeprefix("edit_guide_message:") for callback in guide_callbacks],
        )
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in guide_callbacks))

        inline_callbacks = [
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.inline_message_settings_markup())["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("edit_public_message:")
        ]
        self.assertEqual(
            list(TelegramDashboardBot.INLINE_MESSAGE_KEYS),
            [callback.removeprefix("edit_public_message:") for callback in inline_callbacks],
        )
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in inline_callbacks))

    def test_inline_templates_require_every_live_value_placeholder(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)

        for key in TelegramDashboardBot.INLINE_MESSAGE_KEYS:
            with self.subTest(key=key):
                default = bot.PUBLIC_MESSAGE_DEFAULTS[key]
                first_required = bot.PUBLIC_MESSAGE_REQUIRED_PLACEHOLDERS[key][0]
                self.assertIsNone(bot.validate_message_template(key, default))
                self.assertIn(
                    first_required,
                    str(bot.validate_message_template(key, default.replace(first_required, ""))),
                )
        self.assertIn(
            "[ATH_PRICE]",
            TelegramDashboardBot.PUBLIC_MESSAGE_REQUIRED_PLACEHOLDERS["inline_coin_message"],
        )

    def test_dynamic_values_are_escaped_but_custom_note_markup_is_preserved(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        rendered = bot.render_public_message(
            "alert_token_not_found_message",
            {"[QUERY]": "<unsafe>"},
        )
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertNotIn("<unsafe>", rendered)

        note = '<tg-emoji emoji-id="123">ℹ️</tg-emoji> Saved'
        rendered_note = bot.alert_note_text(note)
        self.assertIn('<tg-emoji emoji-id="123">ℹ️</tg-emoji>', rendered_note)

    def test_alert_status_note_is_always_separated_from_editable_page_text(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.alert_note_message = "\n\n[NOTE]\n"
        page = "🪞 <b>My Alerts</b>\n\n\n\nYou have no saved alerts yet.🙂"

        rendered = bot.append_alert_note(page, "🙂 Alert deleted.")

        self.assertEqual(rendered, f"{page}\n\n🙂 Alert deleted.")

    def test_public_alert_handlers_do_not_send_hardcoded_message_literals(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "src" / "telegram_bot.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        public_handlers = {
            "handle_alert_text_input",
            "prepare_alert_pair",
            "handle_alert_callback",
        }
        violations: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in public_handlers:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr not in {"send_message", "edit_message"}:
                    continue
                text_index = 1 if call.func.attr == "send_message" else 2
                if len(call.args) <= text_index:
                    continue
                if isinstance(call.args[text_index], (ast.Constant, ast.JoinedStr, ast.BinOp)):
                    violations.append((node.name, call.lineno))
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
