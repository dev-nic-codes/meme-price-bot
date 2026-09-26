from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

from .token_report import KNOWN_TOKEN_ADDRESSES, same_ton_address


DEX_TOKEN_BATCH_URL = "https://api.dexscreener.com/tokens/v1/ton/{addresses}"
GECKO_POOL_TRADES_URL = (
    "https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool_address}/trades"
)


@dataclass(frozen=True)
class PulseMarketTrade:
    trade_id: str
    amount_usd: float
    observed_at: float
    pool_address: str
    tx_hash: str = ""


@dataclass(frozen=True)
class PulseMarketObservation:
    ticker: str
    token_address: str
    pair_address: str
    observed_at: float
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    change_m5: float | None
    change_h1: float | None
    change_h6: float | None
    change_h24: float | None
    volume_m5_usd: float | None
    volume_h1_usd: float | None
    volume_h24_usd: float | None
    buys_m5: int | None
    sells_m5: int | None
    buys_h1: int | None
    sells_h1: int | None
    largest_buy_usd: float | None = None
    largest_buy_at: float | None = None
    pool_addresses: tuple[str, ...] = ()
    recent_buys: tuple[PulseMarketTrade, ...] = ()
    trade_coverage_complete: bool = False

    @property
    def chart_url(self) -> str:
        return f"https://www.geckoterminal.com/ton/pools/{self.pair_address}"


@dataclass(frozen=True)
class PulseMarketSnapshot:
    updated_at: float
    observations: tuple[PulseMarketObservation, ...]
    coverage_complete: bool = True
    missing_tickers: tuple[str, ...] = ()


class PulseMarketService:
    """Load a current DEX-wide pulse snapshot for the bot's tracked tokens."""

    def __init__(
        self,
        session_provider: Callable[[], Awaitable[aiohttp.ClientSession]],
        *,
        cache_seconds: int = 60,
        stale_seconds: int = 180,
        trade_concurrency: int = 3,
        token_addresses: dict[str, str] | None = None,
    ) -> None:
        self.session_provider = session_provider
        self.cache_seconds = max(20, int(cache_seconds))
        self.stale_seconds = max(self.cache_seconds, int(stale_seconds))
        self.trade_concurrency = max(1, min(5, int(trade_concurrency)))
        source_addresses = token_addresses or KNOWN_TOKEN_ADDRESSES
        self.token_addresses = {
            str(ticker).upper(): str(address).strip()
            for ticker, address in source_addresses.items()
            if str(ticker).strip() and str(address).strip()
        }
        self._snapshot: PulseMarketSnapshot | None = None
        self._cache_deadline = 0.0
        self._lock = asyncio.Lock()

    async def current(self, *, force: bool = False) -> PulseMarketSnapshot:
        if not force and self._snapshot is not None and time.monotonic() < self._cache_deadline:
            return self._snapshot
        async with self._lock:
            if not force and self._snapshot is not None and time.monotonic() < self._cache_deadline:
                return self._snapshot
            try:
                snapshot = await self._refresh()
            except Exception:
                if (
                    self._snapshot is not None
                    and time.time() - self._snapshot.updated_at <= self.stale_seconds
                ):
                    return self._snapshot
                raise
            self._snapshot = snapshot
            self._cache_deadline = time.monotonic() + self.cache_seconds
            return snapshot

    async def _refresh(self) -> PulseMarketSnapshot:
        session = await self.session_provider()
        observed_at = time.time()
        address_list = ",".join(self.token_addresses.values())
        payload = await self._get_json(
            session,
            DEX_TOKEN_BATCH_URL.format(addresses=address_list),
            retries=2,
        )
        observations = parse_market_pairs(payload, self.token_addresses, observed_at=observed_at)
        minimum_coverage = max(1, math.ceil(len(self.token_addresses) * 0.6))
        if len(observations) < minimum_coverage:
            raise RuntimeError(
                f"DexScreener returned only {len(observations)} of "
                f"{len(self.token_addresses)} tracked pulse markets"
            )

        semaphore = asyncio.Semaphore(self.trade_concurrency)
        enriched = await asyncio.gather(
            *(
                self._with_recent_trades(session, semaphore, observation, observed_at)
                for observation in observations
            ),
            return_exceptions=True,
        )
        completed = [
            result if isinstance(result, PulseMarketObservation) else observation
            for observation, result in zip(observations, enriched)
        ]
        completed.sort(key=lambda item: item.ticker)
        observed_tickers = {item.ticker for item in completed}
        missing_tickers = tuple(sorted(set(self.token_addresses) - observed_tickers))
        return PulseMarketSnapshot(
            updated_at=observed_at,
            observations=tuple(completed),
            coverage_complete=(
                not missing_tickers
                and all(item.trade_coverage_complete for item in completed)
            ),
            missing_tickers=missing_tickers,
        )

    async def _with_recent_trades(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        observation: PulseMarketObservation,
        now: float,
    ) -> PulseMarketObservation:
        pool_addresses = observation.pool_addresses or (observation.pair_address,)
        pool_addresses = tuple(address for address in pool_addresses if address)
        if not pool_addresses:
            return observation
        results = await asyncio.gather(
            *(
                self._recent_buys_for_pool(session, semaphore, pool_address, now)
                for pool_address in pool_addresses
            ),
            return_exceptions=True,
        )
        by_id: dict[str, PulseMarketTrade] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            for trade in result:
                current = by_id.get(trade.trade_id)
                if current is None or trade.amount_usd > current.amount_usd:
                    by_id[trade.trade_id] = trade
        recent_buys = tuple(
            sorted(by_id.values(), key=lambda trade: (trade.observed_at, trade.amount_usd))
        )
        largest = max(recent_buys, key=lambda trade: trade.amount_usd, default=None)
        return replace(
            observation,
            largest_buy_usd=largest.amount_usd if largest is not None else None,
            largest_buy_at=largest.observed_at if largest is not None else None,
            recent_buys=recent_buys,
            trade_coverage_complete=all(not isinstance(result, Exception) for result in results),
        )

    async def _recent_buys_for_pool(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        pool_address: str,
        now: float,
    ) -> tuple[PulseMarketTrade, ...]:
        async with semaphore:
            payload = await self._get_json(
                session,
                GECKO_POOL_TRADES_URL.format(pool_address=pool_address),
                params={"trade_volume_in_usd_greater_than": "100"},
                retries=1,
            )
        return parse_recent_buys(payload, now=now, pool_address=pool_address)

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: dict[str, str] | None = None,
        retries: int,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=12, sock_connect=4, sock_read=9)
        for attempt in range(retries):
            async with session.get(
                url,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "memesbot-pulse/2.0"},
                timeout=timeout,
            ) as response:
                if response.status not in {429, 502, 503, 504}:
                    response.raise_for_status()
                    return await response.json()
                await response.read()
            if attempt + 1 < retries:
                await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"Pulse market provider request failed: {url}")


def parse_market_pairs(
    payload: Any,
    token_addresses: dict[str, str],
    *,
    observed_at: float,
) -> tuple[PulseMarketObservation, ...]:
    if not isinstance(payload, list):
        return ()
    candidates: dict[str, list[tuple[float, PulseMarketObservation]]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        base = item.get("baseToken") if isinstance(item.get("baseToken"), dict) else {}
        base_address = str(base.get("address") or "").strip()
        ticker = next(
            (
                candidate_ticker
                for candidate_ticker, expected_address in token_addresses.items()
                if same_ton_address(base_address, expected_address)
            ),
            "",
        )
        if not ticker:
            continue
        pair_address = str(item.get("pairAddress") or "").strip()
        price = _positive_float(item.get("priceUsd"))
        if not pair_address or price is None:
            continue
        changes = item.get("priceChange") if isinstance(item.get("priceChange"), dict) else {}
        volumes = item.get("volume") if isinstance(item.get("volume"), dict) else {}
        transactions = item.get("txns") if isinstance(item.get("txns"), dict) else {}
        liquidity = _positive_float(
            (item.get("liquidity") or {}).get("usd")
            if isinstance(item.get("liquidity"), dict)
            else None
        )
        observation = PulseMarketObservation(
            ticker=ticker,
            token_address=token_addresses[ticker],
            pair_address=pair_address,
            observed_at=observed_at,
            price_usd=price,
            market_cap_usd=_positive_float(item.get("marketCap")),
            liquidity_usd=liquidity,
            change_m5=_finite_float(changes.get("m5")),
            change_h1=_finite_float(changes.get("h1")),
            change_h6=_finite_float(changes.get("h6")),
            change_h24=_finite_float(changes.get("h24")),
            volume_m5_usd=_nonnegative_float(volumes.get("m5")),
            volume_h1_usd=_nonnegative_float(volumes.get("h1")),
            volume_h24_usd=_nonnegative_float(volumes.get("h24")),
            buys_m5=_transaction_count(transactions, "m5", "buys"),
            sells_m5=_transaction_count(transactions, "m5", "sells"),
            buys_h1=_transaction_count(transactions, "h1", "buys"),
            sells_h1=_transaction_count(transactions, "h1", "sells"),
        )
        quality = (liquidity or 0.0) + (observation.volume_h24_usd or 0.0) * 0.05
        candidates.setdefault(ticker, []).append((quality, observation))

    observations: list[PulseMarketObservation] = []
    for ticker, ticker_candidates in candidates.items():
        ordered = sorted(ticker_candidates, key=lambda item: item[0], reverse=True)
        primary = ordered[0][1]
        primary_liquidity = primary.liquidity_usd or 0.0
        minimum_secondary_liquidity = max(5_000.0, primary_liquidity * 0.05)
        meaningful = [primary]
        for _, candidate in ordered[1:]:
            if len(meaningful) >= 2:
                break
            if (candidate.liquidity_usd or 0.0) >= minimum_secondary_liquidity:
                meaningful.append(candidate)
        observations.append(
            replace(
                primary,
                liquidity_usd=_sum_optional(item.liquidity_usd for item in meaningful),
                volume_m5_usd=_sum_optional(item.volume_m5_usd for item in meaningful),
                volume_h1_usd=_sum_optional(item.volume_h1_usd for item in meaningful),
                volume_h24_usd=_sum_optional(item.volume_h24_usd for item in meaningful),
                buys_m5=_sum_optional_int(item.buys_m5 for item in meaningful),
                sells_m5=_sum_optional_int(item.sells_m5 for item in meaningful),
                buys_h1=_sum_optional_int(item.buys_h1 for item in meaningful),
                sells_h1=_sum_optional_int(item.sells_h1 for item in meaningful),
                pool_addresses=tuple(item.pair_address for item in meaningful),
            )
        )
    observations.sort(key=lambda item: item.ticker)
    return tuple(observations)


def parse_recent_buys(
    payload: Any,
    *,
    now: float,
    pool_address: str,
) -> tuple[PulseMarketTrade, ...]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ()
    trades: dict[str, PulseMarketTrade] = {}
    for item in rows:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attributes, dict) or str(attributes.get("kind") or "").casefold() != "buy":
            continue
        amount = _positive_float(attributes.get("volume_in_usd"))
        timestamp = _parse_timestamp(attributes.get("block_timestamp"))
        if amount is None or timestamp is None or timestamp > now + 30 or now - timestamp > 65 * 60:
            continue
        tx_hash = str(attributes.get("tx_hash") or "").strip()
        source_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        trade_id = source_id or f"{tx_hash}:{pool_address}:{timestamp:.0f}:{amount:.8f}"
        if not trade_id.strip(":"):
            continue
        trade = PulseMarketTrade(
            trade_id=trade_id,
            amount_usd=amount,
            observed_at=timestamp,
            pool_address=pool_address,
            tx_hash=tx_hash,
        )
        current = trades.get(trade_id)
        if current is None or trade.amount_usd > current.amount_usd:
            trades[trade_id] = trade
    return tuple(sorted(trades.values(), key=lambda trade: (trade.observed_at, trade.amount_usd)))


def parse_recent_largest_buy(payload: Any, *, now: float) -> tuple[float | None, float | None]:
    trades = tuple(
        trade
        for trade in parse_recent_buys(payload, now=now, pool_address="")
        if now - trade.observed_at <= 30 * 60
    )
    largest = max(trades, key=lambda trade: trade.amount_usd, default=None)
    if largest is None:
        return None, None
    return largest.amount_usd, largest.observed_at


def _sum_optional(values: Any) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _sum_optional_int(values: Any) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _transaction_count(payload: dict[str, Any], timeframe: str, side: str) -> int | None:
    bucket = payload.get(timeframe) if isinstance(payload.get(timeframe), dict) else {}
    return _nonnegative_int(bucket.get(side))


def _parse_timestamp(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_float(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
