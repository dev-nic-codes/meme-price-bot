from __future__ import annotations


PRICE_DECIMALS_BY_TICKER = {
    "UTYA": 5,
    "REDO": 5,
    "SCAT": 5,
    "CHERRY": 6,
    "YODA": 6,
    "MTONGA": 5,
    "GROYP": 5,
    "GRAMMING": 6,
    "GRM": 6,
}


def format_compact_number(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    n = abs(float(value))
    units = [(1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for divider, suffix in units:
        if n >= divider:
            return f"{sign}{n / divider:.2f}{suffix}".replace(".00", "")
    return f"{sign}{n:,.0f}"


def format_price(value: float | None) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value >= 1:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"${value:.5f}".rstrip("0").rstrip(".")
    if value >= 0.0001:
        return f"${value:.6f}".rstrip("0").rstrip(".")
    return f"${value:.8f}".rstrip("0").rstrip(".")


def format_price_for_ticker(ticker: str, value: float | None) -> str:
    if value is None:
        return "-"
    places = PRICE_DECIMALS_BY_TICKER.get(ticker.upper())
    if places is None:
        return format_price(value)
    return f"${float(value):,.{places}f}"


def format_change(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def change_color(value: float | None) -> tuple[int, int, int]:
    if value is None or abs(value) < 0.0000001:
        return (150, 154, 166)
    if value > 0:
        return (28, 220, 145)
    return (255, 82, 96)
