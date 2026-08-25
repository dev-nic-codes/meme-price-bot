from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock

from src.alert_service import (
    AlertDraft,
    AlertStore,
    parse_alert_target,
    threshold_reached,
)
from src.telegram_bot import TelegramDashboardBot
from src.token_report import TokenPair


def sample_pair(
    *,
    price: float | None = 0.025,
    market_cap: float | None = 2_000_000,
    change_24h: float | None = 5.5,
) -> TokenPair:
    changes = {} if change_24h is None else {"h24": change_24h}
    return TokenPair(
        token_address="EQBaCgUwOoc6gHCNln_oJzb0mVs79YG7wYoavh-o1ItaneLA",
        pair_address="EQ_PAIR",
        name="UTYA",
        symbol="UTYA",
        dex_id="dedust",
        url="https://dexscreener.com/ton/example",
        price_usd=price,
        market_cap=market_cap,
        fdv=2_500_000,
        liquidity_usd=500_000,
        price_change=changes,
    )


def draft_for(pair: TokenPair, *, metric: str, direction: str, target: float) -> AlertDraft:
    draft = AlertDraft(metric=metric, direction=direction, target_value=target)
    draft.set_pair(pair)
    draft.current_value = pair.price_usd
    return draft


class AlertParsingTests(unittest.TestCase):
    def test_parses_currency_percent_and_suffixes(self) -> None:
        self.assertEqual(parse_alert_target("$1,250", "price"), 1250)
        self.assertEqual(parse_alert_target("1.5M", "market_cap"), 1_500_000)
        self.assertEqual(parse_alert_target("-5%", "change_24h"), -5)
        self.assertEqual(parse_alert_target("+10", "change_24h"), 10)

    def test_rejects_invalid_non_positive_market_targets(self) -> None:
        for value in ("", "abc", "0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_alert_target(value, "price")

    def test_thresholds_are_inclusive(self) -> None:
        self.assertTrue(threshold_reached("above", 10, 10))
        self.assertTrue(threshold_reached("below", 10, 10))
        self.assertFalse(threshold_reached("above", 9.99, 10))
        self.assertFalse(threshold_reached("below", 10.01, 10))


class AlertStoreTests(unittest.TestCase):
    def test_persists_alert_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            pair = sample_pair()
            store = AlertStore(path)
            created = store.create(100, draft_for(pair, metric="price", direction="above", target=0.03))
            store.save()

            loaded = AlertStore(path)
            self.assertEqual(loaded.get_for_user(100, created.alert_id).token_symbol, "UTYA")
            self.assertIsNone(loaded.get_for_user(200, created.alert_id))
            with self.assertRaisesRegex(ValueError, "already active"):
                loaded.create(100, draft_for(pair, metric="price", direction="above", target=0.03))

    def test_pause_rearm_and_delete_are_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AlertStore(Path(directory) / "alerts.json")
            pair = sample_pair()
            alert = store.create(100, draft_for(pair, metric="price", direction="above", target=0.03))
            self.assertFalse(store.toggle(100, alert.alert_id).active)
            self.assertTrue(store.toggle(100, alert.alert_id).active)
            self.assertFalse(store.delete(200, alert.alert_id))
            self.assertTrue(store.delete(100, alert.alert_id))

    def test_loads_valid_backup_when_primary_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            store = AlertStore(path)
            store.create(100, draft_for(sample_pair(), metric="price", direction="above", target=0.03))
            store.save()
            store.save()
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(len(AlertStore(path).for_user(100)), 1)


class AlertCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_alert_opens_menu_after_subscription_check(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_alert_inputs = {}
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_alert_menu = AsyncMock()

        await bot.handle_message(
            {"from": {"id": 100}, "chat": {"id": 100, "type": "private"}, "text": "/alert"}
        )

        bot.ensure_subscribed.assert_awaited_once_with(100, 100, "private")
        bot.send_alert_menu.assert_awaited_once_with(100, 100)

    async def test_group_alert_is_refused_without_starting_workflow(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_alert_inputs = {}
        bot.pending_text_edits = {}
        bot.send_message = AsyncMock()

        await bot.handle_message(
            {"from": {"id": 100}, "chat": {"id": -1001, "type": "supergroup"}, "text": "/alert"}
        )

        self.assertNotIn(100, bot.pending_alert_inputs)
        self.assertIn("Private alerts", bot.send_message.await_args.args[1])


class AlertSetupNavigationTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_alert_inputs = {
            100: AlertDraft(
                step="direction",
                token_name="UTYA",
                token_symbol="UTYA",
                metric="price",
                current_value=0.025,
            )
        }
        bot.pending_alert_choices = {100: {"0": "token-address"}}
        bot.alert_search_message = TelegramDashboardBot.DEFAULT_ALERT_SEARCH_MESSAGE
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        return bot

    def test_every_alert_setup_screen_has_contextual_back_navigation(self) -> None:
        bot = self.make_bot()
        draft = bot.pending_alert_inputs[100]
        expected = (
            (bot.alert_cancel_markup(), "alert:home"),
            (bot.alert_metric_markup(draft), "alert:back:token"),
            (bot.alert_direction_markup(), "alert:back:metric"),
            (bot.alert_target_markup(), "alert:back:direction"),
            (bot.alert_confirm_markup(), "alert:back:target"),
        )

        for raw_markup, callback in expected:
            buttons = [
                button
                for row in json.loads(raw_markup)["inline_keyboard"]
                for button in row
            ]
            back_button = next(
                button for button in buttons if button.get("callback_data") == callback
            )
            self.assertEqual(back_button["text"], "⬅️ Back")

    async def test_target_screen_has_back_button_and_returns_to_direction(self) -> None:
        bot = self.make_bot()

        await bot.handle_alert_callback(
            user_id=100,
            chat_id=100,
            message_id=5,
            callback_id="callback",
            data="alert:direction:below",
        )

        draft = bot.pending_alert_inputs[100]
        self.assertEqual((draft.step, draft.direction), ("target", "below"))
        target_markup = json.loads(bot.edit_message.await_args.args[3])
        target_callbacks = {
            button.get("callback_data")
            for row in target_markup["inline_keyboard"]
            for button in row
        }
        self.assertIn("alert:back:direction", target_callbacks)
        self.assertNotIn("alert:home", target_callbacks)

        bot.answer_callback.reset_mock()
        bot.edit_message.reset_mock()
        await bot.handle_alert_callback(
            user_id=100,
            chat_id=100,
            message_id=5,
            callback_id="callback",
            data="alert:back:direction",
        )

        self.assertEqual((draft.step, draft.direction, draft.target_value), ("direction", "", None))
        self.assertIn("Choose the trigger direction", bot.edit_message.await_args.args[2])
        direction_markup = json.loads(bot.edit_message.await_args.args[3])
        direction_callbacks = {
            button.get("callback_data")
            for row in direction_markup["inline_keyboard"]
            for button in row
        }
        self.assertIn("alert:direction:above", direction_callbacks)
        self.assertIn("alert:direction:below", direction_callbacks)

    async def test_metric_back_returns_to_token_search_without_going_home(self) -> None:
        bot = self.make_bot()
        bot.pending_alert_inputs[100].step = "metric"

        await bot.handle_alert_callback(
            user_id=100,
            chat_id=100,
            message_id=5,
            callback_id="callback",
            data="alert:back:token",
        )

        draft = bot.pending_alert_inputs[100]
        self.assertEqual(draft.step, "token")
        self.assertEqual(draft.token_address, "")
        self.assertNotIn(100, bot.pending_alert_choices)
        self.assertIn("Find a TON token", bot.edit_message.await_args.args[2])


class AlertMessageSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_admin_can_open_alert_message_settings(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {100}
        bot.pending_text_edits = {}
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()

        await bot.handle_callback(
            {
                "id": "callback",
                "from": {"id": 100},
                "data": "alert_message_settings",
                "message": {"message_id": 5, "chat": {"id": 100, "type": "private"}},
            }
        )

        bot.answer_callback.assert_awaited_once()
        bot.edit_message.assert_awaited_once()

    async def test_authorized_admin_can_open_group_and_start_any_public_edit(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.admin_ids = {100}
        bot.pending_text_edits = {}
        bot.answer_callback = AsyncMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        callback = {
            "id": "callback",
            "from": {"id": 100},
            "message": {"message_id": 5, "chat": {"id": 100, "type": "private"}},
        }

        await bot.handle_callback(
            {**callback, "data": "public_message_group:alert_targets"}
        )
        self.assertIn("Conditions and targets", bot.edit_message.await_args.args[2])

        await bot.handle_callback(
            {**callback, "data": "edit_public_message:alert_target_message"}
        )
        self.assertEqual("alert_target_message", bot.pending_text_edits[100])
        self.assertIn("[TOKEN_SYMBOL]", bot.send_message.await_args.args[1])

    async def test_custom_emoji_and_required_placeholders_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {}
            bot.pending_text_edits = {100: "alert_trigger_message"}
            bot.alert_trigger_message = TelegramDashboardBot.DEFAULT_ALERT_TRIGGER_MESSAGE
            bot.api = AsyncMock(return_value={"ok": True})
            bot.send_message = AsyncMock()
            text = (
                "🔔 [TOKEN_NAME] $[TOKEN_SYMBOL]\n"
                "[METRIC] [CONDITION] [TARGET]\nCurrent: [CURRENT]"
            )

            await bot.save_pending_text_edit(
                100,
                100,
                text,
                [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "987654"}],
            )

            saved = json.loads(bot.settings_path.read_text(encoding="utf-8"))["alert_trigger_message"]
            self.assertIn('<tg-emoji emoji-id="987654">🔔</tg-emoji>', saved)
            self.assertEqual(bot.alert_trigger_message, saved)
            self.assertNotIn(100, bot.pending_text_edits)

    def test_missing_dynamic_placeholder_is_rejected(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        error = bot.validate_message_template(
            "alert_trigger_message",
            "[TOKEN_NAME] [TOKEN_SYMBOL] [METRIC] [CONDITION] [TARGET]",
        )
        self.assertIn("[CURRENT]", error)

    def test_alert_message_settings_are_present_in_admin_menu(self) -> None:
        callbacks = {
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.message_settings_markup())["inline_keyboard"]
            for button in row
        }
        editor_callbacks = {
            button["callback_data"]
            for row in json.loads(TelegramDashboardBot.alert_message_settings_markup())["inline_keyboard"]
            for button in row
        }
        self.assertIn("alert_message_settings", callbacks)
        expected_groups = {
            f"public_message_group:{group}"
            for group in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS
        }
        self.assertEqual(
            expected_groups,
            {
                callback
                for callback in editor_callbacks
                if callback.startswith("public_message_group:")
            },
        )
        editable_keys = {
            callback.removeprefix("edit_public_message:")
            for group in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS
            for row in json.loads(
                TelegramDashboardBot.public_message_group_markup(group)
            )["inline_keyboard"]
            for button in row
            if (callback := button.get("callback_data", "")).startswith("edit_public_message:")
        }
        expected_keys = {
            key
            for keys in TelegramDashboardBot.PUBLIC_MESSAGE_GROUPS.values()
            for key in keys
        }
        self.assertEqual(expected_keys, editable_keys)


class AlertEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def build_bot(self, path: Path, pair: TokenPair) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.alert_store = AlertStore(path)
        bot.alert_store_lock = asyncio.Lock()
        bot.alert_api_semaphore = asyncio.Semaphore(2)
        bot.token_report_service = type(
            "PairService",
            (),
            {"best_pair_for_token": AsyncMock(return_value=pair)},
        )()
        bot.send_alert_trigger_notification = AsyncMock(return_value={"ok": True})
        return bot

    async def test_trigger_is_persisted_sent_once_and_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            pair = sample_pair(price=0.04)
            bot = await self.build_bot(path, pair)
            alert = bot.alert_store.create(
                100,
                draft_for(pair, metric="price", direction="above", target=0.03),
            )
            bot.alert_store.save()

            await bot.evaluate_alerts_once()
            triggered = bot.alert_store.alerts[alert.alert_id]
            self.assertFalse(triggered.active)
            self.assertEqual(triggered.notification_status, "sent")
            self.assertEqual(triggered.triggered_value, 0.04)
            bot.send_alert_trigger_notification.assert_awaited_once()

            await bot.evaluate_alerts_once()
            bot.send_alert_trigger_notification.assert_awaited_once()
            restored = AlertStore(path).alerts[alert.alert_id]
            self.assertEqual(restored.notification_status, "sent")

    async def test_missing_metric_does_not_fire_or_become_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            pair = sample_pair(market_cap=None)
            bot = await self.build_bot(path, pair)
            alert = bot.alert_store.create(
                100,
                draft_for(pair, metric="market_cap", direction="below", target=1_000_000),
            )

            await bot.evaluate_alerts_once()
            checked = bot.alert_store.alerts[alert.alert_id]
            self.assertTrue(checked.active)
            self.assertIn("unavailable", checked.last_error.lower())
            bot.send_alert_trigger_notification.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
