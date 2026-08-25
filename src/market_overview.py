from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


OVERVIEW_TICKERS = (
    "UTYA",
    "REDO",
    "SCAT",
    "YODA",
    "CHERRY",
    "MTONGA",
    "GROYP",
    "GRAMMING",
    "GRM",
)

PRICE_CHANNELS = {
    "UTYA": "@utyaprices",
    "REDO": "@redopricess",
    "SCAT": "@ScaredCatsPrice",
    "YODA": "@yodaprices",
    "CHERRY": "@cherryprices",
    "MTONGA": "@mtongaprices",
    "GROYP": "@groypprices",
    "GRAMMING": "@grammingprices",
    "GRM": "@GRM_prices",
}


def parse_interval_minutes(raw_value: str) -> int:
    value = str(raw_value or "").strip().lower().replace(" ", "")
    match = re.fullmatch(r"(?P<amount>\d+)(?P<unit>m|min|mins|h|hr|hrs|d|day|days)?", value)
    if not match:
        raise ValueError("Send an interval such as 30m, 2h, or 1d.")

    amount = int(match.group("amount"))
    unit = match.group("unit") or "m"
    if unit in {"h", "hr", "hrs"}:
        amount *= 60
    elif unit in {"d", "day", "days"}:
        amount *= 24 * 60

    if not 5 <= amount <= 7 * 24 * 60:
        raise ValueError("The interval must be from 5 minutes to 7 days.")
    return amount


def format_interval(minutes: int) -> str:
    minutes = max(0, int(minutes))
    days, remainder = divmod(minutes, 24 * 60)
    hours, remaining_minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if remaining_minutes or not parts:
        parts.append(f"{remaining_minutes}m")
    return " ".join(parts)


class MarketOverviewStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = self._load()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return {
            "next_due_at": 0.0,
            "pending": None,
            "last_posted_at": 0.0,
            "last_posted_channel": "",
            "last_approved_by": 0,
        }

    def _load(self) -> dict[str, Any]:
        state = self.defaults()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return state
        if not isinstance(payload, dict):
            return state

        for key in state:
            if key in payload:
                state[key] = payload[key]
        if not isinstance(state.get("pending"), dict):
            state["pending"] = None
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    def schedule_after(self, interval_minutes: int, *, now: float | None = None) -> None:
        self.state["next_due_at"] = float(now if now is not None else time.time()) + (
            int(interval_minutes) * 60
        )
        self.save()

