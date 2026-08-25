from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import FONT_DIR


class AssetCache:
    """Loads fonts and logos once so each render only copies cached assets."""

    def __init__(self) -> None:
        self.font_regular = self._font("Inter-Regular.ttf", 30)
        self.font_label = self._font("Inter-Bold.ttf", 24)
        self.font_name = self._font("Inter-Bold.ttf", 52)
        self.font_ticker = self._font("Inter-Bold.ttf", 26)
        self.font_price = self._font("Inter-Bold.ttf", 62)
        self.font_change = self._font("Inter-Bold.ttf", 40)
        self.font_cap = self._font("Inter-Bold.ttf", 42)
        self.font_small = self._font("Inter-Regular.ttf", 22)
        self.font_badge = self._font("Inter-Bold.ttf", 30)

    @staticmethod
    @lru_cache(maxsize=32)
    def _font(file_name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            FONT_DIR / file_name,
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if "Bold" in file_name else Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf") if "Bold" in file_name else Path("C:/Windows/Fonts/arial.ttf"),
        ]
        for path in candidates:
            if path.exists():
                loaded = ImageFont.truetype(str(path), size=size)
                if weight is not None and hasattr(loaded, "set_variation_by_axes"):
                    try:
                        loaded.set_variation_by_axes([weight])
                    except Exception:
                        pass
                return loaded
        return ImageFont.load_default()

    @staticmethod
    @lru_cache(maxsize=32)
    def logo(path: str, size: int) -> Image.Image | None:
        logo_path = Path(path)
        if not logo_path.exists():
            return None
        image = Image.open(logo_path).convert("RGBA")
        scale = max(size / image.width, size / image.height)
        new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

        left = max(0, (image.width - size) // 2)
        top = max(0, (image.height - size) // 2)
        image = image.crop((left, top, left + size, top + size))

        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.alpha_composite(image)
        canvas.putalpha(mask)
        return canvas.filter(ImageFilter.UnsharpMask(radius=0.6, percent=145, threshold=2))
