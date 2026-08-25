from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import FONT_DIR, TEMPLATE_DIR
from .token_report import format_percent, format_price


CANVAS_SIZE = (1586, 992)
TEMPLATE_PATH = TEMPLATE_DIR / "token_report_v2.png"

WHITE = (246, 247, 249)
GRAY = (153, 151, 153)
POSITIVE = (34, 197, 94)
NEGATIVE = (255, 78, 91)
NEUTRAL = (163, 164, 169)
CHART_COLOR = (6, 133, 252)
ATH_LABEL_CENTER_Y = 512
ATH_VALUE_LEFT = 240
TOKEN_LOGO_LEFT = 125
TOKEN_LOGO_TOP = 79
TOKEN_LOGO_SIZE = 86
TOKEN_LOGO_CENTER_Y = TOKEN_LOGO_TOP + TOKEN_LOGO_SIZE / 2


@dataclass(frozen=True)
class TokenCardData:
    name: str
    symbol: str
    price: float | None
    change_24h: float | None
    ath_price: float | None
    chart_points: tuple[tuple[float, float], ...] = ()
    logo_bytes: bytes | None = None


class TokenCardRenderer:
    """Render the reference-style seven-day USD token card."""

    def __init__(self) -> None:
        with Image.open(TEMPLATE_PATH) as source:
            template = source.convert("RGB")
        if template.size != CANVAS_SIZE:
            template = template.resize(CANVAS_SIZE, Image.Resampling.LANCZOS)
        self.template = template
        self.fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def render(self, data: TokenCardData) -> Image.Image:
        image = self.template.convert("RGBA").copy()
        draw = ImageDraw.Draw(image)
        logo_drawn = self._draw_logo(image, data.logo_bytes)
        self._draw_name(draw, data.name or data.symbol, logo_drawn)
        self._draw_price(draw, data.symbol, data.price)
        self._draw_change(draw, data.change_24h)
        self._draw_ath(draw, data.ath_price)
        self._draw_chart(image, data.chart_points)
        return image.convert("RGB")

    def render_to_file(self, data: TokenCardData, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = self.render(data)
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            image.save(output_path, "JPEG", quality=95, subsampling=0, optimize=True)
        else:
            image.save(output_path, "PNG", optimize=True)
        return output_path

    def _draw_name(self, draw: ImageDraw.ImageDraw, value: str, logo_drawn: bool) -> None:
        text = str(value or "Unknown token").strip()
        left = 224 if logo_drawn else 130
        max_width = 930
        font = self._fitted_font(text, "DejaVuSans-Bold.ttf", 50, 30, max_width)
        text = self._ellipsize(draw, text, font, max_width)
        if logo_drawn:
            bbox = font.getbbox(text)
            text_y = TOKEN_LOGO_CENTER_Y - (bbox[1] + bbox[3]) / 2
        else:
            text_y = 100
        self._draw_text(draw, (left, text_y), text, font, WHITE)

    def _draw_price(
        self,
        draw: ImageDraw.ImageDraw,
        symbol: str,
        value: float | None,
    ) -> None:
        text = self._format_current_price(symbol, value)
        font = self._fitted_font(text, "DejaVuSans-Bold.ttf", 142, 66, 1060)
        x, y = 126, 207
        self._draw_text(draw, (x, y), text, font, WHITE)

    @staticmethod
    def _format_current_price(symbol: str, value: float | None) -> str:
        if value is None:
            return "N/A"
        if str(symbol or "").upper() == "REDO":
            return f"${float(value):.5f}"
        return format_price(value)

    def _draw_change(self, draw: ImageDraw.ImageDraw, value: float | None) -> None:
        text = format_percent(value)
        color = self._accent(value)
        font = self._font("DejaVuSans-Bold.ttf", 61)
        x, y = 129, 382
        self._draw_text(draw, (x, y), text, font, color)
        width = draw.textbbox((0, 0), text, font=font)[2]
        bbox = font.getbbox(text)
        center_y = y + (bbox[1] + bbox[3]) / 2
        self._draw_direction_arrow(draw, x + width + 34, center_y, color, value)

    def _draw_ath(self, draw: ImageDraw.ImageDraw, value: float | None) -> None:
        text = format_price(value) if value is not None else "N/A"
        font = self._fitted_font(text, "Sora.ttf", 45, 30, 680)
        bbox = font.getbbox(text)
        text_y = ATH_LABEL_CENTER_Y - (bbox[1] + bbox[3]) / 2
        self._draw_text(draw, (ATH_VALUE_LEFT, text_y), text, font, WHITE)

    def _draw_logo(self, image: Image.Image, payload: bytes | None) -> bool:
        if not payload:
            return False
        try:
            with Image.open(BytesIO(payload)) as source:
                logo = ImageOps.exif_transpose(source).convert("RGBA")
                logo.thumbnail((78, 78), Image.Resampling.LANCZOS)
        except Exception:
            return False

        badge = Image.new("RGBA", (TOKEN_LOGO_SIZE, TOKEN_LOGO_SIZE), (0, 0, 0, 0))
        badge_draw = ImageDraw.Draw(badge)
        badge_draw.ellipse((1, 1, 84, 84), fill=(4, 13, 23, 235), outline=(212, 215, 218, 175), width=2)
        offset = ((TOKEN_LOGO_SIZE - logo.width) // 2, (TOKEN_LOGO_SIZE - logo.height) // 2)
        circular_mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(circular_mask).ellipse((0, 0, logo.width - 1, logo.height - 1), fill=255)
        alpha = ImageChops.multiply(logo.getchannel("A"), circular_mask)
        badge.paste(logo, offset, alpha)
        image.alpha_composite(badge, (TOKEN_LOGO_LEFT, TOKEN_LOGO_TOP))
        return True

    def _draw_chart(self, image: Image.Image, raw_points: tuple[tuple[float, float], ...]) -> None:
        by_timestamp: dict[float, float] = {}
        for timestamp, price in raw_points:
            try:
                x_value = float(timestamp)
                y_value = float(price)
            except (TypeError, ValueError):
                continue
            if math.isfinite(x_value) and math.isfinite(y_value) and x_value > 0 and y_value > 0:
                by_timestamp[x_value] = y_value
        points = sorted(by_timestamp.items())

        if len(points) < 2:
            draw = ImageDraw.Draw(image)
            font = self._font("Sora.ttf", 24)
            text = "PRICE HISTORY UNAVAILABLE"
            bbox = draw.textbbox((0, 0), text, font=font)
            self._draw_text(draw, ((CANVAS_SIZE[0] - (bbox[2] - bbox[0])) / 2, 674), text, font, (92, 94, 99))
            return

        left, top, right, bottom = 136, 570, 1442, 766
        start_time, end_time = points[0][0], points[-1][0]
        prices = [price for _, price in points]
        low, high = min(prices), max(prices)
        if math.isclose(low, high):
            padding = max(low * 0.02, 1e-12)
            low -= padding
            high += padding
        else:
            padding = (high - low) * 0.08
            low -= padding
            high += padding

        def chart_x(timestamp: float) -> float:
            if math.isclose(start_time, end_time):
                return (left + right) / 2
            return left + ((timestamp - start_time) / (end_time - start_time)) * (right - left)

        def chart_y(price: float) -> float:
            return bottom - ((price - low) / (high - low)) * (bottom - top)

        accent = CHART_COLOR
        scale = 3
        padding = 36
        layer_left = max(0, left - padding)
        layer_top = max(0, top - padding)
        layer_right = min(CANVAS_SIZE[0], right + padding)
        layer_bottom = min(CANVAS_SIZE[1], bottom + padding)
        layer_width = layer_right - layer_left
        layer_height = layer_bottom - layer_top
        scaled_points = [
            (
                round((chart_x(ts) - layer_left) * scale),
                round((chart_y(price) - layer_top) * scale),
            )
            for ts, price in points
        ]
        layer = Image.new(
            "RGBA",
            (layer_width * scale, layer_height * scale),
            (0, 0, 0, 0),
        )

        fill = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        fill_draw = ImageDraw.Draw(fill)
        polygon = scaled_points + [
            ((right - layer_left) * scale, (bottom - layer_top) * scale),
            ((left - layer_left) * scale, (bottom - layer_top) * scale),
        ]
        fill_draw.polygon(polygon, fill=(*accent, 22))
        layer = Image.alpha_composite(layer, fill)

        glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).line(scaled_points, fill=(*accent, 155), width=11 * scale, joint="curve")
        glow = glow.filter(ImageFilter.GaussianBlur(6 * scale))
        layer = Image.alpha_composite(layer, glow)
        ImageDraw.Draw(layer).line(scaled_points, fill=(*accent, 255), width=3 * scale, joint="curve")

        layer = layer.resize((layer_width, layer_height), Image.Resampling.LANCZOS)
        image.alpha_composite(layer, (layer_left, layer_top))
        self._draw_date_labels(ImageDraw.Draw(image), start_time, end_time, left, right)

    def _draw_date_labels(
        self,
        draw: ImageDraw.ImageDraw,
        start_time: float,
        end_time: float,
        left: int,
        right: int,
    ) -> None:
        span = max(0.0, end_time - start_time)
        font = self._font("Sora.ttf", 28)
        for index in range(4):
            ratio = index / 3
            timestamp = start_time + span * ratio
            date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            label = f"{date.strftime('%b').upper()} {date.day}" if span >= 172_800 else date.strftime("%H:%M")
            bbox = draw.textbbox((0, 0), label, font=font)
            width = bbox[2] - bbox[0]
            anchor = left + (right - left) * ratio
            x = left if index == 0 else right - width if index == 3 else anchor - width / 2
            self._draw_text(draw, (x, 827), label, font, GRAY)

    def _draw_direction_arrow(
        self,
        draw: ImageDraw.ImageDraw,
        left: float,
        center_y: float,
        color: tuple[int, int, int],
        value: float | None,
    ) -> None:
        if value is None or value == 0:
            return
        center_x = round(left + 13)
        middle_y = round(center_y)
        if value > 0:
            draw.line((center_x, middle_y + 20, center_x, middle_y - 12), fill=color, width=7)
            draw.polygon(
                ((center_x, middle_y - 25), (center_x - 13, middle_y - 8), (center_x + 13, middle_y - 8)),
                fill=color,
            )
        else:
            draw.line((center_x, middle_y - 20, center_x, middle_y + 12), fill=color, width=7)
            draw.polygon(
                ((center_x, middle_y + 25), (center_x - 13, middle_y + 8), (center_x + 13, middle_y + 8)),
                fill=color,
            )

    def _fitted_font(
        self,
        text: str,
        filename: str,
        start_size: int,
        minimum_size: int,
        max_width: int,
    ) -> ImageFont.FreeTypeFont:
        for size in range(start_size, minimum_size - 1, -2):
            font = self._font(filename, size)
            bbox = font.getbbox(text)
            if bbox[2] - bbox[0] <= max_width:
                return font
        return self._font(filename, minimum_size)

    def _font(self, filename: str, size: int) -> ImageFont.FreeTypeFont:
        key = (filename, size)
        cached = self.fonts.get(key)
        if cached is not None:
            return cached
        loaded = ImageFont.truetype(str(FONT_DIR / filename), size=size)
        self.fonts[key] = loaded
        return loaded

    @staticmethod
    def _ellipsize(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> str:
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return text
        suffix = "…"
        value = text
        while value and draw.textbbox((0, 0), value + suffix, font=font)[2] > max_width:
            value = value[:-1]
        return value.rstrip() + suffix

    @staticmethod
    def _draw_text(
        draw: ImageDraw.ImageDraw,
        point: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple[int, int, int],
    ) -> None:
        x, y = point
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 135))
        draw.text((x, y), text, font=font, fill=fill)

    @staticmethod
    def _accent(value: float | None) -> tuple[int, int, int]:
        if value is None or value == 0:
            return NEUTRAL
        return POSITIVE if value > 0 else NEGATIVE
