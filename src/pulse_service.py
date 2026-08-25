from __future__ import annotations

import bisect
import json
import math
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import CoinValue
from .token_report import KNOWN_TOKEN_ADDRESSES


MARKET_CAP_MILESTONES = (
    1_000_000,
    5_000_000,
    10_000_000,
    25_000_000,
    50_000_000,
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
)
PRICE_TIMEFRAMES = (
    ("5M", 5 * 60, 4.0, 1.20),
    ("15M", 15 * 60, 6.0, 1.12),
    ("30M", 30 * 60, 8.0, 1.06),
    ("1H", 60 * 60, 10.0, 1.00),
)
LIVE_PRICE_TIMEFRAMES = (
    ("5M", "change_m5", 2.5, 1.25),
    ("1H", "change_h1", 6.0, 1.08),
    ("6H", "change_h6", 12.0, 0.95),
)
PRICE_ONLY_TICKERS = frozenset({"GRAM"})
DEFAULT_FEED_WINDOW_SECONDS = 60 * 60
DEFAULT_COVERAGE_GAP_SECONDS = 3 * 60


@dataclass(frozen=True)
class PulseEvent:
    ticker: str
    emoji: str
    score: float
    details: tuple[str, ...]
    signal_types: tuple[str, ...]
    observed_at: float | None = None
    direction: str = ""
    chart_url: str = ""


@dataclass(frozen=True)
class PulseResult:
    updated_at: float
    events: tuple[PulseEvent, ...]
    available: bool = True
    summary: tuple[str, ...] = ()
    coverage_complete: bool = True
    coverage_minutes: int = 0


@dataclass(frozen=True)
class PulseBuy:
    trade_id: str
    amount_usd: float
    observed_at: float
    pool_address: str = ""
    tx_hash: str = ""


@dataclass(frozen=True)
class PulseCoinValue:
    ticker: str
    price: float | None = None
    change_24h: float | None = None
    market_cap: float | None = None
    holders: int | None = None
    liquidity: float | None = None
    change_m5: float | None = None
    change_h1: float | None = None
    change_h6: float | None = None
    volume_m5: float | None = None
    volume_h1: float | None = None
    volume_h24: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    largest_buy_usd: float | None = None
    largest_buy_at: float | None = None
    market_observed_at: float | None = None
    chart_url: str = ""
    recent_buys: tuple[PulseBuy, ...] = ()


@dataclass(frozen=True)
class _Signal:
    ticker: str
    kind: str
    direction: str
    score: float
    detail: str
    magnitude: float = 0.0
    observed_at: float | None = None


class PulseService:
    """Persist normalized market snapshots and derive recent pulse events.

    Provider access stays in ``PulseMarketService``; this class owns signal
    thresholds, history comparisons, grouping, ranking, and persistence.
    """

    def __init__(
        self,
        history_path: Path,
        *,
        cache_seconds: int = 120,
        retention_seconds: int = 48 * 60 * 60,
        minimum_record_interval: int = 45,
        persistence_interval: int = 5 * 60,
        max_events: int = 5,
        max_snapshot_age: int = 10 * 60,
        feed_window_seconds: int = DEFAULT_FEED_WINDOW_SECONDS,
        coverage_gap_seconds: int = DEFAULT_COVERAGE_GAP_SECONDS,
        supported_tickers: Iterable[str] | None = None,
    ) -> None:
        self.history_path = history_path
        self.cache_seconds = max(10, int(cache_seconds))
        self.retention_seconds = max(25 * 60 * 60, int(retention_seconds))
        self.minimum_record_interval = max(15, int(minimum_record_interval))
        self.persistence_interval = max(60, int(persistence_interval))
        self.max_events = max(1, min(8, int(max_events)))
        self.max_snapshot_age = max(60, int(max_snapshot_age))
        self.feed_window_seconds = max(15 * 60, int(feed_window_seconds))
        self.coverage_gap_seconds = max(90, int(coverage_gap_seconds))
        raw_tickers = supported_tickers or (*KNOWN_TOKEN_ADDRESSES.keys(), *PRICE_ONLY_TICKERS)
        self.supported_tickers = frozenset(str(ticker).upper() for ticker in raw_tickers)
        self._lock = threading.RLock()
        self._snapshots = self._load_history()
        self.buy_history_path = self.history_path.with_name("pulse_buys.json")
        self._recent_buys = self._load_buys()
        self._last_persisted_at = (
            float(self._snapshots[-1]["timestamp"]) if self._snapshots else 0.0
        )
        self._cached_result: tuple[float, PulseResult] | None = None

    def record(
        self,
        values: Iterable[CoinValue | PulseCoinValue],
        *,
        now: float | None = None,
        source_complete: bool | None = None,
    ) -> bool:
        timestamp = self._valid_timestamp(now if now is not None else time.time())
        materialized_values = tuple(values)
        coins = self._normalize_values(materialized_values)
        if not coins:
            return False
        recent_buys = self._normalize_recent_buys(materialized_values, timestamp)
        has_live_market = any(
            self._positive_float(data.get("market_observed_at")) is not None
            for data in coins.values()
        )
        market_complete = bool(source_complete) if has_live_market and source_complete is not None else None

        with self._lock:
            changed = False
            buys_changed = self._merge_recent_buys(recent_buys, timestamp)
            if (
                self._snapshots
                and timestamp - self._snapshots[-1]["timestamp"] < self.minimum_record_interval
            ):
                previous = self._snapshots[-1]
                merged = dict(previous["coins"])
                for ticker, coin_data in coins.items():
                    previous_coin = merged.get(ticker)
                    if isinstance(previous_coin, dict):
                        combined = dict(previous_coin)
                        combined.update(
                            {
                                key: value
                                for key, value in coin_data.items()
                                if value is not None and value != ""
                            }
                        )
                        merged[ticker] = combined
                    else:
                        merged[ticker] = coin_data
                if merged != previous["coins"]:
                    self._snapshots[-1] = {
                        "timestamp": max(float(previous["timestamp"]), timestamp),
                        "coins": merged,
                        "market_complete": (
                            market_complete
                            if market_complete is not None
                            else previous.get("market_complete")
                        ),
                    }
                    changed = True
            else:
                self._snapshots.append(
                    {
                        "timestamp": timestamp,
                        "coins": coins,
                        "market_complete": market_complete,
                    }
                )
                changed = True

            cutoff = timestamp - self.retention_seconds
            retained = [item for item in self._snapshots if item["timestamp"] >= cutoff]
            if len(retained) != len(self._snapshots):
                self._snapshots = retained
                changed = True

            if changed:
                if timestamp - self._last_persisted_at >= self.persistence_interval:
                    self._save_history()
                    self._last_persisted_at = timestamp
                self._cached_result = None
            if buys_changed:
                self._save_buys()
                self._cached_result = None
            return changed or buys_changed

    def current(self, *, now: float | None = None) -> PulseResult:
        timestamp = self._valid_timestamp(now if now is not None else time.time())
        with self._lock:
            if self._cached_result and self._cached_result[0] > time.monotonic():
                return self._cached_result[1]
            result = self._analyze(timestamp)
            self._cached_result = (time.monotonic() + self.cache_seconds, result)
            return result

    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def _analyze(self, now: float) -> PulseResult:
        if not self._snapshots:
            return PulseResult(updated_at=now, events=(), available=False)

        latest = self._latest_market_snapshot() or self._snapshots[-1]
        latest_at = float(latest["timestamp"])
        if latest_at > now + 30 or now - latest_at > self.max_snapshot_age:
            return PulseResult(updated_at=latest_at, events=(), available=False)
        current_coins = latest["coins"]
        if not any(self._positive_float(data.get("price")) for data in current_coins.values()):
            return PulseResult(updated_at=latest_at, events=(), available=False)

        by_ticker = self._series_by_ticker()
        has_live_market = self._snapshot_has_market_data(latest)
        signals = self._signals_for_window(latest_at, current_coins, by_ticker)

        chart_urls = {
            ticker: self._safe_chart_url(data.get("chart_url"))
            for ticker, data in current_coins.items()
            if isinstance(data, dict)
        }
        events = self._group_and_rank(signals, chart_urls)
        coverage_complete, coverage_minutes = self._coverage_status(latest_at)
        if latest.get("market_complete") is False:
            coverage_complete = False
        if not has_live_market:
            coverage_complete = True
            coverage_minutes = 0
        return PulseResult(
            updated_at=latest_at,
            events=events,
            available=True,
            summary=self._market_summary(current_coins, latest_at),
            coverage_complete=coverage_complete,
            coverage_minutes=coverage_minutes,
        )

    def _latest_market_snapshot(self) -> dict[str, Any] | None:
        return next(
            (
                snapshot
                for snapshot in reversed(self._snapshots)
                if self._snapshot_has_market_data(snapshot)
            ),
            None,
        )

    def _snapshot_has_market_data(self, snapshot: dict[str, Any]) -> bool:
        coins = snapshot.get("coins") if isinstance(snapshot, dict) else None
        return bool(
            isinstance(coins, dict)
            and any(
                isinstance(data, dict)
                and self._positive_float(data.get("market_observed_at")) is not None
                for data in coins.values()
            )
        )

    def _signals_for_window(
        self,
        latest_at: float,
        current_coins: dict[str, Any],
        by_ticker: dict[str, list[tuple[float, dict[str, Any]]]],
    ) -> list[_Signal]:
        cutoff = latest_at - self.feed_window_seconds
        strongest: dict[tuple[str, str, str], _Signal] = {}
        for ticker in sorted(self.supported_tickers):
            series = by_ticker.get(ticker, [])
            if not series:
                continue
            live_indexes = [
                index
                for index, (timestamp, data) in enumerate(series)
                if cutoff <= timestamp <= latest_at + 30
                and self._positive_float(data.get("market_observed_at")) is not None
            ]
            if not live_indexes:
                current = current_coins.get(ticker)
                if not isinstance(current, dict):
                    continue
                live_indexes = [len(series) - 1]
            for index in live_indexes:
                timestamp, current = series[index]
                if timestamp > latest_at + 30:
                    continue
                try:
                    point_signals = self._signals_for_coin(
                        ticker,
                        current,
                        series[: index + 1],
                        include_large_buy=False,
                    )
                except (ArithmeticError, TypeError, ValueError):
                    continue
                for signal in point_signals:
                    key = (signal.ticker, signal.kind, signal.direction)
                    previous = strongest.get(key)
                    if previous is None or (signal.score, signal.magnitude) > (
                        previous.score,
                        previous.magnitude,
                    ):
                        strongest[key] = signal

            current = current_coins.get(ticker)
            if ticker not in PRICE_ONLY_TICKERS and isinstance(current, dict):
                large_buy = self._large_buy_signal_for_window(ticker, current, latest_at)
                if large_buy is not None:
                    strongest[(ticker, large_buy.kind, large_buy.direction)] = large_buy
        return list(strongest.values())

    def _signals_for_coin(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
        *,
        include_large_buy: bool = True,
    ) -> list[_Signal]:
        signals: list[_Signal] = []
        current_price = self._positive_float(current.get("price"))
        if current_price is None:
            return signals

        price_signal = self._price_signal(ticker, current, series)
        if price_signal is not None:
            signals.append(price_signal)

        if ticker in PRICE_ONLY_TICKERS:
            return signals

        high_low = self._high_low_signal(ticker, current, series)
        if high_low is not None:
            signals.append(high_low)

        cap_signal = self._market_cap_signal(ticker, current, series)
        if cap_signal is not None:
            signals.append(cap_signal)

        holder_signal = self._holder_signal(ticker, current, series)
        if holder_signal is not None:
            signals.append(holder_signal)

        if include_large_buy:
            large_buy_signal = self._large_buy_signal(ticker, current)
            if large_buy_signal is not None:
                signals.append(large_buy_signal)

        volume_signal = self._volume_signal(ticker, current, series)
        if volume_signal is not None:
            signals.append(volume_signal)

        buy_pressure_signal = self._buy_pressure_signal(ticker, current, series[-1][0])
        if buy_pressure_signal is not None:
            signals.append(buy_pressure_signal)
        return signals

    def _price_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
    ) -> _Signal | None:
        current_price = self._positive_float(current.get("price"))
        if current_price is None or not series:
            return None

        candidates: list[_Signal] = []
        current_at = series[-1][0]
        market_observed_at = self._positive_float(current.get("market_observed_at"))
        current_is_live = market_observed_at is not None
        signal_observed_at = market_observed_at or current_at
        for label, seconds, threshold, timeframe_weight in PRICE_TIMEFRAMES:
            previous = self._point_for_period(series, current_at, seconds)
            if current_is_live and previous is not None and self._positive_float(
                previous.get("market_observed_at")
            ) is None:
                continue
            previous_price = self._positive_float(previous.get("price")) if previous else None
            if previous_price is None:
                continue
            change = self._percentage_change(current_price, previous_price)
            typical = self._typical_move(series, seconds)
            anomaly = abs(change) / max(typical or threshold, 0.25)
            significant = abs(change) >= threshold or (
                abs(change) >= threshold * 0.75 and typical is not None and anomaly >= 3.0
            )
            if not significant:
                continue
            ratio = abs(change) / threshold
            score = (
                45.0
                + min(28.0, max(0.0, ratio - 1.0) * 18.0)
                + min(12.0, max(0.0, anomaly - 1.0) * 3.0)
            ) * timeframe_weight
            candidates.append(
                _Signal(
                    ticker=ticker,
                    kind="price",
                    direction="up" if change > 0 else "down",
                    score=min(100.0, score),
                    detail=(
                        f"Price spike: {change:+.1f}% in {label}"
                        if change > 0
                        else f"Sudden drop: {change:+.1f}% in {label}"
                    ),
                    magnitude=abs(change),
                    observed_at=signal_observed_at,
                )
            )

        for label, key, threshold, timeframe_weight in LIVE_PRICE_TIMEFRAMES:
            change = self._finite_float(current.get(key))
            if change is None or abs(change) < threshold:
                continue
            score = (
                58.0
                + min(32.0, max(0.0, abs(change) / threshold - 1.0) * 18.0)
            ) * timeframe_weight
            candidates.append(
                _Signal(
                    ticker=ticker,
                    kind="price",
                    direction="up" if change > 0 else "down",
                    score=min(100.0, score),
                    detail=(
                        f"Price spike: {change:+.1f}% in {label}"
                        if change > 0
                        else f"Sudden drop: {change:+.1f}% in {label}"
                    ),
                    magnitude=abs(change),
                    observed_at=signal_observed_at,
                )
            )
        return max(candidates, key=lambda item: item.score, default=None)

    def _high_low_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
    ) -> _Signal | None:
        current_price = self._positive_float(current.get("price"))
        if current_price is None:
            return None
        if len(series) < 24 or series[-1][0] - series[0][0] < 23 * 60 * 60:
            return None
        cutoff = series[-1][0] - 24 * 60 * 60
        market_observed_at = self._positive_float(current.get("market_observed_at"))
        current_is_live = market_observed_at is not None
        signal_observed_at = market_observed_at or series[-1][0]
        prior_prices = [
            price
            for timestamp, data in series[:-1]
            if timestamp >= cutoff
            and (
                not current_is_live
                or self._positive_float(data.get("market_observed_at")) is not None
            )
            and (price := self._positive_float(data.get("price"))) is not None
        ]
        if len(prior_prices) < 20:
            return None
        previous_high = max(prior_prices)
        previous_low = min(prior_prices)
        if current_price >= previous_high * 1.003:
            breakout = self._percentage_change(current_price, previous_high)
            return _Signal(
                ticker,
                "high",
                "up",
                min(88.0, 64.0 + breakout * 4),
                "New 24H high",
                breakout,
                signal_observed_at,
            )
        if current_price <= previous_low * 0.997:
            breakdown = abs(self._percentage_change(current_price, previous_low))
            return _Signal(
                ticker,
                "low",
                "down",
                min(88.0, 64.0 + breakdown * 4),
                "New 24H low",
                breakdown,
                signal_observed_at,
            )
        return None

    def _market_cap_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
    ) -> _Signal | None:
        current_cap = self._positive_float(current.get("market_cap"))
        if current_cap is None or len(series) < 2:
            return None
        market_observed_at = self._positive_float(current.get("market_observed_at"))
        current_is_live = market_observed_at is not None
        previous_data = next(
            (
                data
                for _, data in reversed(series[:-1])
                if (
                    self._positive_float(data.get("market_observed_at")) is not None
                )
                == current_is_live
            ),
            None,
        )
        if previous_data is None:
            return None
        previous_cap = self._positive_float(previous_data.get("market_cap"))
        if previous_cap is None or previous_cap == current_cap:
            return None
        for milestone in MARKET_CAP_MILESTONES:
            if previous_cap < milestone <= current_cap:
                return _Signal(
                    ticker,
                    "market_cap",
                    "up",
                    min(82.0, 58.0 + max(0.0, math.log10(milestone / 1_000_000)) * 5.0),
                    f"Crossed above {self._format_money(milestone)} market cap",
                    observed_at=market_observed_at or series[-1][0],
                )
            if previous_cap >= milestone > current_cap:
                return _Signal(
                    ticker,
                    "market_cap",
                    "down",
                    min(82.0, 58.0 + max(0.0, math.log10(milestone / 1_000_000)) * 5.0),
                    f"Fell below {self._format_money(milestone)} market cap",
                    observed_at=market_observed_at or series[-1][0],
                )
        return None

    def _holder_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
    ) -> _Signal | None:
        current_holders = self._nonnegative_int(current.get("holders"))
        if current_holders is None or not series:
            return None
        candidates: list[_Signal] = []
        for label, seconds, minimum_percent, minimum_count in (
            ("1H", 60 * 60, 0.5, 5),
            ("6H", 6 * 60 * 60, 1.0, 10),
            ("24H", 24 * 60 * 60, 2.0, 20),
        ):
            previous = self._point_for_period(series, series[-1][0], seconds)
            previous_holders = self._nonnegative_int(previous.get("holders")) if previous else None
            if previous_holders is None or previous_holders <= 0:
                continue
            delta = current_holders - previous_holders
            change = delta / previous_holders * 100.0
            if delta < minimum_count or change < minimum_percent:
                continue
            candidates.append(
                _Signal(
                    ticker,
                    "holders",
                    "up",
                    min(88.0, 56.0 + change * 2.5 + min(12.0, delta / 10.0)),
                    f"Holder growth: +{delta:,} (+{change:.1f}%) in {label}",
                    change,
                    series[-1][0],
                )
            )
        return max(candidates, key=lambda item: item.score, default=None)

    def _large_buy_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
    ) -> _Signal | None:
        amount = self._positive_float(current.get("largest_buy_usd"))
        observed_at = self._positive_float(current.get("largest_buy_at"))
        current_at = self._positive_float(current.get("market_observed_at"))
        if amount is None or observed_at is None or current_at is None:
            return None
        age = max(0.0, current_at - observed_at)
        if age > 30 * 60:
            return None
        liquidity = self._positive_float(current.get("liquidity")) or 0.0
        volume_h1 = self._positive_float(current.get("volume_h1")) or 0.0
        threshold = max(500.0, min(25_000.0, max(liquidity * 0.003, volume_h1 * 0.05)))
        if amount < threshold:
            return None
        ratio = amount / threshold
        recency_bonus = max(0.0, 12.0 * (1.0 - age / (30 * 60)))
        score = min(100.0, 70.0 + min(18.0, max(0.0, ratio - 1.0) * 8.0) + recency_bonus)
        return _Signal(
            ticker,
            "large_buy",
            "up",
            score,
            f"Large buy: ${amount:,.0f}",
            amount,
            observed_at,
        )

    def _large_buy_signal_for_window(
        self,
        ticker: str,
        current: dict[str, Any],
        current_at: float,
    ) -> _Signal | None:
        cutoff = current_at - self.feed_window_seconds
        trades = [
            trade
            for trade in self._recent_buys.values()
            if trade["ticker"] == ticker
            and cutoff <= float(trade["observed_at"]) <= current_at + 30
        ]
        if not trades:
            return self._large_buy_signal(ticker, current)

        by_transaction: dict[str, dict[str, Any]] = {}
        for trade in trades:
            transaction_key = str(trade.get("tx_hash") or trade["trade_id"])
            previous = by_transaction.get(transaction_key)
            if previous is None or float(trade["amount_usd"]) > float(previous["amount_usd"]):
                by_transaction[transaction_key] = trade

        liquidity = self._positive_float(current.get("liquidity")) or 0.0
        volume_h1 = self._positive_float(current.get("volume_h1")) or 0.0
        threshold = max(500.0, min(25_000.0, max(liquidity * 0.003, volume_h1 * 0.05)))
        qualifying = [
            trade
            for trade in by_transaction.values()
            if float(trade["amount_usd"]) >= threshold
        ]
        if not qualifying:
            return None

        latest_trade = max(qualifying, key=lambda trade: float(trade["observed_at"]))
        largest = max(float(trade["amount_usd"]) for trade in qualifying)
        total = sum(float(trade["amount_usd"]) for trade in qualifying)
        ratio = largest / threshold
        recency = max(0.0, current_at - float(latest_trade["observed_at"]))
        recency_bonus = max(0.0, 10.0 * (1.0 - recency / self.feed_window_seconds))
        count_bonus = min(8.0, max(0, len(qualifying) - 1) * 2.0)
        score = min(
            100.0,
            70.0
            + min(18.0, max(0.0, ratio - 1.0) * 8.0)
            + recency_bonus
            + count_bonus,
        )
        detail = (
            f"Large buy: ${largest:,.0f}"
            if len(qualifying) == 1
            else f"{len(qualifying)} large buys: ${total:,.0f} total · largest ${largest:,.0f}"
        )
        return _Signal(
            ticker,
            "large_buy",
            "up",
            score,
            detail,
            largest,
            float(latest_trade["observed_at"]),
        )

    def _volume_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        series: list[tuple[float, dict[str, float | int | None]]],
    ) -> _Signal | None:
        current_volume = self._nonnegative_float(current.get("volume_m5"))
        if current_volume is None or not series:
            return None
        current_at = series[-1][0]
        prior_volumes = [
            volume
            for timestamp, data in series[:-1]
            if current_at - 2 * 60 * 60 <= timestamp <= current_at - 10 * 60
            and (volume := self._positive_float(data.get("volume_m5"))) is not None
        ]
        baseline = statistics.median(prior_volumes) if len(prior_volumes) >= 3 else None
        volume_h1 = self._positive_float(current.get("volume_h1"))
        if baseline is None and volume_h1 is not None and volume_h1 > current_volume:
            baseline = (volume_h1 - current_volume) / 11.0
        if baseline is None:
            return None
        baseline = max(100.0, baseline)
        ratio = current_volume / baseline
        liquidity = self._positive_float(current.get("liquidity")) or 0.0
        minimum_volume = max(500.0, liquidity * 0.001)
        if current_volume < minimum_volume or not (
            ratio >= 3.0 or (current_volume >= 10_000 and ratio >= 2.0)
        ):
            return None
        score = min(96.0, 62.0 + min(24.0, max(0.0, ratio - 2.0) * 7.0))
        return _Signal(
            ticker,
            "volume",
            "up",
            score,
            f"Volume spike: {self._format_money(current_volume)} in 5M · {ratio:.1f}× recent pace",
            ratio,
            self._positive_float(current.get("market_observed_at")) or current_at,
        )

    def _buy_pressure_signal(
        self,
        ticker: str,
        current: dict[str, float | int | None],
        current_at: float,
    ) -> _Signal | None:
        buys = self._nonnegative_int(current.get("buys_m5"))
        sells = self._nonnegative_int(current.get("sells_m5"))
        volume = self._nonnegative_float(current.get("volume_m5"))
        if buys is None or sells is None or volume is None:
            return None
        if buys < 6 or volume < 500 or buys < max(sells + 4, math.ceil(sells * 1.8)):
            return None
        ratio = buys / max(1, sells)
        score = min(88.0, 56.0 + min(20.0, (ratio - 1.0) * 8.0) + min(12.0, buys / 3.0))
        return _Signal(
            ticker,
            "buy_pressure",
            "up",
            score,
            f"Buy pressure: {buys} buys vs {sells} sells in 5M",
            ratio,
            self._positive_float(current.get("market_observed_at")) or current_at,
        )

    def _group_and_rank(
        self,
        signals: list[_Signal],
        chart_urls: dict[str, str] | None = None,
    ) -> tuple[PulseEvent, ...]:
        grouped: dict[str, list[_Signal]] = {}
        for signal in signals:
            if signal.score >= 43.0:
                grouped.setdefault(signal.ticker, []).append(signal)

        events: list[PulseEvent] = []
        for ticker, coin_signals in grouped.items():
            ordered = sorted(coin_signals, key=lambda item: item.score, reverse=True)
            primary = ordered[0]
            selected = [primary]
            seen_kinds = {primary.kind}
            for signal in ordered[1:]:
                if signal.kind in seen_kinds or signal.score < 50.0:
                    continue
                selected.append(signal)
                seen_kinds.add(signal.kind)
                if len(selected) == 4:
                    break
            events.append(
                PulseEvent(
                    ticker=ticker,
                    emoji=self._emoji_for(primary),
                    score=primary.score + sum(signal.score * 0.12 for signal in selected[1:]),
                    details=tuple(signal.detail for signal in selected),
                    signal_types=tuple(signal.kind for signal in selected),
                    observed_at=max(
                        (
                            signal.observed_at
                            for signal in selected
                            if signal.observed_at is not None
                        ),
                        default=None,
                    ),
                    direction=primary.direction,
                    chart_url=(chart_urls or {}).get(ticker, ""),
                )
            )

        ranked = sorted(events, key=lambda item: (-item.score, item.ticker))
        price_events = [event for event in ranked if "price" in event.signal_types]
        if len(price_events) >= 2:
            leader = price_events[0]
            ranked = [
                PulseEvent(
                    ticker=event.ticker,
                    emoji=event.emoji,
                    score=event.score,
                    details=event.details + (("Strongest recorded move",) if event.ticker == leader.ticker else ()),
                    signal_types=event.signal_types,
                    observed_at=event.observed_at,
                    direction=event.direction,
                    chart_url=event.chart_url,
                )
                for event in ranked
            ]
        selected = ranked[: self.max_events]
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.observed_at is None,
                    -(item.observed_at or 0.0),
                    -item.score,
                    item.ticker,
                ),
            )
        )

    def _series_by_ticker(self) -> dict[str, list[tuple[float, dict[str, float | int | None]]]]:
        result: dict[str, list[tuple[float, dict[str, float | int | None]]]] = {}
        for snapshot in self._snapshots:
            timestamp = float(snapshot["timestamp"])
            for ticker, data in snapshot["coins"].items():
                if ticker in self.supported_tickers and isinstance(data, dict):
                    result.setdefault(ticker, []).append((timestamp, data))
        return result

    def _point_for_period(
        self,
        series: list[tuple[float, dict[str, float | int | None]]],
        current_at: float,
        seconds: int,
    ) -> dict[str, float | int | None] | None:
        target = current_at - seconds
        timestamps = [item[0] for item in series]
        index = bisect.bisect_left(timestamps, target)
        choices = [candidate for candidate in (index - 1, index) if 0 <= candidate < len(series) - 1]
        if not choices:
            return None
        best = min(choices, key=lambda candidate: abs(timestamps[candidate] - target))
        tolerance = max(90.0, seconds * 0.20)
        if abs(timestamps[best] - target) > tolerance:
            return None
        return series[best][1]

    def _typical_move(
        self,
        series: list[tuple[float, dict[str, float | int | None]]],
        seconds: int,
    ) -> float | None:
        if len(series) < 8:
            return None
        samples: list[float] = []
        step = max(1, len(series) // 16)
        for index in range(step, len(series) - 1, step):
            end_at, end_data = series[index]
            start_data = self._point_for_period(series[: index + 1], end_at, seconds)
            end_price = self._positive_float(end_data.get("price"))
            start_price = self._positive_float(start_data.get("price")) if start_data else None
            if end_price is None or start_price is None:
                continue
            samples.append(abs(self._percentage_change(end_price, start_price)))
        return statistics.median(samples) if len(samples) >= 4 else None

    def _coverage_status(self, latest_at: float) -> tuple[bool, int]:
        cutoff = latest_at - self.feed_window_seconds
        partial_snapshot_present = any(
            cutoff <= float(snapshot["timestamp"]) <= latest_at + 30
            and self._snapshot_has_market_data(snapshot)
            and snapshot.get("market_complete") is False
            for snapshot in self._snapshots
        )
        timestamps = sorted(
            {
                float(snapshot["timestamp"])
                for snapshot in self._snapshots
                if cutoff <= float(snapshot["timestamp"]) <= latest_at + 30
                and self._snapshot_has_market_data(snapshot)
                and snapshot.get("market_complete") is not False
            }
        )
        if not timestamps:
            return False, 0
        covered_seconds = max(0.0, latest_at - timestamps[0])
        coverage_minutes = min(
            self.feed_window_seconds // 60,
            int(covered_seconds // 60),
        )
        starts_on_time = timestamps[0] <= cutoff + self.coverage_gap_seconds
        gaps_are_complete = all(
            later - earlier <= self.coverage_gap_seconds
            for earlier, later in zip(timestamps, timestamps[1:])
        )
        return starts_on_time and gaps_are_complete and not partial_snapshot_present, coverage_minutes

    def _market_summary(
        self,
        current_coins: dict[str, Any],
        latest_at: float,
    ) -> tuple[str, ...]:
        changes: list[tuple[str, float]] = []
        total_buys = 0
        total_sells = 0
        total_volume = 0.0
        activity_available = False
        for ticker, data in current_coins.items():
            if ticker in PRICE_ONLY_TICKERS or not isinstance(data, dict):
                continue
            change = self._finite_float(data.get("change_h1"))
            if change is not None:
                changes.append((ticker, change))
            buys = self._nonnegative_int(data.get("buys_h1"))
            sells = self._nonnegative_int(data.get("sells_h1"))
            volume = self._nonnegative_float(data.get("volume_h1"))
            if buys is not None:
                total_buys += buys
                activity_available = True
            if sells is not None:
                total_sells += sells
                activity_available = True
            if volume is not None:
                total_volume += volume
                activity_available = True

        rows: list[str] = []
        if changes:
            rising = sum(1 for _, change in changes if change > 0.05)
            falling = sum(1 for _, change in changes if change < -0.05)
            flat = len(changes) - rising - falling
            rows.append(f"Market breadth: {rising} rising · {falling} falling · {flat} flat")
            strongest = max(changes, key=lambda item: item[1])
            weakest = min(changes, key=lambda item: item[1])
            rows.append(
                f"Strongest: {strongest[0]} {strongest[1]:+.1f}% · "
                f"Weakest: {weakest[0]} {weakest[1]:+.1f}%"
            )
        if activity_available:
            rows.append(
                f"Recent activity: {self._format_money(total_volume)} volume · "
                f"{total_buys:,} buys vs {total_sells:,} sells"
            )

        recent_trades = [
            trade
            for trade in self._recent_buys.values()
            if latest_at - self.feed_window_seconds
            <= float(trade["observed_at"])
            <= latest_at + 30
        ]
        if recent_trades:
            largest_trade = max(recent_trades, key=lambda trade: float(trade["amount_usd"]))
            rows.append(
                f"Largest observed buy: ${float(largest_trade['amount_usd']):,.0f} "
                f"in {largest_trade['ticker']}"
            )
        return tuple(rows)

    def _normalize_recent_buys(
        self,
        values: Iterable[Any],
        timestamp: float,
    ) -> tuple[dict[str, Any], ...]:
        normalized: dict[str, dict[str, Any]] = {}
        cutoff = timestamp - self.retention_seconds
        for value in values:
            ticker = str(getattr(value, "ticker", "") or "").upper()
            if ticker not in self.supported_tickers or ticker in PRICE_ONLY_TICKERS:
                continue
            for trade in tuple(getattr(value, "recent_buys", ()) or ()):
                trade_id = str(getattr(trade, "trade_id", "") or "").strip()
                amount = self._positive_float(getattr(trade, "amount_usd", None))
                observed_at = self._positive_float(getattr(trade, "observed_at", None))
                if (
                    not trade_id
                    or amount is None
                    or observed_at is None
                    or observed_at < cutoff
                    or observed_at > timestamp + 30
                ):
                    continue
                tx_hash = str(getattr(trade, "tx_hash", "") or "").strip()[:160]
                pool_address = str(getattr(trade, "pool_address", "") or "").strip()[:160]
                normalized[trade_id] = {
                    "ticker": ticker,
                    "trade_id": trade_id[:240],
                    "amount_usd": amount,
                    "observed_at": observed_at,
                    "pool_address": pool_address,
                    "tx_hash": tx_hash,
                }
        return tuple(normalized.values())

    def _merge_recent_buys(
        self,
        recent_buys: Iterable[dict[str, Any]],
        timestamp: float,
    ) -> bool:
        changed = False
        for trade in recent_buys:
            trade_id = str(trade["trade_id"])
            previous = self._recent_buys.get(trade_id)
            if previous != trade:
                self._recent_buys[trade_id] = dict(trade)
                changed = True
        cutoff = timestamp - self.retention_seconds
        retained = {
            trade_id: trade
            for trade_id, trade in self._recent_buys.items()
            if float(trade.get("observed_at") or 0) >= cutoff
        }
        if len(retained) != len(self._recent_buys):
            self._recent_buys = retained
            changed = True
        return changed

    def _normalize_values(self, values: Iterable[Any]) -> dict[str, dict[str, float | int | None]]:
        coins: dict[str, dict[str, float | int | None]] = {}
        for value in values:
            ticker = str(getattr(value, "ticker", "") or "").upper()
            if ticker not in self.supported_tickers:
                continue
            price = self._positive_float(getattr(value, "price", None))
            if price is None:
                continue
            coins[ticker] = {
                "price": price,
                "change_24h": self._finite_float(getattr(value, "change_24h", None)),
                "market_cap": self._positive_float(getattr(value, "market_cap", None)),
                "holders": self._nonnegative_int(getattr(value, "holders", None)),
                "liquidity": self._positive_float(getattr(value, "liquidity", None)),
                "change_m5": self._finite_float(getattr(value, "change_m5", None)),
                "change_h1": self._finite_float(getattr(value, "change_h1", None)),
                "change_h6": self._finite_float(getattr(value, "change_h6", None)),
                "volume_m5": self._nonnegative_float(getattr(value, "volume_m5", None)),
                "volume_h1": self._nonnegative_float(getattr(value, "volume_h1", None)),
                "volume_h24": self._nonnegative_float(getattr(value, "volume_h24", None)),
                "buys_m5": self._nonnegative_int(getattr(value, "buys_m5", None)),
                "sells_m5": self._nonnegative_int(getattr(value, "sells_m5", None)),
                "buys_h1": self._nonnegative_int(getattr(value, "buys_h1", None)),
                "sells_h1": self._nonnegative_int(getattr(value, "sells_h1", None)),
                "largest_buy_usd": self._positive_float(
                    getattr(value, "largest_buy_usd", None)
                ),
                "largest_buy_at": self._positive_float(getattr(value, "largest_buy_at", None)),
                "market_observed_at": self._positive_float(
                    getattr(value, "market_observed_at", None)
                ),
                "chart_url": self._safe_chart_url(getattr(value, "chart_url", "")),
            }
        return coins

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return []
        raw_snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
        if not isinstance(raw_snapshots, list):
            return []
        snapshots: list[dict[str, Any]] = []
        for raw in raw_snapshots:
            if not isinstance(raw, dict) or not isinstance(raw.get("coins"), dict):
                continue
            timestamp = self._finite_float(raw.get("timestamp"))
            if timestamp is None or timestamp <= 0:
                continue
            coins: dict[str, dict[str, float | int | None]] = {}
            for raw_ticker, data in raw["coins"].items():
                ticker = str(raw_ticker).upper()
                if ticker not in self.supported_tickers or not isinstance(data, dict):
                    continue
                price = self._positive_float(data.get("price"))
                if price is None:
                    continue
                coins[ticker] = {
                    "price": price,
                    "change_24h": self._finite_float(data.get("change_24h")),
                    "market_cap": self._positive_float(data.get("market_cap")),
                    "holders": self._nonnegative_int(data.get("holders")),
                    "liquidity": self._positive_float(data.get("liquidity")),
                    "change_m5": self._finite_float(data.get("change_m5")),
                    "change_h1": self._finite_float(data.get("change_h1")),
                    "change_h6": self._finite_float(data.get("change_h6")),
                    "volume_m5": self._nonnegative_float(data.get("volume_m5")),
                    "volume_h1": self._nonnegative_float(data.get("volume_h1")),
                    "volume_h24": self._nonnegative_float(data.get("volume_h24")),
                    "buys_m5": self._nonnegative_int(data.get("buys_m5")),
                    "sells_m5": self._nonnegative_int(data.get("sells_m5")),
                    "buys_h1": self._nonnegative_int(data.get("buys_h1")),
                    "sells_h1": self._nonnegative_int(data.get("sells_h1")),
                    "largest_buy_usd": self._positive_float(data.get("largest_buy_usd")),
                    "largest_buy_at": self._positive_float(data.get("largest_buy_at")),
                    "market_observed_at": self._positive_float(
                        data.get("market_observed_at")
                    ),
                    "chart_url": self._safe_chart_url(data.get("chart_url")),
                }
            if coins:
                snapshots.append(
                    {
                        "timestamp": timestamp,
                        "coins": coins,
                        "market_complete": (
                            bool(raw.get("market_complete"))
                            if raw.get("market_complete") is not None
                            else None
                        ),
                    }
                )
        snapshots.sort(key=lambda item: item["timestamp"])
        return snapshots

    def _load_buys(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.buy_history_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return {}
        raw_buys = payload.get("buys") if isinstance(payload, dict) else None
        if not isinstance(raw_buys, list):
            return {}
        buys: dict[str, dict[str, Any]] = {}
        for raw in raw_buys:
            if not isinstance(raw, dict):
                continue
            ticker = str(raw.get("ticker") or "").upper()
            trade_id = str(raw.get("trade_id") or "").strip()
            amount = self._positive_float(raw.get("amount_usd"))
            observed_at = self._positive_float(raw.get("observed_at"))
            if (
                ticker not in self.supported_tickers
                or ticker in PRICE_ONLY_TICKERS
                or not trade_id
                or amount is None
                or observed_at is None
            ):
                continue
            buys[trade_id] = {
                "ticker": ticker,
                "trade_id": trade_id[:240],
                "amount_usd": amount,
                "observed_at": observed_at,
                "pool_address": str(raw.get("pool_address") or "")[:160],
                "tx_hash": str(raw.get("tx_hash") or "")[:160],
            }
        return buys

    def _save_buys(self) -> None:
        self.buy_history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.buy_history_path.with_suffix(self.buy_history_path.suffix + ".tmp")
        ordered = sorted(
            self._recent_buys.values(),
            key=lambda trade: (float(trade["observed_at"]), str(trade["trade_id"])),
        )
        temporary.write_text(
            json.dumps({"version": 1, "buys": ordered}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.buy_history_path)

    def _save_history(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 3, "snapshots": self._snapshots}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.history_path)

    @staticmethod
    def _emoji_for(signal: _Signal) -> str:
        if signal.kind == "large_buy":
            return "🐋"
        if signal.kind == "volume":
            return "📊"
        if signal.kind == "buy_pressure":
            return "🟢"
        if signal.kind == "high":
            return "🚀"
        if signal.kind == "low":
            return "⚠️"
        if signal.kind == "market_cap":
            return "🏁"
        if signal.kind == "holders":
            return "👥"
        return "🔥" if signal.direction == "up" else "📉"

    @staticmethod
    def _format_money(value: float) -> str:
        for suffix, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
            if value >= divisor:
                number = value / divisor
                decimals = 0 if number >= 100 else 1 if number >= 10 else 2
                formatted = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
                return f"${formatted}{suffix}"
        return f"${value:,.0f}"

    @staticmethod
    def _percentage_change(current: float, previous: float) -> float:
        return (current / previous - 1.0) * 100.0

    @staticmethod
    def _valid_timestamp(value: float) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("Pulse timestamp must be a positive finite number")
        return parsed

    @staticmethod
    def _finite_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @classmethod
    def _positive_float(cls, value: Any) -> float | None:
        parsed = cls._finite_float(value)
        return parsed if parsed is not None and parsed > 0 else None

    @classmethod
    def _nonnegative_float(cls, value: Any) -> float | None:
        parsed = cls._finite_float(value)
        return parsed if parsed is not None and parsed >= 0 else None

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _safe_chart_url(value: Any) -> str:
        raw = str(value or "").strip()
        prefix = "https://www.geckoterminal.com/ton/pools/"
        suffix = raw.removeprefix(prefix)
        if not raw.startswith(prefix) or not suffix:
            return ""
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in suffix):
            return ""
        return raw
