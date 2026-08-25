from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.models import CoinValue
from src.movement_alert import MovementTracker
from src.telegram_bot import TelegramDashboardBot


class MovementTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "movement.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initializes_without_alerting_and_triggers_at_exact_threshold(self) -> None:
        tracker = MovementTracker(self.state_path)
        self.assertIsNone(tracker.observe(0.04, 10, "2026-07-31T09:00:00+00:00"))
        self.assertIsNone(tracker.observe(0.04399, 10, "2026-07-31T09:01:00+00:00"))

        event = tracker.observe(0.044, 10, "2026-07-31T09:02:00+00:00")

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.direction, "up")
        self.assertAlmostEqual(event.change_percent, 10.0)
        self.assertAlmostEqual(tracker.reference_price or 0, 0.04)

    def test_failed_delivery_does_not_advance_reference(self) -> None:
        tracker = MovementTracker(self.state_path)
        tracker.observe(0.04, 10, "2026-07-31T09:00:00+00:00")
        event = tracker.observe(0.036, 10, "2026-07-31T09:01:00+00:00")

        self.assertIsNotNone(event)
        self.assertAlmostEqual(tracker.reference_price or 0, 0.04)

        reloaded = MovementTracker(self.state_path)
        self.assertAlmostEqual(reloaded.reference_price or 0, 0.04)

    def test_acknowledgement_persists_new_reference(self) -> None:
        tracker = MovementTracker(self.state_path)
        tracker.observe(0.04, 10, "2026-07-31T09:00:00+00:00")
        event = tracker.observe(0.036, 10, "2026-07-31T09:01:00+00:00")
        assert event is not None

        self.assertTrue(tracker.acknowledge(event))

        reloaded = MovementTracker(self.state_path)
        self.assertAlmostEqual(reloaded.reference_price or 0, 0.036)
        self.assertEqual(reloaded.last_alert_direction, "down")
        self.assertEqual(reloaded.alerts_sent, 1)

    def test_reset_preserves_delivery_statistics(self) -> None:
        tracker = MovementTracker(self.state_path)
        tracker.observe(1, 10, "2026-07-31T09:00:00+00:00")
        event = tracker.observe(1.1, 10, "2026-07-31T09:01:00+00:00")
        assert event is not None
        tracker.acknowledge(event)

        tracker.reset()

        self.assertIsNone(tracker.reference_price)
        self.assertEqual(tracker.alerts_sent, 1)


class MovementBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        self.bot.utya_movement_enabled = True
        self.bot.utya_movement_threshold_percent = 10.0
        self.bot.utya_movement_tracker = MovementTracker(
            Path(self.temporary_directory.name) / "movement.json"
        )
        self.bot.utya_movement_lock = asyncio.Lock()
        self.bot.utya_movement_up_message = TelegramDashboardBot.DEFAULT_UTYA_MOVEMENT_UP_MESSAGE
        self.bot.utya_movement_down_message = TelegramDashboardBot.DEFAULT_UTYA_MOVEMENT_DOWN_MESSAGE
        self.bot.api = AsyncMock(return_value={"ok": True})

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_posts_only_after_threshold_and_acknowledges_success(self) -> None:
        await self.bot.process_utya_movement_alert([CoinValue("UTYA", price=0.04)])
        self.bot.api.assert_not_awaited()

        await self.bot.process_utya_movement_alert([CoinValue("UTYA", price=0.044)])

        self.bot.api.assert_awaited_once()
        method, payload = self.bot.api.await_args.args
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], "@utyachat")
        self.assertIn("10.00%", payload["text"])
        self.assertAlmostEqual(self.bot.utya_movement_tracker.reference_price or 0, 0.044)

    async def test_rejected_telegram_post_keeps_reference_for_retry(self) -> None:
        await self.bot.process_utya_movement_alert([CoinValue("UTYA", price=0.04)])
        self.bot.api.return_value = {"ok": False, "error_code": 400, "description": "Bad Request"}

        await self.bot.process_utya_movement_alert([CoinValue("UTYA", price=0.036)])

        self.assertAlmostEqual(self.bot.utya_movement_tracker.reference_price or 0, 0.04)
        self.assertEqual(self.bot.utya_movement_tracker.alerts_sent, 0)

    async def test_disabled_alert_does_not_initialize_or_send(self) -> None:
        self.bot.utya_movement_enabled = False

        await self.bot.process_utya_movement_alert([CoinValue("UTYA", price=0.04)])

        self.bot.api.assert_not_awaited()
        self.assertIsNone(self.bot.utya_movement_tracker.reference_price)

    def test_admin_menu_contains_movement_settings(self) -> None:
        payload = json.loads(self.bot.menu_markup())
        callbacks = {
            button.get("callback_data")
            for row in payload["inline_keyboard"]
            for button in row
        }
        self.assertIn("utya_movement_settings", callbacks)


if __name__ == "__main__":
    unittest.main()
