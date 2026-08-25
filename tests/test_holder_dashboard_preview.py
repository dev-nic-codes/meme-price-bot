from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.dashboard_preview_renderer import (
    CHART_X_BOUNDS,
    CHART_Y_BOUNDS,
    DashboardPreviewRenderer,
    PREVIEW_CANVAS_SIZE,
)
from src.chart_service import DashboardChartService
from src.holder_service import HolderService
from src.price_service import example_values


class HolderDashboardPreviewTests(unittest.TestCase):
    def test_holder_count_validation(self) -> None:
        self.assertEqual(HolderService._positive_int("9326"), 9326)
        self.assertEqual(HolderService._positive_int(0), 0)
        self.assertIsNone(HolderService._positive_int(-1))
        self.assertIsNone(HolderService._positive_int("unknown"))

    def test_preview_renderer_writes_expected_size(self) -> None:
        values = [
            value.__class__(
                ticker=value.ticker,
                price=value.price,
                change_24h=value.change_24h,
                market_cap=value.market_cap,
                holders=1234 if value.ticker != "GRAM" else None,
            )
            for value in example_values()
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "preview.jpg"
            DashboardPreviewRenderer().render_to_file(values, output)
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, PREVIEW_CANVAS_SIZE)
                self.assertEqual(rendered.mode, "RGB")

    def test_first_seven_logos_are_replaced_and_last_two_are_preserved(self) -> None:
        renderer = DashboardPreviewRenderer()
        rendered = renderer.render(example_values())
        logo_regions = [
            (48, 56, 158, 166), (516, 56, 626, 166), (986, 56, 1096, 166),
            (48, 371, 158, 481), (516, 371, 626, 481), (986, 371, 1096, 481),
            (48, 694, 158, 804), (516, 694, 626, 804), (986, 694, 1096, 804),
        ]
        for region in logo_regions[:7]:
            self.assertNotEqual(
                rendered.crop(region).tobytes(),
                renderer.template.crop(region).tobytes(),
            )
        for region in logo_regions[7:]:
            self.assertEqual(
                rendered.crop(region).tobytes(),
                renderer.template.crop(region).tobytes(),
            )

    def test_chart_points_are_sorted_and_invalid_rows_are_ignored(self) -> None:
        rows = [
            [3, 0, 0, 0, 1.3, 0],
            [1, 0, 0, 0, 1.1, 0],
            [2, 0, 0, 0, 1.2, 0],
            [4, 0, 0, 0, -1, 0],
            [5, 0, 0, 0, "invalid", 0],
        ]
        self.assertEqual(
            DashboardChartService.close_points(rows),
            (1.1, 1.2, 1.3),
        )

    def test_sparkline_is_drawn_only_when_real_points_exist(self) -> None:
        renderer = DashboardPreviewRenderer()
        values = example_values()
        without_chart = renderer.render(values)
        values[0] = values[0].__class__(
            ticker=values[0].ticker,
            price=values[0].price,
            change_24h=values[0].change_24h,
            market_cap=values[0].market_cap,
            chart_points=(1.0, 1.2, 1.1, 1.4, 1.3),
        )
        with_chart = renderer.render(values)
        left, right = CHART_X_BOUNDS[0]
        top, bottom = CHART_Y_BOUNDS[0]
        self.assertNotEqual(
            without_chart.crop((left, top, right + 1, bottom + 1)).tobytes(),
            with_chart.crop((left, top, right + 1, bottom + 1)).tobytes(),
        )


if __name__ == "__main__":
    unittest.main()
