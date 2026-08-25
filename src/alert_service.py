from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .token_report import TokenPair, format_money, format_percent, format_price


ALERT_METRICS = frozenset({"price", "market_cap", "change_24h"})
ALERT_DIRECTIONS = frozenset({"above", "below"})
ALERT_STORE_VERSION = 1
SUFFIX_MULTIPLIERS = {
    "": Decimal("1"),
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "t": Decimal("1000000000000"),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_alert_target(raw_value: str, metric: str) -> float:
    if metric not in ALERT_METRICS:
        raise ValueError("Choose a valid alert type first.")
    cleaned = str(raw_value or "").strip().lower()
    cleaned = cleaned.replace("$", "").replace(",", "").replace("_", "").replace(" ", "")
    if metric == "change_24h":
        cleaned = cleaned.replace("%", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([kmbt]?)", cleaned)
    if not match:
        example = "10 or -5%" if metric == "change_24h" else "0.025 or 1.5M"
        raise ValueError(f"Enter a valid number, for example {example}.")
    try:
        parsed = Decimal(match.group(1)) * SUFFIX_MULTIPLIERS[match.group(2)]
    except InvalidOperation as exc:
        raise ValueError("Enter a valid numeric target.") from exc
    if not parsed.is_finite():
        raise ValueError("Enter a finite numeric target.")
    if metric != "change_24h" and parsed <= 0:
        raise ValueError("The target must be greater than zero.")
    if abs(parsed) > Decimal("1000000000000000"):
        raise ValueError("The target is too large.")
    return float(parsed)


def metric_value(pair: TokenPair, metric: str) -> float | None:
    if metric == "price":
        return pair.price_usd
    if metric == "market_cap":
        return pair.market_cap
    if metric == "change_24h":
        return pair.price_change.get("h24")
    return None


def metric_label(metric: str) -> str:
    return {
        "price": "Price",
        "market_cap": "Market cap",
        "change_24h": "24H change",
    }.get(metric, metric)


def direction_label(direction: str) -> str:
    return "reaches or rises above" if direction == "above" else "reaches or falls below"


def format_metric_value(metric: str, value: float | None) -> str:
    if value is None:
        return "Unavailable"
    if metric == "price":
        return format_price(value)
    if metric == "market_cap":
        return format_money(value)
    if metric == "change_24h":
        return format_percent(value)
    return f"{value:,.6g}"


def threshold_reached(direction: str, current_value: float, target_value: float) -> bool:
    if not math.isfinite(current_value) or not math.isfinite(target_value):
        return False
    if direction == "above":
        return current_value >= target_value
    if direction == "below":
        return current_value <= target_value
    return False


@dataclass
class AlertDraft:
    step: str = "token"
    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    pair_address: str = ""
    pair_url: str = ""
    metric: str = ""
    direction: str = ""
    target_value: float | None = None
    current_value: float | None = None

    def set_pair(self, pair: TokenPair) -> None:
        self.token_address = pair.token_address
        self.token_name = pair.name
        self.token_symbol = pair.symbol.upper()
        self.pair_address = pair.pair_address
        self.pair_url = pair.url


@dataclass
class UserAlert:
    alert_id: str
    user_id: int
    token_address: str
    token_name: str
    token_symbol: str
    pair_address: str
    pair_url: str
    metric: str
    direction: str
    target_value: float
    created_at: str
    active: bool = True
    last_value: float | None = None
    last_checked_at: str = ""
    last_error: str = ""
    triggered_at: str = ""
    triggered_value: float | None = None
    notification_status: str = ""
    notification_attempts: int = 0
    last_notification_attempt_at: str = ""
    notification_retry_after_seconds: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UserAlert:
        alert = cls(
            alert_id=str(payload.get("alert_id") or "").strip(),
            user_id=int(payload.get("user_id") or 0),
            token_address=str(payload.get("token_address") or "").strip(),
            token_name=str(payload.get("token_name") or "Unknown").strip() or "Unknown",
            token_symbol=str(payload.get("token_symbol") or "?").strip().upper() or "?",
            pair_address=str(payload.get("pair_address") or "").strip(),
            pair_url=str(payload.get("pair_url") or "").strip(),
            metric=str(payload.get("metric") or "").strip(),
            direction=str(payload.get("direction") or "").strip(),
            target_value=float(payload.get("target_value")),
            created_at=str(payload.get("created_at") or "").strip() or utc_now_iso(),
            active=bool(payload.get("active", True)),
            last_value=_optional_float(payload.get("last_value")),
            last_checked_at=str(payload.get("last_checked_at") or "").strip(),
            last_error=str(payload.get("last_error") or "").strip(),
            triggered_at=str(payload.get("triggered_at") or "").strip(),
            triggered_value=_optional_float(payload.get("triggered_value")),
            notification_status=str(payload.get("notification_status") or "").strip(),
            notification_attempts=max(0, int(payload.get("notification_attempts") or 0)),
            last_notification_attempt_at=str(payload.get("last_notification_attempt_at") or "").strip(),
            notification_retry_after_seconds=max(
                0,
                int(payload.get("notification_retry_after_seconds") or 0),
            ),
        )
        if (
            not alert.alert_id
            or alert.user_id <= 0
            or not alert.token_address
            or alert.metric not in ALERT_METRICS
            or alert.direction not in ALERT_DIRECTIONS
            or not math.isfinite(alert.target_value)
        ):
            raise ValueError("Invalid alert record")
        return alert


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


class AlertStore:
    def __init__(self, path: Path, *, max_active_per_user: int = 20) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")
        self.max_active_per_user = max(1, max_active_per_user)
        self.alerts: dict[str, UserAlert] = {}
        self.load()

    def load(self) -> None:
        payload = self._read_payload(self.path)
        if payload is None:
            payload = self._read_payload(self.backup_path)
        if payload is None:
            self.alerts = {}
            return
        loaded: dict[str, UserAlert] = {}
        for item in payload.get("alerts") or []:
            if not isinstance(item, dict):
                continue
            try:
                alert = UserAlert.from_dict(item)
            except (TypeError, ValueError):
                continue
            loaded[alert.alert_id] = alert
        self.alerts = loaded

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._read_payload(self.path) is not None:
            shutil.copy2(self.path, self.backup_path)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "version": ALERT_STORE_VERSION,
            "updated_at": utc_now_iso(),
            "alerts": [asdict(alert) for alert in sorted(self.alerts.values(), key=lambda item: item.created_at)],
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)
        shutil.copy2(self.path, self.backup_path)

    def for_user(self, user_id: int) -> list[UserAlert]:
        return sorted(
            (alert for alert in self.alerts.values() if alert.user_id == user_id),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def active(self) -> list[UserAlert]:
        return [alert for alert in self.alerts.values() if alert.active]

    def pending_notifications(self) -> list[UserAlert]:
        return [
            alert
            for alert in self.alerts.values()
            if alert.notification_status == "pending" and alert.triggered_at
        ]

    def get_for_user(self, user_id: int, alert_id: str) -> UserAlert | None:
        alert = self.alerts.get(alert_id)
        return alert if alert is not None and alert.user_id == user_id else None

    def create(self, user_id: int, draft: AlertDraft) -> UserAlert:
        if (
            not draft.token_address
            or draft.metric not in ALERT_METRICS
            or draft.direction not in ALERT_DIRECTIONS
            or draft.target_value is None
            or not math.isfinite(float(draft.target_value))
            or (draft.metric != "change_24h" and float(draft.target_value) <= 0)
        ):
            raise ValueError("The alert setup is incomplete.")
        active_count = sum(1 for alert in self.alerts.values() if alert.user_id == user_id and alert.active)
        if active_count >= self.max_active_per_user:
            raise ValueError(f"You can have up to {self.max_active_per_user} active alerts.")
        duplicate = next(
            (
                alert
                for alert in self.alerts.values()
                if alert.user_id == user_id
                and alert.active
                and alert.token_address == draft.token_address
                and alert.metric == draft.metric
                and alert.direction == draft.direction
                and alert.target_value == draft.target_value
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("This alert is already active.")
        alert_id = uuid.uuid4().hex[:10]
        while alert_id in self.alerts:
            alert_id = uuid.uuid4().hex[:10]
        alert = UserAlert(
            alert_id=alert_id,
            user_id=user_id,
            token_address=draft.token_address,
            token_name=draft.token_name,
            token_symbol=draft.token_symbol,
            pair_address=draft.pair_address,
            pair_url=draft.pair_url,
            metric=draft.metric,
            direction=draft.direction,
            target_value=float(draft.target_value),
            created_at=utc_now_iso(),
            last_value=draft.current_value,
        )
        self.alerts[alert.alert_id] = alert
        return alert

    def delete(self, user_id: int, alert_id: str) -> bool:
        alert = self.get_for_user(user_id, alert_id)
        if alert is None:
            return False
        self.alerts.pop(alert.alert_id, None)
        return True

    def toggle(self, user_id: int, alert_id: str) -> UserAlert | None:
        alert = self.get_for_user(user_id, alert_id)
        if alert is None:
            return None
        if alert.active:
            alert.active = False
        else:
            active_count = sum(1 for item in self.alerts.values() if item.user_id == user_id and item.active)
            if active_count >= self.max_active_per_user:
                raise ValueError(f"You can have up to {self.max_active_per_user} active alerts.")
            alert.active = True
            alert.triggered_at = ""
            alert.triggered_value = None
            alert.notification_status = ""
            alert.notification_attempts = 0
            alert.last_notification_attempt_at = ""
            alert.notification_retry_after_seconds = 0
            alert.last_error = ""
        return alert
