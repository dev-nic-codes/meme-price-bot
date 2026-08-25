from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.config import CANVAS_SIZE, COINS
from src.price_service import DISPLAY_TICKERS, example_values
from src.renderer import DashboardRenderer


EXPECTED_TICKERS = [
    "UTYA",
    "REDO",
    "SCAT",
    "YODA",
    "CHERRY",
    "MTONGA",
    "GROYP",
    "GRAMMING",
    "GRM",
]


class NineCardDashboardTests(unittest.TestCase):
    def test_dashboard_order_and_price_sources_are_complete(self) -> None:
        self.assertEqual([coin.ticker for coin in COINS], EXPECTED_TICKERS)
        self.assertEqual(DISPLAY_TICKERS, [*EXPECTED_TICKERS[:5], "BCHERRY", *EXPECTED_TICKERS[5:], "GRAM"])

    def test_renderer_writes_full_resolution_nine_card_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "dashboard.png"
            DashboardRenderer().render_to_file(example_values(), output)
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, CANVAS_SIZE)
                self.assertEqual(rendered.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
