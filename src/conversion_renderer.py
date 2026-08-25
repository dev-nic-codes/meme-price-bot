from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from math import isfinite

from PIL import Image, ImageDraw, ImageFont

from .config import FONT_DIR, TEMPLATE_DIR


@dataclass(frozen=True)
class ConversionCardData:
    gram_amount: Decimal
    token_symbol: str
    token_amount: Decimal
    gram_price_usd: float
    gram_change_24h: float | None = None


class ConversionRenderer:
    TEMPLATE_PATH = TEMPLATE_DIR / "convert.png"

    def __init__(self) -> None:
        self.template = Image.open(self.TEMPLATE_PATH).convert("RGB")
        self.fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def render(self, data: ConversionCardData) -> Image.Image:
        image = self.template.copy()
        draw = ImageDraw.Draw(image)

        # GRAM and the footer label are embedded in the template.
        self._draw_fitted_amount(
            draw,
            (118, 259, 1162, 350),
            format_amount(data.gram_amount),
            max_size=72,
        )

        symbol = clean_symbol(data.token_symbol)
        self._draw_text(draw, (120, 476), symbol, self._font(26), (186, 201, 255))
        converted_amount = format_converted_amount(data.token_amount, symbol)
        self._draw_fitted_amount(
            draw,
            (118, 515, 1162, 610),
            converted_amount,
            max_size=72,
        )

        price_text = f"${data.gram_price_usd:,.2f}"
        footer_font = self._font(30, numeric=True)
        self._draw_text(
            draw,
            (284, 643),
            price_text,
            footer_font,
            (244, 247, 255),
            emphasized=True,
        )
        change_text = format_percentage(data.gram_change_24h)
        if change_text is not None:
            change_x = 284 + int(draw.textlength(price_text, font=footer_font)) + 20
            self._draw_text(
                draw,
                (change_x, 643),
                change_text,
                footer_font,
                percentage_color(data.gram_change_24h),
                emphasized=True,
            )
        return image

    def _draw_fitted_amount(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        text: str,
        *,
        max_size: int,
    ) -> None:
        left, top, right, bottom = box
        max_width = right - left
        max_height = bottom - top
        size = max_size
        while size > 36:
            font = self._font(size, numeric=True)
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=1)
            if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
                break
            size -= 2
        self._draw_text(
            draw,
            (left, top),
            text,
            self._font(size, numeric=True),
            (250, 251, 255),
            emphasized=True,
        )

    def _font(self, size: int, *, numeric: bool = False) -> ImageFont.FreeTypeFont:
        family = "numbers" if numeric else "labels"
        key = (family, size)
        cached = self.fonts.get(key)
        if cached is not None:
            return cached
        font_name = "Sora.ttf"
        loaded = ImageFont.truetype(str(FONT_DIR / font_name), size=size)
        self.fonts[key] = loaded
        return loaded

    @staticmethod
    def _draw_text(
        draw: ImageDraw.ImageDraw,
        point: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple[int, int, int],
        *,
        emphasized: bool = False,
    ) -> None:
        x, y = point
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0), stroke_width=1)
        stroke_fill = fill if emphasized else (8, 17, 35)
        draw.text((x, y), text, font=font, fill=fill, stroke_width=1, stroke_fill=stroke_fill)


def format_amount(value: Decimal) -> str:
    if not value.is_finite():
        return "0"
    absolute = abs(value)
    if absolute == 0:
        return "0"
    if absolute.adjusted() >= 15 or absolute.adjusted() <= -11:
        return f"{value:.8E}".replace("E+", "e+").replace("E-", "e-")
    decimal_places = max(0, min(10, 7 - absolute.adjusted()))
    quantum = Decimal(1).scaleb(-decimal_places)
    with localcontext() as context:
        context.prec = max(40, len(value.as_tuple().digits) + decimal_places + 4)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    text = f"{rounded:,.{decimal_places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_converted_amount(value: Decimal, symbol: str) -> str:
    if clean_symbol(symbol) == "USD":
        with localcontext() as context:
            context.prec = max(40, len(value.as_tuple().digits) + 4)
            rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${rounded:,.2f}"
    return format_amount(value)


def clean_symbol(value: str) -> str:
    cleaned = "".join(char for char in str(value).upper() if char.isalnum() or char in {"-", "_"})
    return cleaned[:20] or "TOKEN"


def format_percentage(value: float | None) -> str | None:
    if value is None or not isfinite(value):
        return None
    return f"{value:+.2f}%"


def percentage_color(value: float | None) -> tuple[int, int, int]:
    if value is None or not isfinite(value) or value == 0:
        return (184, 193, 211)
    if value > 0:
        return (46, 218, 145)
    return (255, 85, 105)
