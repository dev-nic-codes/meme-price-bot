from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import aiohttp

from .tonapi_client import TONAPI_REQUEST_PACER


TONKEEPER_REPOSITORY = "tonkeeper/ton-assets"
GITHUB_COMMITS_URL = f"https://api.github.com/repos/{TONKEEPER_REPOSITORY}/commits"
GITHUB_COMPARE_URL = f"https://api.github.com/repos/{TONKEEPER_REPOSITORY}/compare"
TONAPI_BULK_JETTONS_URL = "https://tonapi.io/v2/jettons/_bulk"
TONAPI_JETTON_HOLDERS_URL = "https://tonapi.io/v2/jettons/{address}/holders"
GECKO_TOKEN_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/ton/tokens/{address}/pools"
DEX_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/ton/{address}"
DEFAULT_NEW_TOKEN_MAX_AGE = timedelta(days=7)
DEFAULT_NEW_TOKEN_MINIMUM_LIQUIDITY_USD = 0.0
DEFAULT_NEW_TOKEN_RESULT_LIMIT = 5
GITHUB_FILE_LIMIT = 300

EXCLUDED_SYMBOLS = frozenset(
    {
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
)
EXCLUDED_NAME_PARTS = (
    "wrapped ton",
    "tether usd",
    "usd coin",
    "liquid staking",
    "staked ton",
)
STABLECOIN_MARKERS = ("USDT", "USD₮", "USDC", "TETHER")


@dataclass(frozen=True)
class NewTokenFilters:
    max_age: timedelta = DEFAULT_NEW_TOKEN_MAX_AGE
    minimum_valuation_usd: float = 0.0


@dataclass(frozen=True)
class VerifiedAsset:
    token_address: str
    name: str
    symbol: str
    verified_at: datetime
    source_path: str


@dataclass(frozen=True)
class NewToken:
    token_address: str
    name: str
    symbol: str
    pool_address: str
    dex_name: str
    created_at: datetime
    price_usd: float | None
    market_cap_usd: float | None
    fdv_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None
    holders: int | None = None
    verification: str | None = None
    mintable: bool | None = None
    pool_created_at: datetime | None = None
    source_path: str = ""
    chart_url_override: str = ""

    @property
    def valuation_usd(self) -> float | None:
        return self.market_cap_usd if self.market_cap_usd is not None else self.fdv_usd

    @property
    def valuation_label(self) -> str:
        return "MCAP" if self.market_cap_usd is not None else "FDV"

    @property
    def chart_url(self) -> str:
        override = str(self.chart_url_override or "").strip()
        if override.startswith("https://"):
            return override
        if self.pool_address:
            return f"https://www.geckoterminal.com/ton/pools/{self.pool_address}"
        return f"https://www.geckoterminal.com/ton/tokens/{_ton_friendly_address(self.token_address)}"


@dataclass(frozen=True)
class NewTokensSnapshot:
    updated_at: datetime
    tokens: tuple[NewToken, ...]


class NewTokensService:
    def __init__(
        self,
        session_provider: Callable[[], Awaitable[aiohttp.ClientSession]],
        cache_path: Path,
        *,
        refresh_seconds: int = 21_600,
        market_refresh_seconds: int = 300,
        minimum_liquidity_usd: float = DEFAULT_NEW_TOKEN_MINIMUM_LIQUIDITY_USD,
        result_limit: int = DEFAULT_NEW_TOKEN_RESULT_LIMIT,
        blocked_symbols: Iterable[str] = (),
        blocked_addresses: Iterable[str] = (),
    ) -> None:
        self.session_provider = session_provider
        self.cache_path = cache_path
        self.refresh_seconds = max(3_600, int(refresh_seconds))
        self.market_refresh_seconds = max(60, int(market_refresh_seconds))
        self.minimum_liquidity_usd = max(0.0, float(minimum_liquidity_usd))
        self.result_limit = max(1, min(20, int(result_limit)))
        self.blocked_symbols = frozenset(
            str(value).strip().upper() for value in blocked_symbols if str(value).strip()
        )
        self.blocked_addresses = frozenset(
            _ton_address_key(value) for value in blocked_addresses if str(value).strip()
        )
        self.refresh_lock = asyncio.Lock()
        self.market_refresh_lock = asyncio.Lock()
        self.last_market_refresh = 0.0
        self.last_market_addresses: frozenset[str] = frozenset()
        self.snapshot = self._load_cache()

    def current(self) -> NewTokensSnapshot | None:
        return self.snapshot

    def refresh_due(self, now: datetime | None = None) -> bool:
        if self.snapshot is None:
            return True
        current_time = now or datetime.now(timezone.utc)
        return (current_time - self.snapshot.updated_at).total_seconds() >= self.refresh_seconds

    async def refresh_if_due(self) -> NewTokensSnapshot:
        if not self.refresh_due() and self.snapshot is not None:
            return self.snapshot
        return await self.refresh()

    async def refresh(self, *, force: bool = False) -> NewTokensSnapshot:
        async with self.refresh_lock:
            if not force and not self.refresh_due() and self.snapshot is not None:
                return self.snapshot

            previous_snapshot = self.snapshot
            previous_by_address = {
                _ton_address_key(token.token_address): token
                for token in previous_snapshot.tokens
            } if previous_snapshot is not None else {}
            session = await self.session_provider()
            refreshed_at = datetime.now(timezone.utc)
            assets = await self._fetch_newly_verified_assets(session, refreshed_at)

            if not assets:
                snapshot = NewTokensSnapshot(updated_at=refreshed_at, tokens=())
                self._save_cache(snapshot)
                self.snapshot = snapshot
                return snapshot

            semaphore = asyncio.Semaphore(3)
            fetched = await asyncio.gather(
                *(self._fetch_asset_market(session, semaphore, asset) for asset in assets),
                return_exceptions=True,
            )
            tokens: list[NewToken] = []
            for asset, result in zip(assets, fetched):
                token = result if isinstance(result, NewToken) else self._token_without_market(asset)
                if self.is_blocked(token):
                    continue
                previous = previous_by_address.get(_ton_address_key(token.token_address))
                if previous is not None:
                    token = self._keep_previous_market_on_failure(token, previous)
                if previous is not None and token.holders is None:
                    token = replace(token, holders=previous.holders)
                tokens.append(token)
            tokens.sort(key=lambda token: token.created_at, reverse=True)
            snapshot = NewTokensSnapshot(updated_at=refreshed_at, tokens=tuple(tokens))
            self.snapshot = snapshot
            enriched = await self.enrich_holders(snapshot.tokens, force=True)
            snapshot = NewTokensSnapshot(updated_at=refreshed_at, tokens=enriched)
            self._save_cache(snapshot)
            self.snapshot = snapshot
            self.last_market_refresh = time.monotonic()
            self.last_market_addresses = frozenset(
                _ton_address_key(token.token_address) for token in snapshot.tokens
            )
            return snapshot

    async def _fetch_newly_verified_assets(
        self,
        session: aiohttp.ClientSession,
        now: datetime,
    ) -> tuple[VerifiedAsset, ...]:
        cutoff = now - DEFAULT_NEW_TOKEN_MAX_AGE
        base_payload = await self._fetch_json(
            session,
            GITHUB_COMMITS_URL,
            params={
                "path": "jettons",
                "until": cutoff.isoformat().replace("+00:00", "Z"),
                "per_page": "1",
            },
        )
        if not isinstance(base_payload, list) or not base_payload:
            raise RuntimeError("Could not resolve Tonkeeper verification-history cutoff")
        base_item = base_payload[0]
        base_sha = str(base_item.get("sha") or "").strip() if isinstance(base_item, dict) else ""
        if not base_sha:
            raise RuntimeError("Tonkeeper verification-history cutoff has no commit")

        comparison = await self._fetch_json(
            session,
            f"{GITHUB_COMPARE_URL}/{base_sha}...main",
        )
        if not isinstance(comparison, dict):
            raise RuntimeError("Tonkeeper returned an invalid verification comparison")
        files = comparison.get("files") or []
        if len(files) >= GITHUB_FILE_LIMIT:
            raise RuntimeError("Tonkeeper verification comparison was truncated")
        added = [
            item
            for item in files
            if isinstance(item, dict)
            and item.get("status") == "added"
            and str(item.get("filename") or "").startswith("jettons/")
            and str(item.get("filename") or "").endswith((".yaml", ".yml"))
        ]
        comparison_commits = comparison.get("commits") or []
        comparison_dates = [
            parsed
            for item in comparison_commits
            if isinstance(item, dict)
            for parsed in (_commit_datetime(item),)
            if parsed is not None and parsed >= cutoff
        ]
        verification_fallback = min(comparison_dates) if comparison_dates else cutoff
        semaphore = asyncio.Semaphore(5)
        loaded = await asyncio.gather(
            *(
                self._fetch_verified_asset(
                    session,
                    semaphore,
                    item,
                    fallback=verification_fallback,
                    comparison_commits=comparison_commits,
                )
                for item in added
            ),
            return_exceptions=True,
        )
        failures = [item for item in loaded if isinstance(item, Exception)]
        if failures:
            raise RuntimeError(
                f"Tonkeeper metadata was unavailable for {len(failures)} newly verified assets"
            )
        assets = [item for item in loaded if isinstance(item, VerifiedAsset)]
        if len(assets) != len(added):
            raise RuntimeError("Tonkeeper returned incomplete newly verified asset metadata")
        assets.sort(key=lambda asset: asset.verified_at, reverse=True)
        return tuple(assets)

    async def _fetch_verified_asset(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        file_item: dict[str, Any],
        *,
        fallback: datetime,
        comparison_commits: list[Any],
    ) -> VerifiedAsset | None:
        source_path = str(file_item.get("filename") or "").strip()
        raw_url = str(file_item.get("raw_url") or "").strip()
        if not source_path or not raw_url:
            return None
        async with semaphore:
            metadata_text, commit_payload = await asyncio.gather(
                self._fetch_text(session, raw_url),
                self._fetch_json(
                    session,
                    GITHUB_COMMITS_URL,
                    params={
                        "path": source_path,
                        "per_page": "100",
                    },
                ),
            )
        token_address = _yaml_scalar(metadata_text, "address")
        name = _yaml_scalar(metadata_text, "name")
        symbol = _yaml_scalar(metadata_text, "symbol")
        if not token_address or not symbol:
            return None
        verified_at = self._verification_date(
            source_path,
            commit_payload,
            comparison_commits,
            fallback=fallback,
        )
        return VerifiedAsset(
            token_address=token_address,
            name=name or symbol,
            symbol=symbol.upper(),
            verified_at=verified_at,
            source_path=source_path,
        )

    @staticmethod
    def _verification_date(
        source_path: str,
        path_commits: Any,
        comparison_commits: list[Any],
        *,
        fallback: datetime,
    ) -> datetime:
        path_items = path_commits if isinstance(path_commits, list) else []
        path_shas = {
            str(item.get("sha") or "").strip()
            for item in path_items
            if isinstance(item, dict)
        }
        merge_dates = [
            parsed
            for item in comparison_commits
            if isinstance(item, dict)
            and isinstance(item.get("parents"), list)
            and any(
                str(parent.get("sha") or "").strip() in path_shas
                for parent in item["parents"][1:]
                if isinstance(parent, dict)
            )
            for parsed in (_commit_datetime(item),)
            if parsed is not None and parsed >= fallback
        ]
        if merge_dates:
            return min(merge_dates)

        dates = [
            parsed
            for item in path_items
            if isinstance(item, dict)
            for parsed in (_commit_datetime(item),)
            if parsed is not None and parsed >= fallback
        ]
        if dates:
            return min(dates)
        stem = Path(source_path).stem.lstrip("$").casefold()
        comparable_commits = comparison_commits if isinstance(comparison_commits, list) else []
        matching_dates = [
            parsed
            for item in comparable_commits
            if isinstance(item, dict)
            and stem
            and stem in str((item.get("commit") or {}).get("message") or "").casefold()
            for parsed in (_commit_datetime(item),)
            if parsed is not None and parsed >= fallback
        ]
        return min(matching_dates) if matching_dates else fallback

    async def _fetch_asset_market(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        asset: VerifiedAsset,
    ) -> NewToken:
        provider_address = _ton_friendly_address(asset.token_address)
        async with semaphore:
            try:
                dex_payload = await self._fetch_json(
                    session,
                    DEX_TOKEN_PAIRS_URL.format(address=provider_address),
                    retries=2,
                )
            except Exception:
                dex_payload = None
        dex_token = self._token_from_dex_payload(dex_payload, asset)
        if dex_token is not None:
            return dex_token

        async with semaphore:
            try:
                payload = await self._fetch_json(
                    session,
                    GECKO_TOKEN_POOLS_URL.format(address=provider_address),
                    params={"include": "base_token,quote_token,dex", "page": "1"},
                    retries=2,
                )
            except Exception:
                return self._token_without_market(asset)
        pool = self._best_pool(payload, asset.token_address)
        if pool is None:
            return self._token_without_market(asset)
        attributes, dex_name, pool_address, token_is_base, volume_24h = pool
        price_usd = _finite_float(attributes.get("token_price_usd"))
        if price_usd is None:
            price_field = "base_token_price_usd" if token_is_base else "quote_token_price_usd"
            price_usd = _finite_float(attributes.get(price_field))
        return NewToken(
            token_address=asset.token_address,
            name=asset.name,
            symbol=asset.symbol,
            pool_address=pool_address,
            dex_name=dex_name,
            created_at=asset.verified_at,
            price_usd=price_usd,
            market_cap_usd=_finite_float(attributes.get("market_cap_usd")),
            fdv_usd=_finite_float(attributes.get("fdv_usd")),
            liquidity_usd=_finite_float(attributes.get("reserve_in_usd")),
            volume_24h_usd=volume_24h,
            verification="whitelist",
            pool_created_at=_parse_datetime(attributes.get("pool_created_at")),
            source_path=asset.source_path,
        )

    @staticmethod
    def _token_from_dex_payload(payload: Any, asset: VerifiedAsset) -> NewToken | None:
        if not isinstance(payload, list):
            return None
        target_key = _ton_address_key(asset.token_address)
        pairs: list[tuple[float, float, dict[str, Any], str]] = []
        seen_pools: set[str] = set()
        total_volume = 0.0
        has_volume = False
        for item in payload:
            if not isinstance(item, dict) or str(item.get("chainId") or "").lower() != "ton":
                continue
            base = item.get("baseToken") if isinstance(item.get("baseToken"), dict) else {}
            if _ton_address_key(base.get("address")) != target_key:
                continue
            pair_address = str(item.get("pairAddress") or "").strip()
            if not pair_address:
                continue
            liquidity_data = item.get("liquidity") if isinstance(item.get("liquidity"), dict) else {}
            volume_data = item.get("volume") if isinstance(item.get("volume"), dict) else {}
            liquidity = _finite_float(liquidity_data.get("usd")) or 0.0
            volume = _finite_float(volume_data.get("h24"))
            pair_key = pair_address.casefold()
            if pair_key not in seen_pools:
                seen_pools.add(pair_key)
                if volume is not None and volume >= 0:
                    total_volume += volume
                    has_volume = True
            pairs.append((liquidity, volume or 0.0, item, pair_address))
        if not pairs:
            return None

        _, _, best, pair_address = max(pairs, key=lambda item: (item[0], item[1]))
        liquidity_data = best.get("liquidity") if isinstance(best.get("liquidity"), dict) else {}
        chart_url = str(best.get("url") or "").strip()
        if not chart_url.startswith("https://"):
            chart_url = f"https://dexscreener.com/ton/{pair_address}"
        return NewToken(
            token_address=asset.token_address,
            name=asset.name,
            symbol=asset.symbol,
            pool_address=pair_address,
            dex_name=_dex_display_name(best.get("dexId")),
            created_at=asset.verified_at,
            price_usd=_finite_float(best.get("priceUsd")),
            market_cap_usd=_finite_float(best.get("marketCap")),
            fdv_usd=_finite_float(best.get("fdv")),
            liquidity_usd=_finite_float(liquidity_data.get("usd")),
            volume_24h_usd=total_volume if has_volume else None,
            verification="whitelist",
            pool_created_at=_parse_unix_milliseconds(best.get("pairCreatedAt")),
            source_path=asset.source_path,
            chart_url_override=chart_url,
        )

    @staticmethod
    def _best_pool(
        payload: Any,
        token_address: str,
    ) -> tuple[dict[str, Any], str, str, bool, float | None] | None:
        if not isinstance(payload, dict):
            return None
        included_by_id = {
            str(item.get("id") or ""): item.get("attributes")
            for item in payload.get("included") or []
            if isinstance(item, dict) and isinstance(item.get("attributes"), dict)
        }
        pools: list[tuple[float, dict[str, Any], str, str, bool, float | None]] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes")
            relationships = item.get("relationships")
            if not isinstance(attributes, dict) or not isinstance(relationships, dict):
                continue
            base_data = ((relationships.get("base_token") or {}).get("data") or {})
            base_id = str(base_data.get("id") or "") if isinstance(base_data, dict) else ""
            base = included_by_id.get(base_id, {})
            base_address = str(base.get("address") or base_id.removeprefix("ton_")).strip()
            quote_data = ((relationships.get("quote_token") or {}).get("data") or {})
            quote_id = str(quote_data.get("id") or "") if isinstance(quote_data, dict) else ""
            quote = included_by_id.get(quote_id, {})
            quote_address = str(quote.get("address") or quote_id.removeprefix("ton_")).strip()
            target_key = _ton_address_key(token_address)
            token_is_base = _ton_address_key(base_address) == target_key
            token_is_quote = _ton_address_key(quote_address) == target_key
            if not token_is_base and not token_is_quote:
                continue
            dex_data = ((relationships.get("dex") or {}).get("data") or {})
            dex_id = str(dex_data.get("id") or "") if isinstance(dex_data, dict) else ""
            dex = included_by_id.get(dex_id, {})
            dex_name = str(dex.get("name") or dex.get("identifier") or dex_id or "Unknown").strip()
            pool_address = str(item.get("id") or "").removeprefix("ton_").strip()
            liquidity = _finite_float(attributes.get("reserve_in_usd")) or 0.0
            volumes = attributes.get("volume_usd") if isinstance(attributes.get("volume_usd"), dict) else {}
            volume_24h = _finite_float(volumes.get("h24"))
            pools.append(
                (liquidity, attributes, dex_name, pool_address, token_is_base, volume_24h)
            )
        if not pools:
            return None
        _, attributes, dex_name, pool_address, token_is_base, _ = max(
            pools,
            key=lambda item: item[0],
        )
        volumes = [item[5] for item in pools if item[5] is not None and item[5] >= 0]
        volume_24h = sum(volumes) if volumes else None
        return attributes, dex_name, pool_address, token_is_base, volume_24h

    @staticmethod
    def _token_without_market(asset: VerifiedAsset) -> NewToken:
        return NewToken(
            token_address=asset.token_address,
            name=asset.name,
            symbol=asset.symbol,
            pool_address="",
            dex_name="Unknown",
            created_at=asset.verified_at,
            price_usd=None,
            market_cap_usd=None,
            fdv_usd=None,
            liquidity_usd=None,
            volume_24h_usd=None,
            verification="whitelist",
            source_path=asset.source_path,
        )

    @staticmethod
    def _keep_previous_market_on_failure(current: NewToken, previous: NewToken) -> NewToken:
        return replace(
            current,
            pool_address=current.pool_address or previous.pool_address,
            dex_name=(
                current.dex_name
                if current.dex_name and current.dex_name != "Unknown"
                else previous.dex_name
            ),
            price_usd=current.price_usd if current.price_usd is not None else previous.price_usd,
            market_cap_usd=(
                current.market_cap_usd
                if current.market_cap_usd is not None
                else previous.market_cap_usd
            ),
            fdv_usd=current.fdv_usd if current.fdv_usd is not None else previous.fdv_usd,
            liquidity_usd=(
                current.liquidity_usd
                if current.liquidity_usd is not None
                else previous.liquidity_usd
            ),
            volume_24h_usd=(
                current.volume_24h_usd
                if current.volume_24h_usd is not None
                else previous.volume_24h_usd
            ),
            pool_created_at=current.pool_created_at or previous.pool_created_at,
            chart_url_override=current.chart_url_override or previous.chart_url_override,
        )

    async def enrich_market(
        self,
        tokens: tuple[NewToken, ...],
        *,
        force: bool = False,
    ) -> tuple[NewToken, ...]:
        if not tokens:
            return tokens
        address_keys = frozenset(_ton_address_key(token.token_address) for token in tokens)
        async with self.market_refresh_lock:
            now = time.monotonic()
            if (
                not force
                and now - self.last_market_refresh < self.market_refresh_seconds
                and address_keys.issubset(self.last_market_addresses)
            ):
                current_snapshot = self.snapshot
                current_by_address = {
                    _ton_address_key(token.token_address): token
                    for token in current_snapshot.tokens
                } if current_snapshot is not None else {}
                return tuple(
                    current_by_address.get(_ton_address_key(token.token_address), token)
                    for token in tokens
                )

            session = await self.session_provider()
            semaphore = asyncio.Semaphore(3)
            assets = tuple(
                VerifiedAsset(
                    token_address=token.token_address,
                    name=token.name,
                    symbol=token.symbol,
                    verified_at=token.created_at,
                    source_path=token.source_path,
                )
                for token in tokens
            )
            fetched = await asyncio.gather(
                *(self._fetch_asset_market(session, semaphore, asset) for asset in assets),
                return_exceptions=True,
            )
            enriched: list[NewToken] = []
            for token, result in zip(tokens, fetched):
                if isinstance(result, NewToken):
                    market = self._keep_previous_market_on_failure(result, token)
                    market = replace(
                        market,
                        holders=token.holders,
                        verification=token.verification,
                        mintable=token.mintable,
                    )
                    enriched.append(market)
                else:
                    enriched.append(token)
            result = tuple(enriched)
            self.last_market_refresh = now
            self.last_market_addresses = address_keys
            self._persist_enriched_tokens(result)
            return result

    async def enrich_holders(
        self,
        tokens: tuple[NewToken, ...],
        *,
        force: bool = False,
    ) -> tuple[NewToken, ...]:
        missing = list(tokens) if force else [token for token in tokens if token.holders is None]
        if not missing:
            return tokens
        session = await self.session_provider()
        metadata_by_address = await self._fetch_bulk_jetton_metadata(session, missing)
        fallback_tokens = [
            token
            for token in missing
            if (_nonnegative_int(
                (metadata_by_address.get(_ton_address_key(token.token_address)) or {}).get(
                    "holders_count"
                )
            ) or 0) == 0
        ]
        holder_totals: dict[str, int] = {}
        for index, token in enumerate(fallback_tokens):
            if index:
                await asyncio.sleep(1.05)
            try:
                payload = await self._fetch_json(
                    session,
                    TONAPI_JETTON_HOLDERS_URL.format(
                        address=_ton_friendly_address(token.token_address)
                    ),
                    params={"limit": "1", "offset": "0"},
                    retries=4,
                )
                total = _nonnegative_int(payload.get("total")) if isinstance(payload, dict) else None
                if total is not None:
                    holder_totals[_ton_address_key(token.token_address)] = total
            except Exception as exc:
                print(
                    f"New-token holder lookup kept prior value for {token.symbol}: "
                    f"{type(exc).__name__}",
                    flush=True,
                )

        enriched: list[NewToken] = []
        for token in tokens:
            address_key = _ton_address_key(token.token_address)
            record = metadata_by_address.get(address_key)
            if record is None:
                total = holder_totals.get(address_key)
                enriched.append(replace(token, holders=total) if total is not None else token)
                continue
            provider_status = str(record.get("verification") or "").strip().casefold()
            verification = provider_status if provider_status in {"graylist", "blacklist"} else token.verification
            bulk_count = _nonnegative_int(record.get("holders_count"))
            holders = holder_totals.get(address_key)
            if holders is None:
                holders = bulk_count if bulk_count is not None and bulk_count > 0 else token.holders
            enriched.append(
                replace(
                    token,
                    holders=holders,
                    verification=verification,
                    mintable=(record.get("mintable") if isinstance(record.get("mintable"), bool) else None),
                )
            )
        result = tuple(enriched)
        self._persist_enriched_tokens(result)
        return result

    async def _fetch_bulk_jetton_metadata(
        self,
        session: aiohttp.ClientSession,
        tokens: list[NewToken],
    ) -> dict[str, dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=15, sock_connect=5, sock_read=12)
        try:
            await TONAPI_REQUEST_PACER.wait()
            async with session.post(
                TONAPI_BULK_JETTONS_URL,
                json={"account_ids": [token.token_address for token in tokens]},
                headers=self._headers(TONAPI_BULK_JETTONS_URL),
                timeout=timeout,
            ) as response:
                if response.status == 429:
                    await response.read()
                    return {}
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError):
            return {}
        records = payload.get("jettons") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        metadata_by_address: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            address_key = _ton_address_key(record.get("address") or metadata.get("address") or "")
            if address_key:
                metadata_by_address[address_key] = record
        return metadata_by_address

    def _persist_enriched_tokens(self, enriched: tuple[NewToken, ...]) -> None:
        snapshot = getattr(self, "snapshot", None)
        if snapshot is None or not enriched:
            return
        updates = {_ton_address_key(token.token_address): token for token in enriched}
        merged = tuple(
            updates.get(_ton_address_key(token.token_address), token)
            for token in snapshot.tokens
        )
        if merged == snapshot.tokens:
            return
        self.snapshot = NewTokensSnapshot(updated_at=snapshot.updated_at, tokens=merged)
        self._save_cache(self.snapshot)

    def select(
        self,
        snapshot: NewTokensSnapshot,
        filters: NewTokenFilters,
        *,
        now: datetime | None = None,
    ) -> tuple[NewToken, ...]:
        return tuple(
            token
            for token in self.candidates(snapshot, filters, now=now)
            if self.is_verified(token)
        )[: self.result_limit]

    def candidates(
        self,
        snapshot: NewTokensSnapshot,
        filters: NewTokenFilters,
        *,
        now: datetime | None = None,
    ) -> tuple[NewToken, ...]:
        current_time = now or datetime.now(timezone.utc)
        effective_max_age = min(filters.max_age, DEFAULT_NEW_TOKEN_MAX_AGE)
        candidates: list[NewToken] = []
        for token in snapshot.tokens:
            age = current_time - token.created_at
            valuation = token.valuation_usd
            if age < timedelta(0) or age > effective_max_age:
                continue
            if self.minimum_liquidity_usd and (
                token.liquidity_usd is None or token.liquidity_usd < self.minimum_liquidity_usd
            ):
                continue
            if filters.minimum_valuation_usd and (
                valuation is None or valuation < filters.minimum_valuation_usd
            ):
                continue
            candidates.append(token)
        return tuple(candidates)

    @staticmethod
    def is_verified(token: NewToken) -> bool:
        return str(token.verification or "").strip().casefold() == "whitelist"

    def is_blocked(self, token: NewToken) -> bool:
        symbol = token.symbol.strip().upper()
        name = token.name.strip().upper()
        return (
            symbol in self.blocked_symbols
            or _ton_address_key(token.token_address) in self.blocked_addresses
            or symbol in EXCLUDED_SYMBOLS
            or any(part in token.name.casefold() for part in EXCLUDED_NAME_PARTS)
            or any(marker in symbol or marker in name for marker in STABLECOIN_MARKERS)
        )

    async def _fetch_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: dict[str, str] | None = None,
        retries: int = 3,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=20, sock_connect=5, sock_read=17)
        for attempt in range(retries):
            if "tonapi.io" in url:
                await TONAPI_REQUEST_PACER.wait()
            async with session.get(
                url,
                params=params,
                headers=self._headers(url),
                timeout=timeout,
            ) as response:
                if response.status not in {429, 502, 503, 504}:
                    response.raise_for_status()
                    return await response.json()
                retry_header = str(response.headers.get("Retry-After") or "").strip()
                delay = int(retry_header) if retry_header.isdigit() else 2 ** attempt
                await response.read()
            if attempt + 1 < retries:
                await asyncio.sleep(max(1, min(delay, 30)))
        raise RuntimeError(f"Provider request failed after {retries} attempts: {url}")

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=15, sock_connect=5, sock_read=12)
        async with session.get(url, headers=self._headers(url), timeout=timeout) as response:
            response.raise_for_status()
            return await response.text()

    @staticmethod
    def _headers(url: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "memepricesbot/1.0"}
        if "api.github.com" in url or "raw.githubusercontent.com" in url:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            github_token = os.getenv("GITHUB_TOKEN", "").strip()
            if github_token:
                headers["Authorization"] = f"Bearer {github_token}"
        if "tonapi.io" in url:
            tonapi_key = os.getenv("TONAPI_KEY", "").strip()
            if tonapi_key:
                headers["Authorization"] = f"Bearer {tonapi_key}"
        return headers

    @staticmethod
    def parse_payload(payload: Any) -> list[NewToken]:
        """Parse GeckoTerminal pool payloads for compatibility and focused tests."""
        if not isinstance(payload, dict):
            return []
        included_by_id = {
            str(item.get("id") or ""): item.get("attributes")
            for item in payload.get("included") or []
            if isinstance(item, dict) and isinstance(item.get("attributes"), dict)
        }
        tokens: list[NewToken] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes")
            relationships = item.get("relationships")
            if not isinstance(attributes, dict) or not isinstance(relationships, dict):
                continue
            base_data = ((relationships.get("base_token") or {}).get("data") or {})
            base_id = str(base_data.get("id") or "") if isinstance(base_data, dict) else ""
            base = included_by_id.get(base_id, {})
            address = str(base.get("address") or base_id.removeprefix("ton_")).strip()
            symbol = str(base.get("symbol") or "").strip().upper()
            created_at = _parse_datetime(attributes.get("pool_created_at"))
            if not address or not symbol or created_at is None:
                continue
            dex_data = ((relationships.get("dex") or {}).get("data") or {})
            dex_id = str(dex_data.get("id") or "") if isinstance(dex_data, dict) else ""
            dex = included_by_id.get(dex_id, {})
            volumes = attributes.get("volume_usd") or {}
            tokens.append(
                NewToken(
                    token_address=address,
                    name=str(base.get("name") or symbol).strip(),
                    symbol=symbol,
                    pool_address=str(item.get("id") or "").removeprefix("ton_").strip(),
                    dex_name=str(dex.get("name") or dex_id or "Unknown").strip(),
                    created_at=created_at,
                    price_usd=_finite_float(attributes.get("base_token_price_usd")),
                    market_cap_usd=_finite_float(attributes.get("market_cap_usd")),
                    fdv_usd=_finite_float(attributes.get("fdv_usd")),
                    liquidity_usd=_finite_float(attributes.get("reserve_in_usd")),
                    volume_24h_usd=_finite_float(volumes.get("h24") if isinstance(volumes, dict) else None),
                    pool_created_at=created_at,
                )
            )
        return tokens

    def _load_cache(self) -> NewTokensSnapshot | None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
            updated_at = _parse_datetime(payload["updated_at"])
            if updated_at is None:
                return None
            tokens = tuple(
                NewToken(
                    token_address=str(item["token_address"]),
                    name=str(item["name"]),
                    symbol=str(item["symbol"]),
                    pool_address=str(item.get("pool_address") or ""),
                    dex_name=str(item.get("dex_name") or "Unknown"),
                    created_at=_parse_datetime(item["created_at"]),
                    price_usd=_finite_float(item.get("price_usd")),
                    market_cap_usd=_finite_float(item.get("market_cap_usd")),
                    fdv_usd=_finite_float(item.get("fdv_usd")),
                    liquidity_usd=_finite_float(item.get("liquidity_usd")),
                    volume_24h_usd=_finite_float(item.get("volume_24h_usd")),
                    holders=_nonnegative_int(item.get("holders")),
                    verification=str(item.get("verification") or "") or None,
                    mintable=item.get("mintable") if isinstance(item.get("mintable"), bool) else None,
                    pool_created_at=_parse_datetime(item.get("pool_created_at")),
                    source_path=str(item.get("source_path") or ""),
                    chart_url_override=str(item.get("chart_url_override") or ""),
                )
                for item in payload["tokens"]
                if isinstance(item, dict) and _parse_datetime(item.get("created_at")) is not None
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None
        return NewTokensSnapshot(updated_at=updated_at, tokens=tokens)

    def _save_cache(self, snapshot: NewTokensSnapshot) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.cache_path.with_suffix(".tmp")
        payload = {
            "updated_at": snapshot.updated_at.astimezone(timezone.utc).isoformat(),
            "tokens": [
                {
                    **asdict(token),
                    "created_at": token.created_at.astimezone(timezone.utc).isoformat(),
                    "pool_created_at": (
                        token.pool_created_at.astimezone(timezone.utc).isoformat()
                        if token.pool_created_at is not None
                        else None
                    ),
                }
                for token in snapshot.tokens
            ],
        }
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.cache_path)


def _commit_datetime(item: dict[str, Any]) -> datetime | None:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    return _parse_datetime(committer.get("date"))


def _yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", str(text or ""), re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            return str(json.loads(value)).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            return value[1:-1].strip()
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'").strip()
    return value.split(" #", 1)[0].strip()


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_unix_milliseconds(value: Any) -> datetime | None:
    timestamp = _finite_float(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _dex_display_name(value: Any) -> str:
    identifier = str(value or "").strip()
    known = {
        "dedust": "DeDust",
        "stonfi": "STON.fi",
        "ston.fi": "STON.fi",
    }
    return known.get(identifier.casefold(), identifier or "Unknown")


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _ton_address_key(value: Any) -> str:
    address = str(value or "").strip()
    if not address:
        return ""
    if ":" in address:
        workchain, account_hash = address.split(":", 1)
        try:
            return f"{int(workchain)}:{account_hash.lower()}"
        except ValueError:
            return address.casefold()
    try:
        decoded = base64.urlsafe_b64decode(address + "=" * (-len(address) % 4))
    except (ValueError, TypeError):
        return address.casefold()
    if len(decoded) != 36:
        return address.casefold()
    workchain = int.from_bytes(decoded[1:2], byteorder="big", signed=True)
    return f"{workchain}:{decoded[2:34].hex()}"


def _ton_friendly_address(value: Any) -> str:
    address = str(value or "").strip()
    if ":" not in address:
        return address
    workchain_text, account_hash = address.split(":", 1)
    try:
        workchain = int(workchain_text)
        hash_bytes = bytes.fromhex(account_hash)
    except (ValueError, TypeError):
        return address
    if not -128 <= workchain <= 127 or len(hash_bytes) != 32:
        return address
    body = bytes((0x11, workchain & 0xFF)) + hash_bytes
    checksum = binascii.crc_hqx(body, 0).to_bytes(2, byteorder="big")
    return base64.urlsafe_b64encode(body + checksum).decode().rstrip("=")
