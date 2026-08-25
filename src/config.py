from __future__ import annotations

from pathlib import Path

from .models import CardLayout, CoinTheme


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"
LOGO_DIR = ASSETS_DIR / "logos"
FONT_DIR = ASSETS_DIR / "fonts"
TEMPLATE_DIR = ASSETS_DIR / "templates"
TEMPLATE_PATH = TEMPLATE_DIR / "dashboard-holders-preview.jpg"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_PATH = OUTPUT_DIR / "dashboard.jpg"

CANVAS_SIZE = (1437, 1094)

COINS: list[CoinTheme] = [
    CoinTheme("UTYA", "UTYA", LOGO_DIR / "utya.png", (255, 205, 35)),
    CoinTheme("REDO", "REDO", LOGO_DIR / "redo.png", (170, 176, 190)),
    CoinTheme("SCAT", "SCAT", LOGO_DIR / "scat.png", (255, 72, 160)),
    CoinTheme("YODA", "YODA", LOGO_DIR / "yoda.png", (94, 220, 118)),
    CoinTheme("CHERRY", "CHERRY", LOGO_DIR / "cherry.png", (255, 70, 82)),
    CoinTheme("MTONGA", "MTONGA", LOGO_DIR / "mtonga.png", (52, 156, 255)),
    CoinTheme("GROYP", "GROYP", LOGO_DIR / "groyp.png", (52, 210, 98)),
    CoinTheme("GRAMMING", "GRAMMING", LOGO_DIR / "gramming.png", (45, 145, 255)),
    CoinTheme("GRM", "GRM", LOGO_DIR / "grm.png", (210, 214, 222)),
]

GRAM_TICKER = "GRAM"


def build_grid_layout() -> dict[str, CardLayout]:
    margin_x = 58
    margin_y = 58
    gap_x = 38
    gap_y = 42
    card_w = 576
    card_h = 448

    layout: dict[str, CardLayout] = {}
    for index, coin in enumerate(COINS):
        row = index // 3
        col = index % 3
        layout[coin.ticker] = CardLayout(
            x=margin_x + col * (card_w + gap_x),
            y=margin_y + row * (card_h + gap_y),
            width=card_w,
            height=card_h,
        )
    return layout


CARD_LAYOUT = build_grid_layout()
