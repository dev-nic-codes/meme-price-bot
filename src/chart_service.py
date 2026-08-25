from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .config import OUTPUT_DIR
from .token_report import KNOWN_TOKEN_ADDRESSES, TokenReportService


CHART_CACHE_PATH = OUTPUT_DIR / "dashboard_charts.json"


@dataclass(frozen=True)
class ChartSeries:
    ticker: str
    jetton_master: str
    pair_address: str
    points: tuple[float, ...]
    updated_at: float
    stale: bool = False


class DashboardChartService:
    """Fetch and persist last-good 24-hour dashboard chart series."""

    def __init__(
        self,
        token_report_service: TokenReportService,
        *,
        cache_seconds: int | None = None,
        concurrency: int = 3,
        cache_path: Path = CHART_CACHE_PATH,
    ) -> None:
        self.token_report_service = token_report_service
        self.cache_seconds = max(
            300,
            cache_seconds
            if cache_seconds is not None
            else int(os.getenv("DASHBOARD_CHART_CACHE_SECONDS", "1800")),
        )
        self.concurrency = max(1, min(5, concurrency))
        self.cache_path = cache_path

    async def fetch_series(self, tickers: list[str]) -> dict[str, ChartSeries]:
        now = time.time()
        cached = self._load_cache(self.cache_path)
        requested = [ticker.upper() for ticker in tickers]
        result: dict[str, ChartSeries] = {}
        stale_tickers: list[str] = []

        for ticker in requested:
            item = cached.get(ticker)
            if item is not None and now - item.updated_at < self.cache_seconds:
                result[ticker] = item
            else:
                stale_tickers.append(ticker)

        if stale_tickers:
            semaphore = asyncio.Semaphore(self.concurrency)
            fetched = await asyncio.gather(
                *(self._fetch_one(semaphore, ticker) for ticker in stale_tickers),
                return_exceptions=True,
            )
            for ticker, item in zip(stale_tickers, fetched):
                if isinstance(item, ChartSeries) and len(item.points) >= 2:
                    result[ticker] = item
                    cached[ticker] = item
                    continue
                previous = cached.get(ticker)
                if previous is not None and len(previous.points) >= 2:
                    result[ticker] = ChartSeries(
                        ticker=previous.ticker,
                        jetton_master=previous.jetton_master,
                        pair_address=previous.pair_address,
                        points=previous.points,
                        updated_at=previous.updated_at,
                        stale=True,
                    )

        if cached:
            self._save_cache(cached, self.cache_path)
        return result

    async def _fetch_one(
        self,
        semaphore: asyncio.Semaphore,
        ticker: str,
    ) -> ChartSeries | None:
        jetton_master = KNOWN_TOKEN_ADDRESSES.get(ticker.lower())
        if not jetton_master:
            return None

        async with semaphore:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    pair = await self.token_report_service.best_pair_for_token(jetton_master)
                    if pair is None:
                        raise RuntimeError("no eligible TON liquidity pool found")
                    rows = await self.token_report_service.fetch_ohlcv(
                        pair.pair_address,
                        "hour",
                        {
                            "aggregate": "1",
                            "limit": "24",
                            "currency": "usd",
                            "token": "base",
                            "include_empty_intervals": "true",
                        },
                    )
                    points = self.close_points(rows)
                    if len(points) < 2:
                        raise RuntimeError("provider returned insufficient hourly history")
                    return ChartSeries(
                        ticker=ticker,
                        jetton_master=jetton_master,
                        pair_address=pair.pair_address,
                        points=points,
                        updated_at=time.time(),
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt * 2)
            if last_error is not None:
                print(
                    f"Dashboard chart fetch failed for {ticker}: "
                    f"{type(last_error).__name__}: {last_error}",
                    flush=True,
                )
        return None

    @staticmethod
    def close_points(rows: list[list[float]]) -> tuple[float, ...]:
        by_timestamp: dict[float, float] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            try:
                timestamp = float(row[0])
                close = float(row[4])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp) or not math.isfinite(close) or close <= 0:
                continue
            by_timestamp[timestamp] = close
        return tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))

    @staticmethod
    def _load_cache(path: Path) -> dict[str, ChartSeries]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}

        result: dict[str, ChartSeries] = {}
        for ticker, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            try:
                points = tuple(float(point) for point in raw.get("points", []))
                updated_at = float(raw.get("updated_at"))
            except (TypeError, ValueError):
                continue
            if len(points) < 2 or not all(math.isfinite(point) and point > 0 for point in points):
                continue
            key = str(ticker).upper()
            result[key] = ChartSeries(
                ticker=key,
                jetton_master=str(raw.get("jetton_master") or ""),
                pair_address=str(raw.get("pair_address") or ""),
                points=points,
                updated_at=updated_at,
            )
        return result

    @staticmethod
    def _save_cache(values: dict[str, ChartSeries], path: Path) -> None:
        payload: dict[str, dict[str, Any]] = {
            ticker: {
                "jetton_master": item.jetton_master,
                "pair_address": item.pair_address,
                "points": list(item.points),
                "updated_at": item.updated_at,
            }
            for ticker, item in values.items()
            if len(item.points) >= 2
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            print(
                f"Dashboard chart cache save failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
