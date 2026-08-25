from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.telegram_bot import TelegramDashboardBot
from src.token_card_renderer import (
    ATH_VALUE_LEFT,
    CANVAS_SIZE,
    CHART_COLOR,
    NEGATIVE,
    POSITIVE,
    TOKEN_LOGO_CENTER_Y,
    TokenCardData,
    TokenCardRenderer,
)
from src.token_report import KNOWN_TOKEN_ADDRESSES, TokenPair, TokenReportService, build_history, parse_pair


def sample_data(
    *,
    logo_bytes: bytes | None = None,
    with_chart: bool = True,
    name: str = "Utya",
    change_24h: float = 8.53,
) -> TokenCardData:
    start = datetime(2026, 7, 22, tzinfo=timezone.utc).timestamp()
    points = tuple(
        (start + index * 86_400, price)
        for index, price in enumerate((0.021, 0.023, 0.022, 0.026, 0.025, 0.028, 0.027, 0.031))
    )
    return TokenCardData(
        name=name,
        symbol="UTYA",
        price=0.031245,
        change_24h=change_24h,
        ath_price=0.05984,
        chart_points=points if with_chart else (),
        logo_bytes=logo_bytes,
    )


class TokenCardRendererTests(unittest.TestCase):
    def test_redo_current_price_always_uses_five_decimals(self) -> None:
        self.assertEqual(
            TokenCardRenderer._format_current_price("REDO", 0.1141),
            "$0.11410",
        )
        self.assertEqual(
            TokenCardRenderer._format_current_price("UTYA", 0.1141),
            "$0.1141",
        )

    def test_renders_reference_canvas_as_rgb(self) -> None:
        rendered = TokenCardRenderer().render(sample_data())
        self.assertEqual(rendered.size, CANVAS_SIZE)
        self.assertEqual(rendered.mode, "RGB")

    def test_real_chart_points_change_only_the_chart_region(self) -> None:
        renderer = TokenCardRenderer()
        without_chart = renderer.render(sample_data(with_chart=False))
        with_chart = renderer.render(sample_data(with_chart=True))
        chart_region = (120, 550, 1460, 875)
        self.assertNotEqual(
            without_chart.crop(chart_region).tobytes(),
            with_chart.crop(chart_region).tobytes(),
        )

    def test_chart_uses_requested_blue(self) -> None:
        self.assertEqual(CHART_COLOR, (6, 133, 252))
        rendered = TokenCardRenderer().render(sample_data())
        chart_region = rendered.crop((120, 550, 1460, 790))
        blue_pixels = sum(
            1
            for r, g, b in chart_region.getdata()
            if r < 70 and 90 <= g <= 180 and b >= 210
        )
        self.assertGreater(blue_pixels, 500)

    def test_price_has_no_usd_suffix(self) -> None:
        renderer = TokenCardRenderer()
        rendered = renderer.render(sample_data())
        suffix_region = (990, 250, 1350, 340)
        self.assertEqual(
            rendered.crop(suffix_region).tobytes(),
            renderer.template.crop(suffix_region).tobytes(),
        )

    def test_official_logo_is_composited_in_header(self) -> None:
        renderer = TokenCardRenderer()
        without_logo = renderer.render(sample_data())
        logo_bytes = Path("assets/logos/utya.png").read_bytes()
        with_logo = renderer.render(sample_data(logo_bytes=logo_bytes))
        logo_region = (118, 72, 218, 175)
        self.assertNotEqual(
            without_logo.crop(logo_region).tobytes(),
            with_logo.crop(logo_region).tobytes(),
        )

    def test_embedded_mp_logo_is_preserved(self) -> None:
        renderer = TokenCardRenderer()
        rendered = renderer.render(sample_data())
        mp_region = (1260, 52, 1500, 200)
        self.assertEqual(
            renderer.template.crop(mp_region).tobytes(),
            rendered.crop(mp_region).tobytes(),
        )

    def test_token_name_is_bold_and_high_contrast(self) -> None:
        renderer = TokenCardRenderer()
        logo_bytes = Path("assets/logos/utya.png").read_bytes()
        rendered = renderer.render(sample_data(logo_bytes=logo_bytes))
        name_region = rendered.crop((220, 90, 650, 175))
        bright_pixels = sum(1 for r, g, b in name_region.getdata() if min(r, g, b) >= 220)
        self.assertGreater(bright_pixels, 700)

    def test_token_name_is_vertically_centered_with_logo(self) -> None:
        renderer = TokenCardRenderer()
        logo_bytes = Path("assets/logos/utya.png").read_bytes()
        rendered = renderer.render(sample_data(logo_bytes=logo_bytes))
        pixels = rendered.load()
        coordinates = [
            (x, y)
            for y in range(70, 175)
            for x in range(220, 650)
            if min(pixels[x, y]) >= 220
        ]
        self.assertTrue(coordinates)
        center_y = (min(y for _, y in coordinates) + max(y for _, y in coordinates) + 1) / 2
        self.assertAlmostEqual(center_y, TOKEN_LOGO_CENTER_Y, delta=2)

    def test_ath_value_is_centered_on_baked_label(self) -> None:
        self.assertEqual(ATH_VALUE_LEFT, 240)
        rendered = TokenCardRenderer().render(sample_data())
        pixels = rendered.load()
        coordinates = [
            (x, y)
            for y in range(460, 555)
            for x in range(260, 650)
            if min(pixels[x, y]) >= 220
        ]
        self.assertTrue(coordinates)
        center_y = (min(y for _, y in coordinates) + max(y for _, y in coordinates) + 1) / 2
        self.assertAlmostEqual(center_y, 512, delta=1)

    def test_24h_change_uses_clear_up_and_down_arrows(self) -> None:
        renderer = TokenCardRenderer()
        positive = renderer.render(sample_data(change_24h=8.53))
        negative = renderer.render(sample_data(change_24h=-8.53))

        def accent_count(image, box: tuple[int, int, int, int], accent: tuple[int, int, int]) -> int:
            return sum(
                1
                for r, g, b in image.crop(box).getdata()
                if max(abs(r - accent[0]), abs(g - accent[1]), abs(b - accent[2])) <= 12
            )

        self.assertEqual(POSITIVE, (34, 197, 94))
        self.assertEqual(NEGATIVE, (255, 78, 91))
        change_region = (120, 370, 500, 460)
        self.assertGreater(accent_count(positive, change_region, POSITIVE), 500)
        self.assertGreater(accent_count(negative, change_region, NEGATIVE), 500)

        top = (390, 386, 470, 416)
        bottom = (390, 418, 470, 450)
        self.assertGreater(
            accent_count(positive, top, POSITIVE),
            accent_count(positive, bottom, POSITIVE),
        )
        self.assertGreater(
            accent_count(negative, bottom, NEGATIVE),
            accent_count(negative, top, NEGATIVE),
        )


class TokenHistoryChartTests(unittest.TestCase):
    def test_build_history_keeps_ordered_seven_day_close_points(self) -> None:
        now = datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp()

        def row(days_ago: int, close: float) -> list[float]:
            timestamp = now - days_ago * 86_400
            return [timestamp, close, close * 1.02, close * 0.98, close, 100]

        hourly = [row(8, 0.01), row(1, 0.03), row(7, 0.02), row(3, 0.025), row(0, 0.031)]
        history = build_history(hourly, [], now_timestamp=now)
        self.assertEqual(
            history.chart_points,
            tuple(sorted((item[0], item[4]) for item in hourly if item[0] >= now - 7 * 86_400)),
        )

    def test_pair_parser_carries_provider_token_artwork(self) -> None:
        pair = parse_pair(
            {
                "chainId": "ton",
                "pairAddress": "pool",
                "baseToken": {"address": "token", "name": "Token", "symbol": "TOK"},
                "priceUsd": "0.1",
                "info": {"imageUrl": "https://cdn.dexscreener.com/cms/images/example.png"},
            }
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair.image_url, "https://cdn.dexscreener.com/cms/images/example.png")

    def test_only_official_provider_cdn_urls_are_downloaded(self) -> None:
        self.assertTrue(TokenReportService._trusted_logo_url("https://cdn.dexscreener.com/cms/images/a.png"))
        self.assertTrue(TokenReportService._trusted_logo_url("https://dd.dexscreener.com/ds-data/tokens/a.png"))
        self.assertFalse(TokenReportService._trusted_logo_url("http://cdn.dexscreener.com/a.png"))
        self.assertFalse(TokenReportService._trusted_logo_url("https://example.com/a.png"))


class TokenLogoSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_token_uses_bundled_official_logo(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.token_report_service = type("Service", (), {"fetch_logo": AsyncMock(return_value=b"remote")})()
        pair = TokenPair(
            token_address=KNOWN_TOKEN_ADDRESSES["utya"],
            pair_address="pool",
            name="Utya",
            symbol="UTYA",
            dex_id="stonfi",
            url="",
            price_usd=0.03,
            market_cap=1_000_000,
            fdv=1_000_000,
            liquidity_usd=100_000,
            price_change={"h24": 1.0},
            image_url="https://cdn.dexscreener.com/cms/images/remote.png",
        )
        logo = await bot.token_logo_bytes(pair)
        self.assertEqual(logo, Path("assets/logos/utya.png").read_bytes())
        bot.token_report_service.fetch_logo.assert_not_awaited()

    async def test_unknown_token_uses_provider_metadata_logo(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.token_report_service = type("Service", (), {"fetch_logo": AsyncMock(return_value=b"remote")})()
        pair = TokenPair(
            token_address="EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c",
            pair_address="pool",
            name="Token",
            symbol="TOK",
            dex_id="dedust",
            url="",
            price_usd=0.1,
            market_cap=1_000_000,
            fdv=1_000_000,
            liquidity_usd=100_000,
            price_change={},
            image_url="https://cdn.dexscreener.com/cms/images/remote.png",
        )
        self.assertEqual(await bot.token_logo_bytes(pair), b"remote")
        bot.token_report_service.fetch_logo.assert_awaited_once_with(pair.image_url)


if __name__ == "__main__":
    unittest.main()
