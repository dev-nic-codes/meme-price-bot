from __future__ import annotations

import asyncio
import base64
import html
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp


DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEX_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/ton/{address}"
GECKO_POOL_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/ton/pools/{pool}/ohlcv/{timeframe}"
COINGECKO_CONTRACT_URL = "https://api.coingecko.com/api/v3/coins/the-open-network/contract/{address}"
COINGECKO_COIN_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
LOGO_CDN_HOSTS = frozenset({"cdn.dexscreener.com", "dd.dexscreener.com"})
MAX_LOGO_BYTES = 3_000_000

TON_ADDRESS_RE = re.compile(r"^(?:EQ|UQ)[A-Za-z0-9_-]{46}$|^-?\d+:[0-9a-fA-F]{64}$")

QUERY_ALIASES = {
    "hmster": "hmstr",
    "hamster": "hmstr",
    "hamster kombat": "hmstr",
    "resistance": "redo",
    "resistance dog": "redo",
}

KNOWN_TOKEN_ADDRESSES = {
    "utya": "EQBaCgUwOoc6gHCNln_oJzb0mVs79YG7wYoavh-o1ItaneLA",
    "redo": "EQBZ_cafPyDr5KUTs0aNxh0ZTDhkpEZONmLJA2SNGlLm4Cko",
    "scat": "EQDV0Q8euPPdsxHfaOQAmtdDrh3j5o_odMYIJM4nuiUO6t88",
    "yoda": "EQC7vuKEYLdC72YhUWt3AUVA-Oi66Q1DxTHXH7r6pXaV50j7",
    "cherry": "EQBKRSNRkeP1-2jcg5T_f__0s5Hj-vrbfNLMQy8dnZs7xd_p",
    "bcherry": "EQD5u2gJ_dyqH0IvEBbMxqR6DLSkToHM-6ZdtGocsiqJbn3o",
    "mtonga": "EQDuGgqZU7_AEgiOwEe-abozIefuoairTWLOyd7c_f8GhzMf",
    "groyp": "EQAtwo6qMNwtr0iTA9eKVZ32cuACFJ0VKd78GrBWOe83-X1P",
    "gramming": "EQAmsYIAadPQrEn-wZrRKwqhnReLDOeKl9T70umuk0MA1ULW",
    "grm": "EQC47093oX5Xhb0xuk2lCr2RhS8rj-vul61u4W2UH5ORmG_O",
}


@dataclass(frozen=True)
class TokenPair:
    token_address: str
    pair_address: str
    name: str
    symbol: str
    dex_id: str
    url: str
    price_usd: float | None
    market_cap: float | None
    fdv: float | None
    liquidity_usd: float | None
    price_change: dict[str, float]
    image_url: str | None = None


@dataclass(frozen=True)
class TokenChoice:
    token_address: str
    name: str
    symbol: str
    liquidity_usd: float | None
    price_usd: float | None


@dataclass(frozen=True)
class TokenHistory:
    high_24h: float | None = None
    low_24h: float | None = None
    chart_high: float | None = None
    chart_high_at: datetime | None = None
    changes: dict[str, float | None] | None = None
    chart_points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class TokenAth:
    price_usd: float
    reached_at: datetime


@dataclass(frozen=True)
class TokenReportResult:
    text: str | None = None
    choices: list[TokenChoice] | None = None
    query: str = ""

    @property
    def has_choices(self) -> bool:
        return bool(self.choices)


class TokenReportService:
    def __init__(
        self,
        session_provider: Callable[[], Awaitable[aiohttp.ClientSession]],
        *,
        cache_seconds: int = 45,
        search_cache_seconds: int = 300,
        history_cache_seconds: int = 1800,
        max_api_concurrency: int = 8,
    ) -> None:
        self.session_provider = session_provider
        self.cache_seconds = cache_seconds
        self.search_cache_seconds = search_cache_seconds
        self.history_cache_seconds = history_cache_seconds
        self.api_semaphore = asyncio.Semaphore(max_api_concurrency)
        self.report_cache: dict[str, tuple[float, TokenReportResult]] = {}
        self.pair_cache: dict[str, tuple[float, TokenPair | None]] = {}
        self.search_cache: dict[str, tuple[float, list[TokenPair]]] = {}
        self.history_cache: dict[str, tuple[float, TokenHistory]] = {}
        self.ath_cache: dict[str, tuple[float, TokenAth | None]] = {}
        self.logo_cache: dict[str, tuple[float, bytes | None]] = {}
        self.inflight: dict[str, asyncio.Task[TokenReportResult]] = {}

    async def report_for_query(self, raw_query: str) -> TokenReportResult:
        query = normalize_query(raw_query)
        if not query:
            return TokenReportResult(text=self.not_found_text(raw_query), query=raw_query)

        cache_key = f"query:{query}"
        cached = self._get_report_cache(cache_key)
        if cached:
            return cached

        return await self._coalesced(cache_key, lambda: self._build_report_for_query(query, raw_query))

    async def report_for_token_address(self, token_address: str) -> TokenReportResult:
        token_address = token_address.strip()
        cache_key = f"token:{token_address}"
        cached = self._get_report_cache(cache_key)
        if cached:
            return cached

        return await self._coalesced(cache_key, lambda: self._build_report_for_token(token_address))

    async def _build_report_for_query(self, query: str, raw_query: str) -> TokenReportResult:
        known_address = KNOWN_TOKEN_ADDRESSES.get(query.lower())
        if known_address:
            pair = await self.best_pair_for_token(known_address)
            if pair is None:
                result = TokenReportResult(text=self.not_found_text(raw_query), query=raw_query)
            else:
                result = await self._format_pair_report(pair)
                self._set_report_cache(f"token:{pair.token_address}", result)
            self._set_report_cache(f"query:{query}", result)
            return result

        pairs = await self.search_pairs(query)
        if not pairs:
            result = TokenReportResult(text=self.not_found_text(raw_query), query=raw_query)
            self._set_report_cache(f"query:{query}", result)
            return result

        selected = select_best_pair(query, pairs)
        if isinstance(selected, list):
            result = TokenReportResult(
                choices=[
                    TokenChoice(
                        token_address=pair.token_address,
                        name=pair.name,
                        symbol=pair.symbol,
                        liquidity_usd=pair.liquidity_usd,
                        price_usd=pair.price_usd,
                    )
                    for pair in selected[:6]
                ],
                query=raw_query,
            )
            self._set_report_cache(f"query:{query}", result)
            return result

        fresh_pair = await self.best_pair_for_token(selected.token_address)
        if fresh_pair is not None:
            selected = fresh_pair

        result = await self._format_pair_report(selected)
        self._set_report_cache(f"query:{query}", result)
        self._set_report_cache(f"token:{selected.token_address}", result)
        return result

    async def _build_report_for_token(self, token_address: str) -> TokenReportResult:
        pair = await self.best_pair_for_token(token_address)
        if pair is None:
            result = TokenReportResult(text=self.not_found_text(token_address), query=token_address)
            self._set_report_cache(f"token:{token_address}", result)
            return result
        result = await self._format_pair_report(pair)
        self._set_report_cache(f"token:{pair.token_address}", result)
        return result

    async def _format_pair_report(self, pair: TokenPair) -> TokenReportResult:
        history, ath = await asyncio.gather(
            self.fetch_history(pair.pair_address),
            self.fetch_ath(pair.token_address),
        )
        return TokenReportResult(text=format_token_report(pair, history, ath), query=pair.symbol)

    async def search_pairs(self, query: str) -> list[TokenPair]:
        normalized = normalize_query(query)
        if TON_ADDRESS_RE.match(normalized):
            pair = await self.best_pair_for_token(normalized)
            return [pair] if pair else []

        cache_key = f"search:{normalized}"
        cached = self._get_search_cache(cache_key)
        if cached is not None:
            return cached

        payload = await self._get_json(DEX_SEARCH_URL, params={"q": normalized}, timeout=10)
        raw_pairs = payload.get("pairs") if isinstance(payload, dict) else []
        pairs = [parse_pair(item) for item in raw_pairs if isinstance(item, dict) and item.get("chainId") == "ton"]
        compacted = best_pair_per_token([pair for pair in pairs if pair is not None])
        self._set_search_cache(cache_key, compacted)
        return compacted

    async def best_pair_for_token(self, token_address: str) -> TokenPair | None:
        token_address = token_address.strip()
        cached = self._get_pair_cache(token_address)
        if cached is not None:
            return cached

        payload = await self._get_json(DEX_TOKEN_PAIRS_URL.format(address=token_address), timeout=10)
        if not isinstance(payload, list):
            raise RuntimeError("DexScreener returned an invalid token-pairs response")
        pairs = [
            parse_pair(item, expected_token_address=token_address)
            for item in payload
            if isinstance(item, dict)
        ]

        valid_pairs = [
            pair
            for pair in pairs
            if pair is not None
            and pair.token_address
            and pair.price_usd is not None
            and pair.price_usd > 0
        ]
        best = sorted(valid_pairs, key=pair_quality_score, reverse=True)[0] if valid_pairs else None
        self._set_pair_cache(token_address, best)
        return best

    async def fetch_history(self, pair_address: str) -> TokenHistory:
        cached = self._get_history_cache(pair_address)
        if cached is not None:
            return cached

        hourly_task = asyncio.create_task(
            self.fetch_ohlcv(
                pair_address,
                "hour",
                {"aggregate": "1", "limit": "168", "currency": "usd", "token": "base"},
            )
        )
        daily_task = asyncio.create_task(
            self.fetch_ohlcv(pair_address, "day", {"aggregate": "1", "limit": "1000", "currency": "usd", "token": "base"})
        )
        hourly, daily = await asyncio.gather(hourly_task, daily_task, return_exceptions=True)
        hourly_rows = [] if isinstance(hourly, Exception) else hourly
        daily_rows = [] if isinstance(daily, Exception) else daily

        history = build_history(hourly_rows, daily_rows)
        self._set_history_cache(pair_address, history)
        return history

    async def fetch_logo(self, image_url: str | None) -> bytes | None:
        url = str(image_url or "").strip()
        if not self._trusted_logo_url(url):
            return None

        cached = self.logo_cache.get(url)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        logo: bytes | None = None
        cache_seconds = 600
        session = await self.session_provider()
        timeout = aiohttp.ClientTimeout(total=10, sock_connect=4, sock_read=8)
        headers = {"Accept": "image/*", "User-Agent": "memepricebot/1.0"}
        try:
            async with self.api_semaphore:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    if not content_type.startswith("image/"):
                        raise ValueError("token logo response is not an image")
                    if response.content_length is not None and response.content_length > MAX_LOGO_BYTES:
                        raise ValueError("token logo response is too large")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65_536):
                        chunks.extend(chunk)
                        if len(chunks) > MAX_LOGO_BYTES:
                            raise ValueError("token logo response is too large")
                    payload = bytes(chunks)
                    if not payload:
                        raise ValueError("token logo response is empty or too large")
                    logo = payload
                    cache_seconds = 21_600
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logo = None

        self.logo_cache[url] = (time.monotonic() + cache_seconds, logo)
        return logo

    @staticmethod
    def _trusted_logo_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme == "https" and (parsed.hostname or "").lower() in LOGO_CDN_HOSTS

    async def fetch_ohlcv(self, pair_address: str, timeframe: str, params: dict[str, str]) -> list[list[float]]:
        payload = await self._get_json(
            GECKO_POOL_OHLCV_URL.format(pool=pair_address, timeframe=timeframe),
            params=params,
            timeout=12,
        )
        rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        parsed: list[list[float]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                parsed.append([float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
            except (TypeError, ValueError):
                continue
        return parsed

    async def fetch_ath(self, token_address: str) -> TokenAth | None:
        return await self._fetch_coingecko_ath(
            cache_key=token_address,
            url=COINGECKO_CONTRACT_URL.format(address=token_address),
            parser=lambda payload: parse_coingecko_ath(payload, token_address),
        )

    async def fetch_coin_ath(self, coin_id: str) -> TokenAth | None:
        normalized_id = str(coin_id or "").strip().lower()
        if not normalized_id or not re.fullmatch(r"[a-z0-9-]+", normalized_id):
            return None
        return await self._fetch_coingecko_ath(
            cache_key=f"coin:{normalized_id}",
            url=COINGECKO_COIN_URL.format(coin_id=normalized_id),
            parser=lambda payload: parse_coingecko_coin_ath(payload, normalized_id),
        )

    async def _fetch_coingecko_ath(
        self,
        *,
        cache_key: str,
        url: str,
        parser: Callable[[dict[str, Any]], TokenAth | None],
    ) -> TokenAth | None:
        cached = self.ath_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        session = await self.session_provider()
        request_timeout = aiohttp.ClientTimeout(total=10, sock_connect=4, sock_read=8)
        headers = {"Accept": "application/json", "User-Agent": "memepricebot/1.0"}
        ath: TokenAth | None = None
        cache_seconds = 600
        try:
            async with self.api_semaphore:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=request_timeout,
                ) as response:
                    if response.status == 404:
                        payload = None
                    else:
                        response.raise_for_status()
                        payload = await response.json()
            if isinstance(payload, dict):
                ath = parser(payload)
                if ath is not None:
                    cache_seconds = 21_600
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # ATH must fail closed. Partial pool history is not a valid
            # substitute for a provider-maintained lifetime high.
            ath = None

        self.ath_cache[cache_key] = (time.monotonic() + cache_seconds, ath)
        return ath

    async def _get_json(self, url: str, *, params: dict[str, str] | None = None, timeout: int = 12) -> Any:
        session = await self.session_provider()
        request_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=4, sock_read=max(6, timeout - 2))
        headers = {"Accept": "application/json", "User-Agent": "memepricebot/1.0"}
        async with self.api_semaphore:
            async with session.get(url, params=params, headers=headers, timeout=request_timeout) as response:
                response.raise_for_status()
                return await response.json()

    async def _coalesced(
        self,
        key: str,
        builder: Callable[[], Awaitable[TokenReportResult]],
    ) -> TokenReportResult:
        existing = self.inflight.get(key)
        if existing:
            return await existing
        task = asyncio.create_task(builder())
        self.inflight[key] = task
        try:
            return await task
        finally:
            self.inflight.pop(key, None)

    def _get_report_cache(self, key: str) -> TokenReportResult | None:
        item = self.report_cache.get(key)
        if not item:
            return None
        expires_at, result = item
        if expires_at <= time.monotonic():
            self.report_cache.pop(key, None)
            return None
        return result

    def _set_report_cache(self, key: str, result: TokenReportResult) -> None:
        self.report_cache[key] = (time.monotonic() + self.cache_seconds, result)

    def _get_search_cache(self, key: str) -> list[TokenPair] | None:
        item = self.search_cache.get(key)
        if not item:
            return None
        expires_at, pairs = item
        if expires_at <= time.monotonic():
            self.search_cache.pop(key, None)
            return None
        return pairs

    def _set_search_cache(self, key: str, pairs: list[TokenPair]) -> None:
        self.search_cache[key] = (time.monotonic() + self.search_cache_seconds, pairs)

    def _get_pair_cache(self, key: str) -> TokenPair | None:
        item = self.pair_cache.get(key)
        if not item:
            return None
        expires_at, pair = item
        if expires_at <= time.monotonic():
            self.pair_cache.pop(key, None)
            return None
        return pair

    def _set_pair_cache(self, key: str, pair: TokenPair | None) -> None:
        self.pair_cache[key] = (time.monotonic() + self.cache_seconds, pair)

    def _get_history_cache(self, key: str) -> TokenHistory | None:
        item = self.history_cache.get(key)
        if not item:
            return None
        expires_at, history = item
        if expires_at <= time.monotonic():
            self.history_cache.pop(key, None)
            return None
        return history

    def _set_history_cache(self, key: str, history: TokenHistory) -> None:
        self.history_cache[key] = (time.monotonic() + self.history_cache_seconds, history)

    @staticmethod
    def not_found_text(query: str) -> str:
        safe_query = html.escape(query.strip() or "that token")
        return (
            "🔎 <b>Token not found</b>\n\n"
            f"I could not find a TON token for <b>{safe_query}</b>.\n\n"
            "Try one of these formats:\n"
            "• <b>/meme utya</b>\n"
            "• <b>/meme $HMSTR</b>\n"
            "• <b>/meme resistance dog</b>\n"
            "• <b>/meme EQ...</b>"
        )


def normalize_query(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    alias_key = cleaned.lower()
    return QUERY_ALIASES.get(alias_key, cleaned)


def parse_pair(payload: dict[str, Any], *, expected_token_address: str | None = None) -> TokenPair | None:
    chain_id = str(payload.get("chainId") or "").strip().lower()
    if chain_id and chain_id != "ton":
        return None

    base = payload.get("baseToken") if isinstance(payload.get("baseToken"), dict) else {}
    token_address = str(base.get("address") or "").strip()
    pair_address = str(payload.get("pairAddress") or "").strip()
    if not token_address or not pair_address:
        return None
    if expected_token_address and not same_ton_address(token_address, expected_token_address):
        # DexScreener's price fields describe the base token. A quote-token
        # match must never be presented as data for the requested token.
        return None

    price_usd = positive_float_or_none(payload.get("priceUsd"))
    liquidity_usd = positive_float_or_none(
        (payload.get("liquidity") or {}).get("usd")
        if isinstance(payload.get("liquidity"), dict)
        else None
    )
    changes = payload.get("priceChange") if isinstance(payload.get("priceChange"), dict) else {}
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    image_url = str(info.get("imageUrl") or "").strip() or None
    return TokenPair(
        token_address=token_address,
        pair_address=pair_address,
        name=str(base.get("name") or "Unknown").strip() or "Unknown",
        symbol=str(base.get("symbol") or "?").strip() or "?",
        dex_id=str(payload.get("dexId") or "").strip(),
        url=str(payload.get("url") or "").strip(),
        price_usd=price_usd,
        market_cap=positive_float_or_none(payload.get("marketCap")),
        fdv=positive_float_or_none(payload.get("fdv")),
        liquidity_usd=liquidity_usd,
        price_change={str(key): value for key, raw in changes.items() if (value := float_or_none(raw)) is not None},
        image_url=image_url,
    )


def best_pair_per_token(pairs: list[TokenPair]) -> list[TokenPair]:
    by_token: dict[str, TokenPair] = {}
    for pair in pairs:
        current = by_token.get(pair.token_address)
        if current is None or pair_quality_score(pair) > pair_quality_score(current):
            by_token[pair.token_address] = pair
    return sorted(by_token.values(), key=pair_quality_score, reverse=True)


def select_best_pair(query: str, pairs: list[TokenPair]) -> TokenPair | list[TokenPair]:
    if len(pairs) == 1:
        return pairs[0]

    normalized = normalize_query(query).lower()
    scored = sorted(pairs, key=lambda pair: query_score(normalized, pair), reverse=True)
    exact = [
        pair
        for pair in scored
        if pair.symbol.lower() == normalized or pair.name.lower() == normalized
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact[:6]

    top = scored[0]
    second = scored[1] if len(scored) > 1 else None
    top_score = query_score(normalized, top)
    second_score = query_score(normalized, second) if second else -1
    top_liq = top.liquidity_usd or 0
    second_liq = (second.liquidity_usd or 0) if second else 0

    clearly_more_liquid = second is None or top_liq >= max(2_000, second_liq * 3)
    clearly_better_match = second is None or top_score >= second_score + 250
    if clearly_more_liquid and clearly_better_match:
        return top

    return scored[:6]


def query_score(query: str, pair: TokenPair | None) -> float:
    if pair is None:
        return -1.0
    symbol = pair.symbol.lower()
    name = pair.name.lower()
    score = pair_quality_score(pair)
    if symbol == query:
        score += 10_000
    if name == query:
        score += 8_000
    if symbol.startswith(query):
        score += 2_500
    if query in name:
        score += 2_000
    return score


def pair_quality_score(pair: TokenPair) -> float:
    liquidity = pair.liquidity_usd or 0.0
    market_cap = pair.market_cap or pair.fdv or 0.0
    price = pair.price_usd or 0.0
    return math.log10(liquidity + 1) * 100 + math.log10(market_cap + 1) * 25 + (10 if price > 0 else 0)


def build_history(
    hourly_rows: list[list[float]],
    daily_rows: list[list[float]],
    *,
    now_timestamp: float | None = None,
) -> TokenHistory:
    hourly = sorted(valid_ohlcv_rows(hourly_rows), key=lambda row: row[0])
    daily = sorted(valid_ohlcv_rows(daily_rows), key=lambda row: row[0])
    now = time.time() if now_timestamp is None else now_timestamp
    recent_hourly = [row for row in hourly if now - 86_400 <= row[0] <= now + 300]
    high_24h = max((row[2] for row in recent_hourly), default=None)
    low_24h = min((row[3] for row in recent_hourly), default=None)

    chart_rows = [row for row in hourly if now - 7 * 86_400 <= row[0] <= now + 300]
    if len(chart_rows) < 2:
        chart_rows = [row for row in daily if now - 7 * 86_400 <= row[0] <= now + 300]
    chart_points = tuple((row[0], row[4]) for row in chart_rows)

    chart_row = max(daily or hourly, key=lambda row: row[2], default=None)
    chart_high = chart_row[2] if chart_row else None
    chart_high_at = datetime.fromtimestamp(chart_row[0], tz=timezone.utc) if chart_row else None

    changes: dict[str, float | None] = {
        "7d": percent_change_from_days(daily, 7),
        "14d": percent_change_from_days(daily, 14),
        "30d": percent_change_from_days(daily, 30),
    }
    return TokenHistory(
        high_24h=high_24h,
        low_24h=low_24h,
        chart_high=chart_high,
        chart_high_at=chart_high_at,
        changes=changes,
        chart_points=chart_points,
    )


def percent_change_from_days(rows: list[list[float]], days: int) -> float | None:
    valid_rows = sorted(valid_ohlcv_rows(rows), key=lambda row: row[0])
    if len(valid_rows) < 2:
        return None
    latest = valid_rows[-1]
    target_timestamp = latest[0] - days * 86_400
    candidates = [row for row in valid_rows if row[0] <= target_timestamp]
    if not candidates:
        return None
    latest_close = latest[4]
    past_close = candidates[-1][4]
    if latest_close <= 0 or past_close <= 0:
        return None
    return ((latest_close - past_close) / past_close) * 100


def format_token_report(pair: TokenPair, history: TokenHistory, ath: TokenAth | None) -> str:
    safe_name = html.escape(pair.name)
    safe_symbol = html.escape(pair.symbol.upper())
    market_cap = pair.market_cap if pair.market_cap is not None else pair.fdv
    market_label = "Market cap" if pair.market_cap is not None else "FDV"
    h1 = pair.price_change.get("h1")
    h24 = pair.price_change.get("h24")
    changes = history.changes or {}

    lines = [
        f"🪙 <b>{safe_name}</b> <b>${safe_symbol}</b>",
        "",
        f"💵 <b>Price</b>\n{format_price(pair.price_usd)}",
        "",
        f"🏦 <b>{market_label}</b>\n{format_money(market_cap)} {format_change_inline(h24)}",
        "",
        "📊 <b>24H Range</b>",
        f"High: <b>{format_price(history.high_24h)}</b>",
        f"Low: <b>{format_price(history.low_24h)}</b>",
        "",
        "🏆 <b>All-Time High</b>",
        f"{format_price(ath.price_usd if ath else None)}",
        f"<i>{format_date(ath.reached_at) if ath else 'Unavailable from a complete-history source'}</i>",
        "",
        "📈 <b>Performance</b>",
        f"1H: <b>{format_percent(h1)}</b>",
        f"24H: <b>{format_percent(h24)}</b>",
        f"7D: <b>{format_percent(changes.get('7d'))}</b>",
        f"14D: <b>{format_percent(changes.get('14d'))}</b>",
        f"30D: <b>{format_percent(changes.get('30d'))}</b>",
    ]
    return "\n".join(lines)


def format_money(value: float | None) -> str:
    if value is None:
        return "N/A"
    n = abs(float(value))
    sign = "-" if value < 0 else ""
    for divider, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= divider:
            return f"${sign}{n / divider:.2f}{suffix}"
    return f"${sign}{n:,.2f}"


def format_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    value = float(value)
    if value >= 1:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"${value:.5f}".rstrip("0").rstrip(".")
    if value >= 0.0001:
        return f"${value:.6f}".rstrip("0").rstrip(".")
    return f"${value:.10f}".rstrip("0").rstrip(".")


def format_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def format_change_inline(value: float | None) -> str:
    if value is None:
        return ""
    return f"({format_percent(value)})"


def format_date(value: datetime | None) -> str:
    if value is None:
        return "N/A"
    return value.strftime("%d %b %Y")


def float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def positive_float_or_none(value: Any) -> float | None:
    parsed = float_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def valid_ohlcv_rows(rows: list[list[float]]) -> list[list[float]]:
    valid: list[list[float]] = []
    for row in rows:
        if len(row) < 6 or not all(math.isfinite(value) for value in row[:6]):
            continue
        timestamp, open_price, high, low, close, volume = row[:6]
        if timestamp <= 0 or min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            continue
        valid.append(row[:6])
    return valid


def canonical_ton_address(value: str) -> str | None:
    address = value.strip()
    raw_match = re.fullmatch(r"(-?\d+):([0-9a-fA-F]{64})", address)
    if raw_match:
        return f"{int(raw_match.group(1))}:{raw_match.group(2).lower()}"
    if not re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}", address):
        return None
    try:
        decoded = base64.urlsafe_b64decode(address + "=" * (-len(address) % 4))
    except (ValueError, TypeError):
        return None
    if len(decoded) != 36:
        return None
    workchain = int.from_bytes(decoded[1:2], byteorder="big", signed=True)
    return f"{workchain}:{decoded[2:34].hex()}"


def same_ton_address(left: str, right: str) -> bool:
    canonical_left = canonical_ton_address(left)
    canonical_right = canonical_ton_address(right)
    if canonical_left is not None and canonical_right is not None:
        return canonical_left == canonical_right
    return left.strip() == right.strip()


def parse_coingecko_market_ath(payload: dict[str, Any]) -> TokenAth | None:
    market_data = payload.get("market_data") if isinstance(payload.get("market_data"), dict) else {}
    ath_values = market_data.get("ath") if isinstance(market_data.get("ath"), dict) else {}
    ath_dates = market_data.get("ath_date") if isinstance(market_data.get("ath_date"), dict) else {}
    price_usd = positive_float_or_none(ath_values.get("usd"))
    raw_date = str(ath_dates.get("usd") or "").strip()
    if price_usd is None or not raw_date:
        return None
    try:
        reached_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if reached_at.tzinfo is None:
        reached_at = reached_at.replace(tzinfo=timezone.utc)
    return TokenAth(price_usd=price_usd, reached_at=reached_at)


def parse_coingecko_ath(payload: dict[str, Any], expected_token_address: str) -> TokenAth | None:
    platform = str(payload.get("asset_platform_id") or "").strip().lower()
    contract_address = str(payload.get("contract_address") or "").strip()
    if platform != "the-open-network" or not same_ton_address(contract_address, expected_token_address):
        return None
    return parse_coingecko_market_ath(payload)


def parse_coingecko_coin_ath(payload: dict[str, Any], expected_coin_id: str) -> TokenAth | None:
    if str(payload.get("id") or "").strip().lower() != expected_coin_id.strip().lower():
        return None
    return parse_coingecko_market_ath(payload)


def choice_button_label(choice: TokenChoice) -> str:
    symbol = choice.symbol.upper()
    name = choice.name
    liquidity = format_money(choice.liquidity_usd).replace("$", "")
    address = choice.token_address
    short_address = f"{address[:5]}...{address[-4:]}" if len(address) > 12 else address
    label = f"{symbol} · {name} · {liquidity} liq · {short_address}"
    return label[:60]


def choices_text(query: str) -> str:
    safe_query = html.escape(query.strip())
    return (
        "🔎 <b>Multiple TON tokens found</b>\n\n"
        f"Search: <b>{safe_query}</b>\n"
        "Choose the exact token below. This prevents the bot from showing data for a fake token with the same ticker."
    )
