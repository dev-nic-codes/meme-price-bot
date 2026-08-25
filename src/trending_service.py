from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import aiohttp


TRENDING_URL = "https://api.geckoterminal.com/api/v2/networks/ton/trending_pools"
TRENDING_PAGE_SIZE = 20
TRENDING_MAX_PAGES = 5
DEFAULT_BLOCKED_SYMBOLS = frozenset({"67", "FRT"})
DEFAULT_BLOCKED_ADDRESSES = frozenset(
    {
        "eqdtbhs52jxa9s7p3xusvcohgyqbklqdjqlq98a3nld7qnmp",
        "eqa1eidrr33zgl21rwdifgo7h4etwieentuvg7jit-3ap5gg",
    }
)
EXCLUDED_SYMBOLS = {
    "TON",
    "WTON",
    "GRAM",
    "USDT",
    "USD₮",
    "USDC",
    "JUSDT",
    "JUSDC",
    "TSTON",
    "STTON",
    "NOT",
}
EXCLUDED_NAME_PARTS = (
    "wrapped ton",
    "tether usd",
    "usd coin",
    "liquid staking",
    "staked ton",
)


@dataclass(frozen=True)
class TrendingCoin:
    token_address: str
    name: str
    symbol: str
    change_24h: float
    liquidity_usd: float
    pool_address: str = ""

    @property
    def chart_url(self) -> str:
        if self.pool_address:
            return f"https://www.geckoterminal.com/ton/pools/{self.pool_address}"
        return f"https://www.geckoterminal.com/ton/tokens/{self.token_address}"


@dataclass(frozen=True)
class TrendingSnapshot:
    updated_at: datetime
    coins: tuple[TrendingCoin, ...]


class TrendingService:
    def __init__(
        self,
        session_provider: Callable[[], Awaitable[aiohttp.ClientSession]],
        cache_path: Path,
        *,
        refresh_seconds: int = 3_600,
        minimum_liquidity_usd: float = 10_000,
        result_limit: int = 10,
        blocked_symbols: Iterable[str] = DEFAULT_BLOCKED_SYMBOLS,
        blocked_addresses: Iterable[str] = DEFAULT_BLOCKED_ADDRESSES,
    ) -> None:
        self.session_provider = session_provider
        self.cache_path = cache_path
        self.refresh_seconds = max(3_600, int(refresh_seconds))
        self.minimum_liquidity_usd = max(0.0, float(minimum_liquidity_usd))
        self.result_limit = max(1, min(20, int(result_limit)))
        self.blocked_symbols = frozenset(
            str(value).strip().upper() for value in blocked_symbols if str(value).strip()
        )
        self.blocked_addresses = frozenset(
            str(value).strip().casefold() for value in blocked_addresses if str(value).strip()
        )
        self.refresh_lock = asyncio.Lock()
        self.snapshot = self._load_cache()

    def current(self) -> TrendingSnapshot | None:
        return self.snapshot

    def refresh_due(self, now: datetime | None = None) -> bool:
        if self.snapshot is None:
            return True
        current_time = now or datetime.now(timezone.utc)
        age = (current_time - self.snapshot.updated_at).total_seconds()
        return age >= self.refresh_seconds

    async def refresh_if_due(self) -> TrendingSnapshot:
        if not self.refresh_due() and self.snapshot is not None:
            return self.snapshot
        return await self.refresh()

    async def refresh(self, *, force: bool = False) -> TrendingSnapshot:
        async with self.refresh_lock:
            if not force and not self.refresh_due() and self.snapshot is not None:
                return self.snapshot

            session = await self.session_provider()
            coins: list[TrendingCoin] = []
            seen_addresses: set[str] = set()
            for page in range(1, TRENDING_MAX_PAGES + 1):
                payload = await self._fetch_page(session, page)
                page_coins = self.parse_payload(
                    payload,
                    minimum_liquidity_usd=self.minimum_liquidity_usd,
                    result_limit=TRENDING_PAGE_SIZE,
                    blocked_symbols=self.blocked_symbols,
                    blocked_addresses=self.blocked_addresses,
                )
                for coin in page_coins:
                    normalized_address = coin.token_address.casefold()
                    if normalized_address in seen_addresses:
                        continue
                    seen_addresses.add(normalized_address)
                    coins.append(coin)
                    if len(coins) >= self.result_limit:
                        break
                if len(coins) >= self.result_limit:
                    break
                if len(payload.get("data") or []) < TRENDING_PAGE_SIZE:
                    break
                await asyncio.sleep(1)
            if not coins:
                raise RuntimeError("GeckoTerminal returned no eligible TON trending tokens")

            snapshot = TrendingSnapshot(
                updated_at=datetime.now(timezone.utc),
                coins=tuple(coins[: self.result_limit]),
            )
            self._save_cache(snapshot)
            self.snapshot = snapshot
            return snapshot

    async def _fetch_page(self, session: aiohttp.ClientSession, page: int) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=15, sock_connect=5, sock_read=12)
        params = {
            "include": "base_token",
            "duration": "24h",
            "page": str(page),
        }
        headers = {"Accept": "application/json", "User-Agent": "memepricebot/1.0"}
        for attempt in range(3):
            retry_after = 0
            async with session.get(
                TRENDING_URL,
                params=params,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status != 429:
                    response.raise_for_status()
                    payload = await response.json()
                    if not isinstance(payload, dict):
                        raise RuntimeError("GeckoTerminal returned an invalid trending response")
                    return payload
                retry_header = str(response.headers.get("Retry-After") or "").strip()
                retry_after = int(retry_header) if retry_header.isdigit() else 2 ** (attempt + 1)
                await response.read()
            if attempt < 2:
                await asyncio.sleep(max(1, min(retry_after, 30)))
        raise RuntimeError(f"GeckoTerminal rate-limited trending page {page}")

    @staticmethod
    def parse_payload(
        payload: Any,
        *,
        minimum_liquidity_usd: float = 10_000,
        result_limit: int = 10,
        blocked_symbols: Iterable[str] = DEFAULT_BLOCKED_SYMBOLS,
        blocked_addresses: Iterable[str] = DEFAULT_BLOCKED_ADDRESSES,
    ) -> list[TrendingCoin]:
        if not isinstance(payload, dict):
            return []

        normalized_blocked_symbols = {
            str(value).strip().upper() for value in blocked_symbols if str(value).strip()
        }
        normalized_blocked_addresses = {
            str(value).strip().casefold() for value in blocked_addresses if str(value).strip()
        }
        included_by_id: dict[str, dict[str, Any]] = {}
        for item in payload.get("included") or []:
            if not isinstance(item, dict) or item.get("type") != "token":
                continue
            item_id = str(item.get("id") or "")
            attributes = item.get("attributes")
            if item_id and isinstance(attributes, dict):
                included_by_id[item_id] = attributes

        coins: list[TrendingCoin] = []
        seen_tokens: set[str] = set()
        for pool in payload.get("data") or []:
            if not isinstance(pool, dict):
                continue
            attributes = pool.get("attributes")
            relationships = pool.get("relationships")
            if not isinstance(attributes, dict) or not isinstance(relationships, dict):
                continue

            base_relation = relationships.get("base_token") or {}
            base_data = base_relation.get("data") if isinstance(base_relation, dict) else {}
            base_id = str((base_data or {}).get("id") or "") if isinstance(base_data, dict) else ""
            token = included_by_id.get(base_id)
            if not token:
                continue

            token_address = str(token.get("address") or base_id.removeprefix("ton_")).strip()
            pool_address = str(pool.get("id") or "").removeprefix("ton_").strip()
            symbol = str(token.get("symbol") or "").strip().upper()
            name = str(token.get("name") or symbol).strip()
            if not token_address or not symbol or token_address in seen_tokens:
                continue
            if symbol in normalized_blocked_symbols or token_address.casefold() in normalized_blocked_addresses:
                continue
            if symbol in EXCLUDED_SYMBOLS or any(part in name.casefold() for part in EXCLUDED_NAME_PARTS):
                continue

            price_changes = attributes.get("price_change_percentage") or {}
            change_24h = _finite_float(price_changes.get("h24") if isinstance(price_changes, dict) else None)
            liquidity = _finite_float(attributes.get("reserve_in_usd"))
            if change_24h is None or liquidity is None or liquidity < minimum_liquidity_usd:
                continue

            seen_tokens.add(token_address)
            coins.append(
                TrendingCoin(
                    token_address=token_address,
                    name=name,
                    symbol=symbol,
                    change_24h=change_24h,
                    liquidity_usd=liquidity,
                    pool_address=pool_address,
                )
            )
            if len(coins) >= result_limit:
                break
        return coins

    def _load_cache(self) -> TrendingSnapshot | None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
            updated_at = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            coins = tuple(
                coin
                for item in payload["coins"]
                if isinstance(item, dict)
                for coin in (
                    TrendingCoin(
                    token_address=str(item["token_address"]),
                    name=str(item["name"]),
                    symbol=str(item["symbol"]),
                    change_24h=float(item["change_24h"]),
                    liquidity_usd=float(item["liquidity_usd"]),
                    pool_address=str(item.get("pool_address") or ""),
                    ),
                )
                if not self.is_blocked(coin)
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None
        return TrendingSnapshot(updated_at=updated_at.astimezone(timezone.utc), coins=coins) if coins else None

    def is_blocked(self, coin: TrendingCoin) -> bool:
        return (
            coin.symbol.strip().upper() in self.blocked_symbols
            or coin.token_address.strip().casefold() in self.blocked_addresses
        )

    def _save_cache(self, snapshot: TrendingSnapshot) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.cache_path.with_suffix(".tmp")
        payload = {
            "updated_at": snapshot.updated_at.astimezone(timezone.utc).isoformat(),
            "coins": [asdict(coin) for coin in snapshot.coins],
        }
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(self.cache_path)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
