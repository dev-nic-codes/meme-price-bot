from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MovementEvent:
    direction: str
    reference_price: float
    current_price: float
    change_percent: float
    observed_at: str


class MovementTracker:
    """Persists a delivered-alert baseline for percentage movement alerts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.reference_price: float | None = None
        self.reference_at = ""
        self.last_observed_price: float | None = None
        self.last_observed_at = ""
        self.last_alert_at = ""
        self.last_alert_direction = ""
        self.last_alert_change_percent: float | None = None
        self.alerts_sent = 0
        self._load()

    def observe(
        self,
        price: float,
        threshold_percent: float,
        observed_at: str,
    ) -> MovementEvent | None:
        current = self._positive_float(price)
        threshold = self._positive_float(threshold_percent)
        if current is None:
            raise ValueError("The observed price must be a positive finite number.")
        if threshold is None:
            raise ValueError("The movement threshold must be a positive finite number.")

        timestamp = str(observed_at or "").strip()
        self.last_observed_price = current
        self.last_observed_at = timestamp

        reference = self._positive_float(self.reference_price)
        if reference is None:
            self.reference_price = current
            self.reference_at = timestamp
            self.save()
            return None

        change_percent = ((current / reference) - 1.0) * 100.0
        self.save()
        if abs(change_percent) + 1e-12 < threshold:
            return None

        return MovementEvent(
            direction="up" if change_percent > 0 else "down",
            reference_price=reference,
            current_price=current,
            change_percent=change_percent,
            observed_at=timestamp,
        )

    def acknowledge(self, event: MovementEvent) -> bool:
        reference = self._positive_float(self.reference_price)
        if reference is None or abs(reference - event.reference_price) > max(reference, 1.0) * 1e-12:
            return False

        self.reference_price = event.current_price
        self.reference_at = event.observed_at
        self.last_alert_at = event.observed_at
        self.last_alert_direction = event.direction
        self.last_alert_change_percent = event.change_percent
        self.alerts_sent += 1
        self.save()
        return True

    def reset(self) -> None:
        self.reference_price = None
        self.reference_at = ""
        self.last_observed_price = None
        self.last_observed_at = ""
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "reference_price": self.reference_price,
            "reference_at": self.reference_at,
            "last_observed_price": self.last_observed_price,
            "last_observed_at": self.last_observed_at,
            "last_alert_at": self.last_alert_at,
            "last_alert_direction": self.last_alert_direction,
            "last_alert_change_percent": self.last_alert_change_percent,
            "alerts_sent": self.alerts_sent,
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(payload, dict):
            return

        self.reference_price = self._positive_float(payload.get("reference_price"))
        self.reference_at = str(payload.get("reference_at") or "")
        self.last_observed_price = self._positive_float(payload.get("last_observed_price"))
        self.last_observed_at = str(payload.get("last_observed_at") or "")
        self.last_alert_at = str(payload.get("last_alert_at") or "")
        self.last_alert_direction = str(payload.get("last_alert_direction") or "")
        self.last_alert_change_percent = self._finite_float(
            payload.get("last_alert_change_percent")
        )
        try:
            self.alerts_sent = max(0, int(payload.get("alerts_sent") or 0))
        except (TypeError, ValueError):
            self.alerts_sent = 0

    @staticmethod
    def _finite_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) else None

    @classmethod
    def _positive_float(cls, value: Any) -> float | None:
        parsed = cls._finite_float(value)
        return parsed if parsed is not None and parsed > 0 else None
