from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .assets import AssetCache
from .config import COINS, GRAM_TICKER, TEMPLATE_DIR
from .formatting import change_color, format_change, format_compact_number, format_price_for_ticker
from .models import CoinTheme, CoinValue


PREVIEW_TEMPLATE_PATH = TEMPLATE_DIR / "dashboard-holders-preview.jpg"
OFFICIAL_LOGO_DIR = TEMPLATE_DIR.parent / "official_logos"
PREVIEW_CANVAS_SIZE = (1437, 1094)
SUPERSAMPLE = 4

WHITE = (232, 234, 238)
GRAY = (160, 164, 173)
GREEN = (57, 214, 118)
RED = (255, 76, 82)

TICKER_INDEX = {coin.ticker: index for index, coin in enumerate(COINS)}
OFFICIAL_LOGO_TICKERS = (
    "UTYA",
    "REDO",
    "SCAT",
    "YODA",
    "CHERRY",
    "MTONGA",
    "GROYP",
)

# Anchors are normalized from the filled 1401x1068 reference to the blank
# 1437x1094 artwork. Values are aligned to the labels baked into each card.
PRICE_LEFT_X = (
    (59, 525, 996),
    (58, 528, 997),
    (58, 528, 997),
)
PRICE_TOP_Y = (180, 499, 814)
CHANGE_X_BOUNDS = ((307, 439), (770, 902), (1244, 1376))
CHANGE_PILL_HEIGHT = 42
MARKET_CAP_LEFT_X = (
    (58, 527, 996),
    (58, 527, 996),
    (57, 527, 996),
)
HOLDERS_CENTER_X = (386, 856, 1327)
METRIC_TOP_Y = (288, 610, 927)
CHART_X_BOUNDS = ((307, 439), (770, 902), (1244, 1376))
CHART_Y_BOUNDS = ((115, 169), (434, 488), (749, 803))
CHART_ACCENTS = {
    "REDO": (58, 148, 255),
}
LOGO_GEOMETRY = {
    # Subpixel centers and true inner diameters measured from the baked frames.
    "UTYA": (103.50, 111.50, 87.0),
    "REDO": (571.00, 111.25, 87.0),
    "SCAT": (1041.50, 111.25, 87.0),
    "YODA": (103.00, 426.75, 85.0),
    "CHERRY": (570.75, 426.75, 85.0),
    "MTONGA": (1042.00, 427.00, 85.0),
    "GROYP": (101.50, 749.00, 81.0),
}
LOGO_MASK_SCALE = 8
LOGO_CLEANUP_MARGIN = 0.5
LOGO_RASTER_CENTER_CORRECTION = 0.5
TRANSPARENT_LOGO_PADDING = 0.10
LOGO_FOCUS = {
    # GROYP's artwork is weighted toward the lower-left inside its source image.
    "GROYP": (1.20, 260 / 640, 340 / 640),
}


class DashboardPreviewRenderer:
    """Render the approved holders dashboard over its fixed artwork."""

    def __init__(self, assets: AssetCache | None = None) -> None:
        self.assets = assets or AssetCache()
        self.template = self._load_template()
        self.official_logos = self._load_official_logos()
        self.price_font = self.assets._font("Sora.ttf", 39 * SUPERSAMPLE)
        self.metric_font = self.assets._font("Sora.ttf", 24 * SUPERSAMPLE)
        self.change_font = self.assets._font("Sora.ttf", 23 * SUPERSAMPLE)
        self.gram_font = self.assets._font("Sora.ttf", 16 * SUPERSAMPLE)

    @staticmethod
    def _load_template() -> Image.Image:
        if not PREVIEW_TEMPLATE_PATH.exists():
            raise FileNotFoundError(f"Preview template is missing: {PREVIEW_TEMPLATE_PATH}")
        with Image.open(PREVIEW_TEMPLATE_PATH) as source:
            image = source.convert("RGB")
        if image.size != PREVIEW_CANVAS_SIZE:
            image = image.resize(PREVIEW_CANVAS_SIZE, Image.Resampling.LANCZOS)
        return image

    def render(self, values: list[CoinValue]) -> Image.Image:
        by_ticker = {value.ticker.upper(): value for value in values}
        base = self.template.convert("RGBA").copy()
        self._paste_official_logos(base)
        overlay = Image.new(
            "RGBA",
            (PREVIEW_CANVAS_SIZE[0] * SUPERSAMPLE, PREVIEW_CANVAS_SIZE[1] * SUPERSAMPLE),
            (0, 0, 0, 0),
        )
        draw = ImageDraw.Draw(overlay)
        for coin in COINS:
            self._draw_card(draw, coin, by_ticker.get(coin.ticker, CoinValue(coin.ticker)))
        self._draw_gram(draw, by_ticker.get(GRAM_TICKER))
        overlay = overlay.resize(PREVIEW_CANVAS_SIZE, Image.Resampling.LANCZOS)
        return Image.alpha_composite(base, overlay).convert("RGB")

    @staticmethod
    def _load_official_logos() -> dict[str, Image.Image]:
        logos: dict[str, Image.Image] = {}
        for ticker in OFFICIAL_LOGO_TICKERS:
            path = OFFICIAL_LOGO_DIR / f"{ticker.lower()}.png"
            if not path.exists():
                continue
            with Image.open(path) as source:
                rgba = source.convert("RGBA")
                alpha_bbox = rgba.getchannel("A").getbbox()
                if alpha_bbox and alpha_bbox != (0, 0, *rgba.size):
                    rgba = rgba.crop(alpha_bbox)
                    side = max(rgba.size)
                    padded_side = round(side * (1 + TRANSPARENT_LOGO_PADDING * 2))
                    padded = Image.new("RGBA", (padded_side, padded_side), (0, 0, 0, 0))
                    padded.alpha_composite(
                        rgba,
                        ((padded_side - rgba.width) // 2, (padded_side - rgba.height) // 2),
                    )
                    rgba = padded
                background = Image.new("RGBA", rgba.size, (3, 8, 14, 255))
                background.alpha_composite(rgba)
                logos[ticker] = background
        return logos

    def _paste_official_logos(self, image: Image.Image) -> None:
        for ticker, logo in self.official_logos.items():
            center_x, center_y, diameter = LOGO_GEOMETRY[ticker]
            cleanup_diameter = diameter + LOGO_CLEANUP_MARGIN
            patch_margin = 3
            left = int(center_x - cleanup_diameter / 2 - patch_margin)
            top = int(center_y - cleanup_diameter / 2 - patch_margin)
            right = int(center_x + cleanup_diameter / 2 + patch_margin + 1)
            bottom = int(center_y + cleanup_diameter / 2 + patch_margin + 1)
            patch_width = right - left
            patch_height = bottom - top
            high_size = (patch_width * LOGO_MASK_SCALE, patch_height * LOGO_MASK_SCALE)
            patch = Image.new("RGBA", high_size, (0, 0, 0, 0))
            local_center = (
                (center_x - left + LOGO_RASTER_CENTER_CORRECTION) * LOGO_MASK_SCALE,
                (center_y - top + LOGO_RASTER_CENTER_CORRECTION) * LOGO_MASK_SCALE,
            )

            draw = ImageDraw.Draw(patch)
            cleanup_radius = cleanup_diameter * LOGO_MASK_SCALE / 2
            draw.ellipse(
                (
                    local_center[0] - cleanup_radius,
                    local_center[1] - cleanup_radius,
                    local_center[0] + cleanup_radius,
                    local_center[1] + cleanup_radius,
                ),
                fill=(3, 8, 14, 255),
            )

            logo_size = round(diameter * LOGO_MASK_SCALE)
            logo_source = logo.convert("RGB")
            focus = LOGO_FOCUS.get(ticker)
            if focus:
                zoom, center_x_ratio, center_y_ratio = focus
                crop_width = logo_source.width / zoom
                crop_height = logo_source.height / zoom
                source_center_x = logo_source.width * center_x_ratio
                source_center_y = logo_source.height * center_y_ratio
                logo_source = logo_source.transform(
                    logo_source.size,
                    Image.Transform.EXTENT,
                    (
                        source_center_x - crop_width / 2,
                        source_center_y - crop_height / 2,
                        source_center_x + crop_width / 2,
                        source_center_y + crop_height / 2,
                    ),
                    Image.Resampling.BICUBIC,
                    fillcolor=logo_source.getpixel((0, 0)),
                )
            fitted = ImageOps.fit(
                logo_source,
                (logo_size, logo_size),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            ).convert("RGBA")
            logo_mask = Image.new("L", (logo_size, logo_size), 0)
            ImageDraw.Draw(logo_mask).ellipse((0, 0, logo_size, logo_size), fill=255)
            fitted.putalpha(logo_mask)
            patch.alpha_composite(
                fitted,
                (
                    round(local_center[0] - logo_size / 2),
                    round(local_center[1] - logo_size / 2),
                ),
            )
            patch = patch.resize((patch_width, patch_height), Image.Resampling.LANCZOS)
            image.alpha_composite(patch, (left, top))

    def render_to_file(self, values: list[CoinValue], output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = self.render(values)
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            image.save(output_path, "JPEG", quality=95, subsampling=0, optimize=True)
        else:
            image.save(output_path, "PNG", optimize=True)
        return output_path

    def _draw_card(self, draw: ImageDraw.ImageDraw, coin: CoinTheme, value: CoinValue) -> None:
        ticker = coin.ticker
        row, column = divmod(TICKER_INDEX[ticker], 3)
        self._draw_sparkline(
            draw,
            ticker,
            value.chart_points,
            CHART_ACCENTS.get(ticker, coin.accent),
            row,
            column,
        )
        price_text = format_price_for_ticker(ticker, value.price)
        self._draw_top_left(
            draw,
            (PRICE_LEFT_X[row][column], PRICE_TOP_Y[row]),
            price_text,
            self.price_font,
            WHITE,
        )
        price_height = self._text_height(draw, price_text, self.price_font)
        pill_center_y = PRICE_TOP_Y[row] + price_height / 2
        pill_left, pill_right = CHANGE_X_BOUNDS[column]
        change_bounds = (
            pill_left,
            round(pill_center_y - CHANGE_PILL_HEIGHT / 2),
            pill_right,
            round(pill_center_y + CHANGE_PILL_HEIGHT / 2),
        )
        self._draw_change_badge(draw, change_bounds, value.change_24h)
        self._draw_top_left(
            draw,
            (MARKET_CAP_LEFT_X[row][column], METRIC_TOP_Y[row]),
            self._format_market_cap(value.market_cap),
            self.metric_font,
            WHITE,
        )
        self._draw_centered(
            draw,
            (HOLDERS_CENTER_X[column], METRIC_TOP_Y[row]),
            self._format_holders(value.holders),
            self.metric_font,
            WHITE,
        )

    @staticmethod
    def _draw_sparkline(
        draw: ImageDraw.ImageDraw,
        ticker: str,
        values: tuple[float, ...] | None,
        accent: tuple[int, int, int],
        row: int,
        column: int,
    ) -> None:
        if values is None or len(values) < 2:
            return
        valid = [float(value) for value in values if math.isfinite(value) and value > 0]
        if len(valid) < 2:
            return

        left, right = CHART_X_BOUNDS[column]
        top, bottom = CHART_Y_BOUNDS[row]
        if ticker == "GRAMMING":
            left = 824
        low = min(valid)
        high = max(valid)
        spread = high - low
        if spread <= max(abs(high), 1.0) * 1e-12:
            y_values = [(top + bottom) / 2 for _ in valid]
        else:
            vertical_padding = 4
            usable_height = bottom - top - vertical_padding * 2
            y_values = [
                bottom - vertical_padding - ((value - low) / spread) * usable_height
                for value in valid
            ]
        x_step = (right - left) / (len(valid) - 1)
        points = [
            (round((left + index * x_step) * SUPERSAMPLE), round(y * SUPERSAMPLE))
            for index, y in enumerate(y_values)
        ]

        draw.line(
            points,
            fill=(*accent, 45),
            width=8 * SUPERSAMPLE,
            joint="curve",
        )
        draw.line(
            points,
            fill=(*accent, 235),
            width=2 * SUPERSAMPLE,
            joint="curve",
        )

    def _draw_change_badge(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        change: float | None,
    ) -> None:
        x1, y1, x2, y2 = (round(value * SUPERSAMPLE) for value in bounds)
        fill = change_color(change)
        if change is None or abs(change) < 0.0000001:
            fill = GRAY
        outline = (*fill, 190)
        background = (*fill, 20)
        draw.rounded_rectangle(
            (x1, y1, x2, y2),
            radius=18 * SUPERSAMPLE,
            fill=background,
            outline=outline,
            width=1 * SUPERSAMPLE,
        )
        text = "-" if change is None else f"{'-' if change < 0 else ''}{abs(change):.2f}%"
        text_bbox = draw.textbbox(
            (0, 0),
            text,
            font=self.change_font,
            stroke_width=SUPERSAMPLE,
        )
        text_width = (text_bbox[2] - text_bbox[0]) / SUPERSAMPLE
        text_height = (text_bbox[3] - text_bbox[1]) / SUPERSAMPLE
        marker_width = 14 if change is not None else 0
        marker_gap = 3 if change is not None else 0
        group_width = marker_width + marker_gap + text_width
        group_left = (bounds[0] + bounds[2] - group_width) / 2
        if change is not None:
            self._draw_change_marker(
                draw,
                bounds,
                change,
                fill,
                group_left + marker_width / 2,
            )
        text_top = (bounds[1] + bounds[3] - text_height) / 2
        self._draw_top_left(
            draw,
            (group_left + marker_width + marker_gap, text_top),
            text,
            self.change_font,
            fill,
        )

    @staticmethod
    def _draw_change_marker(draw, bounds, change, fill, marker_center_x) -> None:
        center_x = marker_center_x * SUPERSAMPLE
        center_y = ((bounds[1] + bounds[3]) / 2) * SUPERSAMPLE
        half_width = 7 * SUPERSAMPLE
        half_height = 6 * SUPERSAMPLE
        if change > 0:
            points = (
                (center_x, center_y - half_height),
                (center_x - half_width, center_y + half_height),
                (center_x + half_width, center_y + half_height),
            )
        elif change < 0:
            points = (
                (center_x - half_width, center_y - half_height),
                (center_x + half_width, center_y - half_height),
                (center_x, center_y + half_height),
            )
        else:
            radius = 4 * SUPERSAMPLE
            draw.ellipse(
                (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
                fill=(*fill, 255),
            )
            return
        draw.polygon(points, fill=(*fill, 255))

    def _draw_gram(self, draw: ImageDraw.ImageDraw, value: CoinValue | None) -> None:
        price = "-"
        change = "-"
        change_fill = GRAY
        if value is not None and value.price is not None:
            price = f"${value.price:.2f}"
            change = format_change(value.change_24h)
            change_fill = change_color(value.change_24h)
        self._draw_top_left(draw, (169, 1039), price, self.gram_font, WHITE)
        width = self._text_width(draw, price, self.gram_font)
        self._draw_top_left(draw, (181 + width, 1040), change, self.gram_font, change_fill)

    def _draw_top_left(self, draw, position, text, font, fill) -> None:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=SUPERSAMPLE)
        x = position[0] - bbox[0] / SUPERSAMPLE
        y = position[1] - bbox[1] / SUPERSAMPLE
        self._draw_text(draw, (x, y), text, font, fill)

    def _draw_centered(self, draw, position, text, font, fill) -> None:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=SUPERSAMPLE)
        width = (bbox[2] - bbox[0]) / SUPERSAMPLE
        self._draw_top_left(draw, (position[0] - width / 2, position[1]), text, font, fill)

    @staticmethod
    def _draw_text(draw, position, text, font, fill) -> None:
        x = round(position[0] * SUPERSAMPLE)
        y = round(position[1] * SUPERSAMPLE)
        draw.text(
            (x + SUPERSAMPLE, y + SUPERSAMPLE),
            text,
            font=font,
            fill=(0, 0, 0, 210),
            stroke_width=SUPERSAMPLE,
            stroke_fill=(0, 0, 0, 210),
        )
        draw.text(
            (x, y),
            text,
            font=font,
            fill=(*fill, 255),
            stroke_width=SUPERSAMPLE,
            stroke_fill=(12, 18, 24, 230),
        )

    @staticmethod
    def _text_width(draw, text, font) -> float:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=SUPERSAMPLE)
        return (bbox[2] - bbox[0]) / SUPERSAMPLE

    @staticmethod
    def _text_height(draw, text, font) -> float:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=SUPERSAMPLE)
        return (bbox[3] - bbox[1]) / SUPERSAMPLE

    @staticmethod
    def _format_market_cap(value: float | None) -> str:
        return "-" if value is None else f"${format_compact_number(value)}"

    @staticmethod
    def _format_holders(value: int | None) -> str:
        if value is None:
            return "-"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
        if value >= 1_000:
            return f"{value / 1_000:.1f}K".replace(".0K", "K")
        return f"{value:,}"
