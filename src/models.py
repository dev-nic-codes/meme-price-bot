from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


Color = tuple[int, int, int]
Point = tuple[int, int]


@dataclass(frozen=True)
class CoinTheme:
    name: str
    ticker: str
    logo_path: Path
    accent: Color


@dataclass(frozen=True)
class CoinValue:
    ticker: str
    price: float | None = None
    change_24h: float | None = None
    market_cap: float | None = None
    holders: int | None = None
    chart_points: tuple[float, ...] | None = None
    ath_price: float | None = None


@dataclass(frozen=True)
class CardLayout:
    x: int
    y: int
    width: int
    height: int

    @property
    def origin(self) -> Point:
        return self.x, self.y

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height
