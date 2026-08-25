from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import socket
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from io import BytesIO
from typing import Any

import aiohttp
from dotenv import load_dotenv

from .alert_service import (
    AlertDraft,
    AlertStore,
    UserAlert,
    direction_label,
    format_metric_value,
    metric_label,
    metric_value,
    parse_alert_target,
    threshold_reached,
    utc_now_iso,
)
from .config import COINS, LOGO_DIR, OUTPUT_PATH
from .chart_service import DashboardChartService
from .conversion_renderer import ConversionCardData, ConversionRenderer
from .holder_service import HolderService
from .formatting import format_change, format_compact_number
from .inline_mode import (
    DEFAULT_INLINE_COIN_MESSAGE,
    DEFAULT_INLINE_CONVERSION_MESSAGE,
    DEFAULT_INLINE_HELP_MESSAGE,
    FEATURED_INLINE_SYMBOLS,
    INLINE_COIN_NAMES,
    INLINE_LOGO_URLS,
    InlineCoin,
    InlineMessageTemplates,
    build_inline_results,
    help_result,
)
from .movement_alert import MovementEvent, MovementTracker
from .market_overview import (
    MarketOverviewStateStore,
    OVERVIEW_TICKERS,
    PRICE_CHANNELS,
    format_interval,
    parse_interval_minutes,
)
from .new_tokens_service import (
    NewToken,
    NewTokenFilters,
    NewTokensService,
    NewTokensSnapshot,
)
from .price_service import PriceService
from .pulse_market import PulseMarketService
from .pulse_service import PulseBuy, PulseCoinValue, PulseEvent, PulseResult, PulseService
from .renderer import DashboardRenderer
from .token_card_renderer import TokenCardData, TokenCardRenderer
from .token_report import (
    KNOWN_TOKEN_ADDRESSES,
    TokenAth,
    TokenChoice,
    TokenPair,
    TokenReportResult,
    TokenReportService,
    choice_button_label,
    format_price,
    normalize_query,
    same_ton_address,
    select_best_pair,
)
from .trending_service import (
    DEFAULT_BLOCKED_ADDRESSES,
    DEFAULT_BLOCKED_SYMBOLS,
    TrendingService,
    TrendingSnapshot,
)


MAX_CONVERSION_AMOUNT = Decimal("10000000")
INLINE_RESULTS_BUTTON_TEXT = "What can this bot do?"
INLINE_RESULTS_START_PARAMETER = "inline_start"


def parse_gram_amount(raw_amount: str) -> Decimal:
    amount_text = str(raw_amount or "").replace(",", "").replace("_", "").strip()
    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise ValueError("The GRAM amount must be a valid number.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("The GRAM amount must be greater than zero.")
    if amount > MAX_CONVERSION_AMOUNT:
        raise ValueError("The GRAM amount is too large.")
    return amount


def parse_conversion_args(raw_args: str) -> tuple[str, Decimal]:
    parts = str(raw_args or "").strip().rsplit(maxsplit=1)
    if len(parts) != 2 or not parts[0].strip():
        raise ValueError("Use /swap <token name> <GRAM amount>.")

    query = parts[0].strip()
    return query, parse_gram_amount(parts[1])


def parse_conversion_request(raw_args: str) -> tuple[str | None, Decimal]:
    value = str(raw_args or "").strip()
    if value and len(value.split()) == 1:
        return None, parse_gram_amount(value)
    query, amount = parse_conversion_args(value)
    return query, amount


def calculate_token_amount(gram_amount: Decimal, gram_price_usd: float, token_price_usd: float) -> Decimal:
    gram_price = Decimal(str(gram_price_usd))
    token_price = Decimal(str(token_price_usd))
    if not gram_price.is_finite() or gram_price <= 0:
        raise ValueError("The live GRAM price is unavailable.")
    if not token_price.is_finite() or token_price <= 0:
        raise ValueError("The live token price is unavailable.")
    with localcontext() as context:
        context.prec = 40
        return (gram_amount * gram_price) / token_price


class TelegramDashboardBot:
    NATIVE_TON_ATH_QUERIES = frozenset({"gram", "ton", "toncoin", "the open network"})
    ALLOWED_UPDATES = ("message", "callback_query", "inline_query", "my_chat_member")
    TOKEN_REPORT_PREWARM_QUERIES = ("utya", "redo")
    UTYA_MOVEMENT_CHANNEL = "@utyachat"
    PULSE_EMOJI_SETTING_PREFIX = "pulse_emoji:"
    PULSE_COIN_EMOJI_SETTING_PREFIX = "pulse_coin_emoji:"
    PULSE_EMOJI_DEFAULTS = {
        "large_buy": "🐋",
        "buy_pressure": "🟢",
        "volume": "📊",
        "holders": "👥",
        "price_up": "🔥",
        "price_down": "📉",
        "high": "🚀",
        "low": "⚠️",
        "market_cap": "🏁",
    }
    PULSE_EMOJI_LABELS = {
        "large_buy": "Large buy",
        "buy_pressure": "Buy pressure",
        "volume": "Volume spike",
        "holders": "Holder growth",
        "price_up": "Price spike",
        "price_down": "Sudden price drop",
        "high": "New 24H high",
        "low": "New 24H low",
        "market_cap": "Market-cap crossing",
    }
    PULSE_COIN_EMOJI_DEFAULTS = {
        "UTYA": '<tg-emoji emoji-id="6257998990743183503">🤗</tg-emoji>',
        "REDO": '<tg-emoji emoji-id="6260258912340025212">🐶</tg-emoji>',
        "SCAT": '<tg-emoji emoji-id="6260227425934777920">🐈‍⬛</tg-emoji>',
        "YODA": '<tg-emoji emoji-id="6260032391469867220">🤗</tg-emoji>',
        "CHERRY": '<tg-emoji emoji-id="6260006535766745449">🍒</tg-emoji>',
        "BCHERRY": '<tg-emoji emoji-id="6260006535766745449">🍒</tg-emoji>',
        "MTONGA": '<tg-emoji emoji-id="6264645611547531835">🙂</tg-emoji>',
        "GROYP": '<tg-emoji emoji-id="6264769864951405532">🙂</tg-emoji>',
        "GRAMMING": '<tg-emoji emoji-id="6264553729312170296">🙂</tg-emoji>',
        "GRM": '<tg-emoji emoji-id="5248979506395367605">🤑</tg-emoji>',
        "GRAM": "💎",
    }
    DEFAULT_WELCOME_MESSAGE = (
        "✅ <b>Subscription confirmed</b>\n\n"
        "Send /meme to receive the latest meme price dashboard.\n"
        "Send /ath TOKEN to view a TON token's all-time high."
    )
    DEFAULT_SUBSCRIPTION_MESSAGE = (
        "🔒 <b>Subscription required</b>\n\n"
        "You must be subscribed to <b>@memeprice</b> to use /meme.\n"
        "Join the channel, then try again."
    )
    DEFAULT_HELP_MESSAGE = (
        "💎 <b>Meme Price Bot</b>\n\n"
        "<b>Public commands</b>\n"
        "• <code>/meme</code> — latest meme-price dashboard\n"
        "• <code>/meme utya</code> — detailed report for any TON token\n"
        "• <code>/ath utya</code> — all-time high for any TON token\n"
        "• <code>/swap 100</code> — convert GRAM to USD\n"
        "• <code>/swap utya 100</code> — convert GRAM to a TON token\n"
        "• <code>/trending</code> — top trending TON meme coins over 24 hours\n"
        "• <code>/pulse</code> — latest buys, volume, holder, and price movements\n"
        "• <code>/new</code> — TON tokens newly verified during the last 7 days\n"
        "• <code>/alert</code> — manage private token alerts\n"
        "\n🪄 <b>Inline mode</b>\n"
        "• <code>@memepricesbot</code> — view and share all supported prices in any chat\n"
        "• <code>@memepricesbot SCAT</code> — find one coin and share its statistics\n"
        "• <code>@memepricesbot 100 USD to GRAM</code> — convert between USD, GRAM, and supported tokens\n\n"
        "• <code>/help</code> — show this guide"
    )
    DEFAULT_PRIVATE_HELP_MESSAGE = (
        "💎 <b>Meme Price Bot</b>\n\n"
        "<b>Private commands</b>\n"
        "• <code>/meme</code> — latest meme-price dashboard\n"
        "• <code>/meme utya</code> — detailed report for any TON token\n"
        "• <code>/ath utya</code> — all-time high for any TON token\n"
        "• <code>/swap 100</code> — convert GRAM to USD\n"
        "• <code>/swap utya 100</code> — convert GRAM to a TON token\n"
        "• <code>/trending</code> — top trending TON meme coins over 24 hours\n"
        "• <code>/pulse</code> — latest buys, volume, holder, and price movements\n"
        "• <code>/new</code> — TON tokens newly verified during the last 7 days\n"
        "• <code>/alert</code> — manage private token alerts\n"
        "\n🪄 <b>Inline mode</b>\n"
        "• <code>@memepricesbot</code> — view and share all supported prices in any chat\n"
        "• <code>@memepricesbot SCAT</code> — find one coin and share its statistics\n"
        "• <code>@memepricesbot 100 USD to GRAM</code> — convert between USD, GRAM, and supported tokens\n\n"
        "• <code>/guide</code> — beginner's TON meme coin guide\n"
        "• <code>/help</code> — show this command list"
    )
    DEFAULT_GUIDE_PRIVATE_ONLY_MESSAGE = (
        "🏳️ <b>TON Meme Coin Guide</b>\n\n"
        "This guide works only in private messages. Open @memepricesbot privately to continue."
    )
    DEFAULT_GUIDE_HOME_MESSAGE = (
        "🏳️ <b>TON Meme Coin Guide</b>\n\n"
        "New to meme coins?\n\n"
        "Learn the basics, understand the market information shown by the bot, and discover how to "
        "research tokens more carefully.\n\n"
        "Choose a topic below:"
    )
    DEFAULT_GUIDE_START_MESSAGE = (
        "🚀 <b>Getting Started</b>\n\n"
        "Meme coins are community-driven tokens whose prices can change very quickly.\n\n"
        "A basic way to get started:\n\n"
        "1. Set up a wallet that supports the TON blockchain.\n"
        "2. Add GRAM to your wallet.\n"
        "3. Find a token you are interested in.\n"
        "4. Verify its correct contract address.\n"
        "5. Research its market data and community.\n"
        "6. Start with a small amount.\n"
        "7. Track the token through @memepricesbot.\n\n"
        "Meme coins are highly speculative. Never invest money you cannot afford to lose."
    )
    DEFAULT_GUIDE_RESEARCH_MESSAGE = (
        "🔍 <b>Researching a Token</b>\n\n"
        "Before buying a meme coin, check more than its name or recent price increase.\n\n"
        "Important things to review:\n\n"
        "• Contract address, always verify that you are looking at the correct token.\n"
        "• Market capitalization, the estimated total market value of the token.\n"
        "• Liquidity, the funds available for buying and selling.\n"
        "• Trading volume, the value traded during a selected period.\n"
        "• Price movement, including both gains and losses.\n"
        "• Token age, newer tokens generally carry more uncertainty.\n"
        "• Community activity, check whether the project has active and credible social accounts.\n\n"
        "Use <code>/meme TOKEN</code> to search for a TON token by its name, ticker, or contract address.\n\n"
        "Example:\n\n"
        "<code>/meme UTYA</code>\n\n"
        "The bot must not claim that any token is safe or legitimate."
    )
    DEFAULT_GUIDE_RESEARCH_SEARCH_MESSAGE = (
        "🔎 <b>Search for a Token</b>\n\n"
        "Send:\n\n"
        "<code>/meme TOKEN</code>\n\n"
        "Replace TOKEN with the token name, ticker, or contract address.\n\n"
        "Examples:\n\n"
        "<code>/meme UTYA</code>\n"
        "<code>/meme REDO</code>"
    )
    DEFAULT_GUIDE_SAFETY_MESSAGE = (
        "🛡 <b>Staying Safe</b>\n\n"
        "No single check can prove that a meme coin is safe.\n\n"
        "Before buying:\n\n"
        "• Verify the official contract address.\n"
        "• Be cautious when a few wallets control a large part of the supply.\n"
        "• Avoid tokens with extremely low liquidity.\n"
        "• Check whether the project has active social accounts and a real community.\n"
        "• Do not trust guaranteed-profit claims.\n"
        "• Never share your seed phrase or private key.\n"
        "• Do not connect your wallet to unknown websites or applications.\n"
        "• Start with a small transaction when using an unfamiliar token or service.\n\n"
        "A token can be trending, have high volume, or increase rapidly in price and still be manipulated "
        "or fraudulent.\n\n"
        "The information provided by @memepricesbot is for informational purposes only. It must not be "
        "presented as financial advice or a safety guarantee."
    )
    DEFAULT_GUIDE_TRADING_MESSAGE = (
        "💱 <b>Buying &amp; Selling</b>\n\n"
        "TON meme coins are usually traded through decentralized exchanges.\n\n"
        "Before confirming a swap:\n\n"
        "• Verify the token contract address.\n"
        "• Check how many tokens you will receive.\n"
        "• Review the price impact.\n"
        "• Avoid unnecessarily high slippage.\n"
        "• Keep some GRAM in your wallet for network fees.\n"
        "• Consider testing an unfamiliar token with a small transaction first.\n\n"
        "You can use the converter to estimate how much of a token a selected GRAM amount represents:\n\n"
        "<code>/swap TOKEN AMOUNT</code>\n\n"
        "Example:\n\n"
        "<code>/swap UTYA 100</code>\n\n"
        "The converter provides an estimate based on available live market prices. It does not execute a "
        "purchase or swap."
    )
    DEFAULT_GUIDE_CONVERTER_MESSAGE = (
        "💱 <b>Token Converter</b>\n\n"
        "Use:\n\n"
        "<code>/swap TOKEN AMOUNT</code>\n\n"
        "TOKEN is the token you want to calculate.\n\n"
        "AMOUNT is the amount of GRAM you want to convert.\n\n"
        "Example:\n\n"
        "<code>/swap UTYA 100</code>\n\n"
        "The result is an estimate based on the current available market price."
    )
    DEFAULT_GUIDE_INLINE_MESSAGE = (
        "🪄 <b>Inline Mode</b>\n\n"
        "Type <code>@memepricesbot</code> in any Telegram chat—even when the bot is not a member—and "
        "choose a result to insert it into the conversation.\n\n"
        "<b>Available features</b>\n"
        "• Leave the query empty to view GRAM, UTYA, REDO, SCAT, YODA, CHERRY, BCHERRY, MTONGA, GROYP, "
        "GRAMMING, and GRM.\n"
        "• Enter a ticker or token name to find one supported coin.\n"
        "• Select a coin to share its USD price, 24-hour change, ATH, holder count, and market cap.\n"
        "• Convert between USD, GRAM, and every supported token in either direction.\n"
        "• Use TON or TONCOIN as aliases for GRAM.\n\n"
        "<b>Examples</b>\n"
        "<code>@memepricesbot SCAT</code>\n"
        "<code>@memepricesbot 100 USD to GRAM</code>\n"
        "<code>@memepricesbot 1000 GRM to UTYA</code>\n"
        "<code>@memepricesbot 250 TON to YODA</code>\n\n"
        "Inline conversions display current market calculations; they do not execute a trade."
    )
    DEFAULT_GUIDE_COMMANDS_MESSAGE = (
        "📊 <b>Using Meme Prices Bot</b>\n\n"
        "<code>/meme</code>\n\n"
        "View the main dashboard for UTYA, REDO, SCAT, YODA, CHERRY, MTONGA, GROYP, GRAMMING, and GRM.\n\n"
        "<code>/meme TOKEN</code>\n\n"
        "Search for any available TON token by its name, ticker, or contract address.\n\n"
        "Example:\n\n"
        "<code>/meme UTYA</code>\n\n"
        "<code>/ath TOKEN</code>\n\n"
        "View the token's recorded all-time high.\n\n"
        "Example:\n\n"
        "<code>/ath UTYA</code>\n\n"
        "<code>/swap TOKEN AMOUNT</code>\n\n"
        "Estimate how much of a selected token a GRAM amount represents.\n\n"
        "Example:\n\n"
        "<code>/swap UTYA 100</code>\n\n"
        "<code>/trending</code>\n\n"
        "View TON meme tokens currently trending based on available GeckoTerminal data.\n\n"
        "<code>/pulse</code>\n\n"
        "View the latest large buys, buy pressure, volume spikes, holder growth, and sudden price "
        "movements across tracked TON meme coins.\n\n"
        "<code>/new</code>\n\n"
        "View TON tokens newly added to Tonkeeper's reviewed asset list during the last 7 days.\n\n"
        "<code>@memepricesbot</code>\n\n"
        "Open inline mode in any chat to share supported coin statistics or convert between USD, GRAM, "
        "and supported tokens. Add a ticker, token name, or conversion after the bot username.\n\n"
        "Market information comes from external data providers. When information is unavailable, the bot "
        "must leave it unavailable instead of inventing or estimating data."
    )
    DEFAULT_GUIDE_NEW_MESSAGE = (
        "🆕 <b>Newly Verified TON Tokens</b>\n\n"
        "Use <code>/new</code> to find tokens newly added to Tonkeeper's reviewed TON asset list during "
        "the last 7 days. Results show verification age, market cap or FDV, liquidity, 24-hour volume, "
        "holders when available, contract address, and the token's strongest available DEX pool.\n\n"
        "Only reviewed list additions are displayed, and graylisted or blacklisted tokens are removed. "
        "When a pool has no reported market cap, the bot labels and uses FDV instead. Provider "
        "verification is not a safety guarantee; always verify the contract and liquidity yourself."
    )
    DEFAULT_GUIDE_TERMS_MESSAGE = (
        "📖 <b>Common Meme Coin Terms</b>\n\n"
        "<b>Market cap</b>\n"
        "The estimated total value of the token’s circulating supply.\n\n"
        "<b>Liquidity</b>\n"
        "Funds available in a trading pool for buying and selling the token.\n\n"
        "<b>Volume</b>\n"
        "The total value traded during a selected period.\n\n"
        "<b>Price impact</b>\n"
        "The effect a trade has on the token’s execution price.\n\n"
        "<b>Slippage</b>\n"
        "The difference between the expected swap price and the final price.\n\n"
        "<b>Contract address</b>\n"
        "The unique blockchain address identifying a token.\n\n"
        "<b>Holder</b>\n"
        "A wallet that owns the token.\n\n"
        "<b>Whale</b>\n"
        "A wallet holding a large amount of the token supply.\n\n"
        "<b>Rug pull</b>\n"
        "A situation where creators remove liquidity, sell a large supply, or abandon the project after "
        "attracting buyers.\n\n"
        "<b>Honeypot</b>\n"
        "A malicious token that may be easy to buy but difficult or impossible to sell normally.\n\n"
        "<b>DEX</b>\n"
        "A decentralized exchange used to trade tokens directly through a blockchain wallet."
    )
    DEFAULT_GUIDE_LIVE_CHANNELS_MESSAGE = (
        "📡 <b>Live Price Channels</b>\n\n"
        "Follow the dedicated Meme Prices channels to receive live price updates for nine TON tokens "
        "directly on Telegram.\n\n"
        "Each channel automatically publishes the token’s latest price and market updates.\n\n"
        "Choose a token below to open its live price channel:"
    )
    PUBLIC_BUTTON_DEFAULTS = {
        "subscription_join": "📢 Join [CHANNEL]",
        "subscription_check": "✅ I joined",
        "guide_private_open": "🏳️ Open private guide",
        "guide_start": "🚀 Getting Started",
        "guide_research": "🔍 Researching a Token",
        "guide_safety": "🛡 Staying Safe",
        "guide_trading": "💱 Buying & Selling",
        "guide_commands": "📊 Using the Bot",
        "guide_inline": "🪄 Inline Mode",
        "guide_terms": "📖 Common Terms",
        "guide_live_channels": "📡 Live Price Channels",
        "guide_add_channels": "📁 Add All Channels",
        "guide_close": "❌ Close",
        "guide_search": "🔎 Search a Token",
        "guide_converter": "💱 Converter Help",
        "guide_trending": "🔥 View Trending",
        "guide_new": "🆕 Newly Verified Tokens",
        "guide_back": "⬅️ Back",
        "token_choice": "[CHOICE]",
        "conversion_choice": "[CHOICE]",
        "alert_token_choice": "[CHOICE]",
        "alert_create": "➕ Create alert",
        "alert_list": "📋 My alerts",
        "alert_help": "❓ How alerts work",
        "alert_refresh": "🔄 Refresh",
        "alert_cancel_setup": "✖️ Cancel setup",
        "alert_home": "⬅️ Back",
        "alert_price": "💵 Price",
        "alert_market_cap": "🏦 Market cap",
        "alert_change_24h": "📊 24H change",
        "alert_cancel": "✖️ Cancel",
        "alert_above": "📈 At or above",
        "alert_below": "📉 At or below",
        "alert_change_metric": "⬅️ Back",
        "alert_back_direction": "⬅️ Back",
        "alert_save": "✅ Save alert",
        "alert_change_target": "⬅️ Back",
        "alert_delete": "🗑️ Delete alert",
        "alert_keep": "⬅️ Back",
        "alert_pause": "⏸️ Pause alert",
        "alert_rearm": "▶️ Re-arm alert",
        "alert_open_chart": "📈 Open chart",
        "alert_previous": "⬅️ Previous",
        "alert_next": "Next ➡️",
        "alert_manage": "🔔 Manage alerts",
        "alert_item": "[STATUS] $[TOKEN_SYMBOL] · [METRIC] · [TARGET]",
    }
    PUBLIC_BUTTON_LABELS = {
        "subscription_join": "join required channel",
        "subscription_check": "confirm subscription",
        "guide_private_open": "open private guide",
        "guide_start": "guide: getting started",
        "guide_research": "guide: research",
        "guide_safety": "guide: safety",
        "guide_trading": "guide: buying and selling",
        "guide_commands": "guide: using the bot",
        "guide_inline": "guide: inline mode",
        "guide_terms": "guide: common terms",
        "guide_live_channels": "guide: live price channels",
        "guide_add_channels": "guide: add all channels",
        "guide_close": "guide: close",
        "guide_search": "guide: search token",
        "guide_converter": "guide: converter help",
        "guide_trending": "guide: trending",
        "guide_new": "guide: new TON tokens",
        "guide_back": "guide: back",
        "token_choice": "token search result",
        "conversion_choice": "converter token result",
        "alert_token_choice": "alert token result",
        "alert_create": "alerts: create",
        "alert_list": "alerts: saved alerts",
        "alert_help": "alerts: help",
        "alert_refresh": "alerts: refresh",
        "alert_cancel_setup": "alerts: cancel setup",
        "alert_home": "alerts: back",
        "alert_price": "alerts: price",
        "alert_market_cap": "alerts: market cap",
        "alert_change_24h": "alerts: 24H change",
        "alert_cancel": "alerts: cancel",
        "alert_above": "alerts: at or above",
        "alert_below": "alerts: at or below",
        "alert_change_metric": "alerts: back to metric",
        "alert_back_direction": "alerts: back to direction",
        "alert_save": "alerts: save",
        "alert_change_target": "alerts: back to target",
        "alert_delete": "alerts: delete",
        "alert_keep": "alerts: back from deletion",
        "alert_pause": "alerts: pause",
        "alert_rearm": "alerts: re-arm",
        "alert_open_chart": "alerts: open chart",
        "alert_previous": "alerts: previous page",
        "alert_next": "alerts: next page",
        "alert_manage": "alerts: manage",
        "alert_item": "saved alert row",
    }
    PUBLIC_BUTTON_REQUIRED_PLACEHOLDERS = {
        "subscription_join": ("[CHANNEL]",),
        "token_choice": ("[CHOICE]",),
        "conversion_choice": ("[CHOICE]",),
        "alert_token_choice": ("[CHOICE]",),
        "alert_item": ("[STATUS]", "[TOKEN_SYMBOL]", "[METRIC]", "[TARGET]"),
    }
    PUBLIC_BUTTON_GROUPS = {
        "access": ("subscription_join", "subscription_check", "guide_private_open"),
        "guide": (
            "guide_start",
            "guide_research",
            "guide_safety",
            "guide_trading",
            "guide_commands",
            "guide_inline",
            "guide_terms",
            "guide_live_channels",
            "guide_add_channels",
            "guide_search",
            "guide_converter",
            "guide_trending",
            "guide_new",
            "guide_back",
            "guide_close",
        ),
        "token": ("token_choice", "conversion_choice", "alert_token_choice"),
        "alert_home": (
            "alert_create",
            "alert_list",
            "alert_help",
            "alert_refresh",
            "alert_cancel_setup",
            "alert_home",
        ),
        "alert_setup": (
            "alert_price",
            "alert_market_cap",
            "alert_change_24h",
            "alert_cancel",
            "alert_above",
            "alert_below",
            "alert_change_metric",
            "alert_back_direction",
            "alert_save",
            "alert_change_target",
        ),
        "alert_manage": (
            "alert_delete",
            "alert_keep",
            "alert_pause",
            "alert_rearm",
            "alert_open_chart",
            "alert_previous",
            "alert_next",
            "alert_manage",
            "alert_item",
        ),
    }
    PUBLIC_BUTTON_GROUP_LABELS = {
        "access": "🔐 Access buttons",
        "guide": "🏳️ Guide buttons",
        "token": "🪙 Token-choice buttons",
        "alert_home": "🔔 Alert home buttons",
        "alert_setup": "🎯 Alert setup buttons",
        "alert_manage": "📋 Alert management buttons",
    }
    DEFAULT_RATE_LIMIT_MESSAGE = "⏳ Too fast! Wait <b>[WAIT_SECONDS]s</b> to check the prices again."
    DEFAULT_TRENDING_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>Trending list unavailable</b>\n\n"
        "The current 24-hour snapshot could not be loaded. Try again shortly."
    )
    DEFAULT_NEW_MESSAGE = (
        "🆕 <b>New TON Tokens</b>\n\n"
        "[LIST]\n\n"
        "Updated: <b>[UPDATED_AT]</b>\n"
        "<i>Tonkeeper verification is not a safety guarantee. Verify the contract and liquidity before trading.</i>"
    )
    DEFAULT_NEW_ROW = (
        "[RANK]. <b>$[TICKER] — [NAME]</b>\n"
        "├ 📊 [VALUATION_LABEL]: [MARKET_CAP]\n"
        "└ 👥 Holders: [HOLDERS]"
    )
    DEFAULT_NEW_USAGE_MESSAGE = (
        "⚠️ <b>Use /new without additional text</b>\n\n"
        "Send <code>/new</code> to view TON tokens newly verified during the last 7 days."
    )
    DEFAULT_NEW_EMPTY_MESSAGE = (
        "🔎 <b>No newly verified tokens found</b>\n\n"
        "No reviewed TON asset-list additions were found during the last 7 days.\n\n"
        "Try again later."
    )
    DEFAULT_NEW_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>Newly verified feed unavailable</b>\n\nTry again shortly."
    )
    DEFAULT_TOKEN_REPORT_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>Token report unavailable</b>\n\n"
        "I could not load this market report right now. Try again in a few seconds."
    )
    DEFAULT_TOKEN_CHOICES_MESSAGE = (
        "🔎 <b>Multiple TON tokens found</b>\n\n"
        "Search: <b>[QUERY]</b>\n"
        "Choose the exact token below to avoid selecting a different token with the same ticker."
    )
    DEFAULT_TOKEN_NOT_FOUND_MESSAGE = (
        "🔎 <b>Token not found</b>\n\n"
        "I could not find a TON token for <b>[QUERY]</b>.\n\n"
        "Try its exact ticker, full name, or contract address."
    )
    DEFAULT_ATH_USAGE_MESSAGE = (
        "Use <code>/ath TOKEN</code> to view a TON token's all-time high.\n\n"
        "Examples: <code>/ath UTYA</code> or <code>/ath GRAM</code>"
    )
    DEFAULT_ATH_RESULT_MESSAGE = "<b>[TOKEN_SYMBOL] ATH</b> - <b>[ATH_PRICE]</b>."
    DEFAULT_ATH_UNAVAILABLE_MESSAGE = "<b>[TOKEN_SYMBOL] ATH</b> - unavailable."
    DEFAULT_ATH_LOOKUP_UNAVAILABLE_MESSAGE = (
        "⚠️ ATH lookup is temporarily unavailable. Try again shortly."
    )
    DEFAULT_CONVERSION_USAGE_MESSAGE = (
        "🔄 <b>GRAM Converter</b>\n\n"
        "[ERROR]\n\n"
        "Examples:\n"
        "• <code>/swap 100</code> — GRAM to USD\n"
        "• <code>/swap utya 100</code> — GRAM to UTYA"
    )
    DEFAULT_CONVERSION_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>Conversion unavailable</b>\n\n[ERROR]"
    )
    DEFAULT_CONVERSION_NOT_FOUND_MESSAGE = (
        "🔎 <b>Token not found</b>\n\n"
        "I could not find a priced TON token for <b>[QUERY]</b>.\n\n"
        "Try its exact ticker, full name, or contract address."
    )
    DEFAULT_TRENDING_TITLE = "🔥 <b>TON Meme Trends · 24H</b>"
    DEFAULT_PULSE_TITLE = "⚡️ <b>TON MEME PULSE · LIVE</b>"
    DEFAULT_PULSE_MESSAGE = "[TITLE]\n\n[EVENTS]\n\n🕒 Updated: [UPDATED_AT]"
    DEFAULT_PULSE_EVENT = "[EMOJI] <b>$[TICKER]</b> · <i>[AGE]</i>\n[DETAILS]"
    DEFAULT_PULSE_NORMAL_MESSAGE = (
        "[TITLE]\n\n"
        "No major movements are dominating the market right now.\n\n"
        "🕒 Updated: [UPDATED_AT]"
    )
    DEFAULT_PULSE_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>TON Meme Pulse unavailable</b>\n\n"
        "There is not enough current market data to build a reliable snapshot. Try again shortly."
    )
    DEFAULT_MARKET_OVERVIEW_MESSAGE = (
        "📊 <b>Market Overview:</b>\n\n"
        "[PRICES]\n\n"
        "📈 <b>Market Caps:</b>\n\n"
        "[MARKET_CAPS]"
    )
    DEFAULT_MARKET_OVERVIEW_PRICE_ROW = (
        '<a href="[CHANNEL_URL]">$[TICKER]</a> : <b>[PRICE]</b>'
    )
    DEFAULT_MARKET_OVERVIEW_CAP_ROW = (
        '<a href="[CHANNEL_URL]">$[TICKER]</a> : <b>[MARKET_CAP]</b> ([CHANGE_24H])'
    )
    DEFAULT_ALERT_HOME_MESSAGE = (
        "🔔 <b>Token Alerts</b>\n\n"
        "Create a private alert for a TON token's price, market cap, or 24H price change.\n\n"
        "Active alerts: <b>[ACTIVE_COUNT]/[MAX_ALERTS]</b>\n"
        "Saved alerts: <b>[SAVED_COUNT]</b>\n\n"
        "Alerts are checked automatically. A triggered alert pauses after one notification "
        "so it cannot repeatedly notify you."
    )
    DEFAULT_ALERT_SEARCH_MESSAGE = (
        "🔎 <b>Find a TON token</b>\n\n"
        "Send its symbol, name, or jetton master address.\n\n"
        "Examples: <b>UTYA</b>, <b>REDO</b>, or an <b>EQ...</b> address."
    )
    DEFAULT_ALERT_CONFIRMATION_MESSAGE = (
        "✅ <b>Confirm Alert</b>\n\n"
        "Token: <b>[TOKEN_NAME] ($[TOKEN_SYMBOL])</b>\n"
        "Condition: <b>[METRIC]</b> [CONDITION]\n"
        "Target: <b>[TARGET]</b>\n"
        "Current: <b>[CURRENT]</b>\n\n"
        "The alert will notify you once and then pause."
    )
    DEFAULT_ALERT_TRIGGER_MESSAGE = (
        "🔔 <b>Price Alert Triggered</b>\n\n"
        "<b>[TOKEN_NAME] ($[TOKEN_SYMBOL])</b>\n"
        "Condition: [METRIC] [CONDITION] <b>[TARGET]</b>\n"
        "Current value: <b>[CURRENT]</b>\n\n"
        "This alert is now paused. Re-arm it from /alert if you want to use it again."
    )
    DEFAULT_ALERT_HELP_MESSAGE = (
        "❓ <b>How Token Alerts Work</b>\n\n"
        "1. Choose any TON token.\n"
        "2. Select price, market cap, or 24H change.\n"
        "3. Choose whether the value should rise above or fall below your target.\n"
        "4. Enter only the target value.\n\n"
        "Examples: <b>0.025</b>, <b>1.5M</b>, <b>10%</b>, or <b>-5%</b>.\n\n"
        "Checks run automatically. Market data can be delayed or temporarily unavailable. "
        "Triggered alerts pause after one notification and can be re-armed."
    )
    DEFAULT_PRIVATE_ALERTS_MESSAGE = (
        "🔔 <b>Private alerts</b>\n\n"
        "Open a private chat with @memepricesbot and send /alert."
    )
    DEFAULT_ALERT_TOKEN_SEARCH_UNAVAILABLE_MESSAGE = (
        "⚠️ Token search is temporarily unavailable. Try again shortly."
    )
    DEFAULT_ALERT_TOKEN_CHOICES_MESSAGE = (
        "🔎 <b>Choose the exact token</b>\n\n"
        "Several TON tokens match that search. Select the correct one below."
    )
    DEFAULT_ALERT_TOKEN_NOT_FOUND_MESSAGE = (
        "🚫 No TON token was found for <b>[QUERY]</b>.\n\n"
        "Send another symbol, name, or jetton master address."
    )
    DEFAULT_ALERT_INVALID_TARGET_MESSAGE = (
        "⚠️ <b>Invalid target</b>\n\n[ERROR]"
    )
    DEFAULT_ALERT_CONTINUE_MESSAGE = (
        "Use the buttons to continue, or send /cancel to stop."
    )
    DEFAULT_ALERT_METRIC_MESSAGE = (
        "🎯 <b>Choose what to monitor</b>\n\n"
        "Token: <b>[TOKEN_NAME] ($[TOKEN_SYMBOL])</b>\n"
        "Price: <b>[PRICE]</b>\n"
        "Market cap: <b>[MARKET_CAP]</b>\n"
        "24H change: <b>[CHANGE_24H]</b>"
    )
    DEFAULT_ALERT_SELECTION_UNAVAILABLE_MESSAGE = (
        "⚠️ Live data for this token is unavailable. Send another token."
    )
    DEFAULT_ALERT_LIVE_DATA_UNAVAILABLE_MESSAGE = (
        "⚠️ Live token data is temporarily unavailable. Try again."
    )
    DEFAULT_ALERT_METRIC_UNAVAILABLE_MESSAGE = (
        "⚠️ <b>[METRIC] is unavailable</b>\n\n"
        "Choose a different alert type for this token."
    )
    DEFAULT_ALERT_DIRECTION_MESSAGE = (
        "📍 <b>Choose the trigger direction</b>\n\n"
        "Token: <b>$[TOKEN_SYMBOL]</b>\n"
        "Monitoring: <b>[METRIC]</b>\n"
        "Current value: <b>[CURRENT]</b>"
    )
    DEFAULT_ALERT_METRIC_BACK_MESSAGE = (
        "🎯 <b>Choose what to monitor</b>\n\n"
        "Token: <b>[TOKEN_NAME] ($[TOKEN_SYMBOL])</b>"
    )
    DEFAULT_ALERT_TARGET_MESSAGE = (
        "✍️ <b>Enter the target value</b>\n\n"
        "Token: <b>$[TOKEN_SYMBOL]</b>\n"
        "Condition: [METRIC] [CONDITION]\n"
        "Current value: <b>[CURRENT]</b>\n\n"
        "Send only the value, for example <b>[EXAMPLE]</b>."
    )
    DEFAULT_ALERT_NEW_TARGET_MESSAGE = (
        "✍️ <b>Enter a new target value</b>\n\nSend only the number."
    )
    DEFAULT_ALERT_DELETE_CONFIRMATION_MESSAGE = (
        "🗑️ <b>Delete this alert?</b>\n\n"
        "$[TOKEN_SYMBOL] · [METRIC] · [TARGET]\n\n"
        "This cannot be undone."
    )
    DEFAULT_ALERT_LIST_EMPTY_MESSAGE = (
        "📋 <b>My Alerts</b>\n\nYou have no saved alerts yet."
    )
    DEFAULT_ALERT_LIST_MESSAGE = (
        "📋 <b>My Alerts</b>\n\n"
        "Page <b>[PAGE]/[TOTAL_PAGES]</b> · Total: <b>[TOTAL]</b>\n"
        "Select an alert to manage it."
    )
    DEFAULT_ALERT_DETAIL_MESSAGE = (
        "🔔 <b>Alert Details</b>\n\n"
        "Token: <b>[TOKEN_NAME] ($[TOKEN_SYMBOL])</b>\n"
        "Status: <b>[STATUS]</b>\n"
        "Condition: [METRIC] [CONDITION]\n"
        "Target: <b>[TARGET]</b>\n"
        "Latest value: <b>[LATEST]</b>\n"
        "Last checked: <b>[LAST_CHECKED]</b>"
    )
    DEFAULT_ALERT_ISSUE_MESSAGE = "\nIssue: [ERROR]"
    DEFAULT_ALERT_NOTE_MESSAGE = "\n\nℹ️ [NOTE]"
    DEFAULT_ALERT_CANCELLED_NOTE = "Alert setup cancelled."
    DEFAULT_ALERT_EXPIRED_NOTE = "The previous setup expired."
    DEFAULT_ALERT_SAVED_NOTE = "Alert saved and monitoring has started."
    DEFAULT_ALERT_DELETED_NOTE = "Alert deleted."
    DEFAULT_UTYA_MOVEMENT_UP_MESSAGE = (
        "🚀 <b>UTYA Price Movement</b>\n\n"
        "<b>$UTYA increased by [CHANGE_PERCENT]</b>\n"
        "Price: <b>[OLD_PRICE] → [NEW_PRICE]</b>\n\n"
        "🕒 [TIME_UTC]"
    )
    DEFAULT_UTYA_MOVEMENT_DOWN_MESSAGE = (
        "🔻 <b>UTYA Price Movement</b>\n\n"
        "<b>$UTYA decreased by [CHANGE_PERCENT]</b>\n"
        "Price: <b>[OLD_PRICE] → [NEW_PRICE]</b>\n\n"
        "🕒 [TIME_UTC]"
    )
    DEFAULT_TRENDING_MESSAGE = "[TITLE]\n\n[LIST]"
    DEFAULT_TRENDING_ROW = "[RANK]. <b>$[TICKER]</b>: <code>[CHANGE_24H]</code>"
    PUBLIC_MESSAGE_DEFAULTS = {
        "welcome_message": DEFAULT_WELCOME_MESSAGE,
        "subscription_message": DEFAULT_SUBSCRIPTION_MESSAGE,
        "help_message": DEFAULT_HELP_MESSAGE,
        "private_help_message": DEFAULT_PRIVATE_HELP_MESSAGE,
        "guide_private_only_message": DEFAULT_GUIDE_PRIVATE_ONLY_MESSAGE,
        "guide_home_message": DEFAULT_GUIDE_HOME_MESSAGE,
        "guide_start_message": DEFAULT_GUIDE_START_MESSAGE,
        "guide_research_message": DEFAULT_GUIDE_RESEARCH_MESSAGE,
        "guide_research_search_message": DEFAULT_GUIDE_RESEARCH_SEARCH_MESSAGE,
        "guide_safety_message": DEFAULT_GUIDE_SAFETY_MESSAGE,
        "guide_trading_message": DEFAULT_GUIDE_TRADING_MESSAGE,
        "guide_converter_message": DEFAULT_GUIDE_CONVERTER_MESSAGE,
        "guide_inline_message": DEFAULT_GUIDE_INLINE_MESSAGE,
        "guide_commands_message": DEFAULT_GUIDE_COMMANDS_MESSAGE,
        "guide_new_message": DEFAULT_GUIDE_NEW_MESSAGE,
        "guide_terms_message": DEFAULT_GUIDE_TERMS_MESSAGE,
        "guide_live_channels_message": DEFAULT_GUIDE_LIVE_CHANNELS_MESSAGE,
        "rate_limit_message": DEFAULT_RATE_LIMIT_MESSAGE,
        "trending_unavailable_message": DEFAULT_TRENDING_UNAVAILABLE_MESSAGE,
        "pulse_title": DEFAULT_PULSE_TITLE,
        "pulse_message": DEFAULT_PULSE_MESSAGE,
        "pulse_event": DEFAULT_PULSE_EVENT,
        "pulse_normal_message": DEFAULT_PULSE_NORMAL_MESSAGE,
        "pulse_unavailable_message": DEFAULT_PULSE_UNAVAILABLE_MESSAGE,
        "market_overview_message": DEFAULT_MARKET_OVERVIEW_MESSAGE,
        "market_overview_price_row": DEFAULT_MARKET_OVERVIEW_PRICE_ROW,
        "market_overview_cap_row": DEFAULT_MARKET_OVERVIEW_CAP_ROW,
        "new_message": DEFAULT_NEW_MESSAGE,
        "new_row": DEFAULT_NEW_ROW,
        "new_usage_message": DEFAULT_NEW_USAGE_MESSAGE,
        "new_empty_message": DEFAULT_NEW_EMPTY_MESSAGE,
        "new_unavailable_message": DEFAULT_NEW_UNAVAILABLE_MESSAGE,
        "token_report_unavailable_message": DEFAULT_TOKEN_REPORT_UNAVAILABLE_MESSAGE,
        "token_choices_message": DEFAULT_TOKEN_CHOICES_MESSAGE,
        "token_not_found_message": DEFAULT_TOKEN_NOT_FOUND_MESSAGE,
        "ath_usage_message": DEFAULT_ATH_USAGE_MESSAGE,
        "ath_result_message": DEFAULT_ATH_RESULT_MESSAGE,
        "ath_unavailable_message": DEFAULT_ATH_UNAVAILABLE_MESSAGE,
        "ath_lookup_unavailable_message": DEFAULT_ATH_LOOKUP_UNAVAILABLE_MESSAGE,
        "conversion_usage_message": DEFAULT_CONVERSION_USAGE_MESSAGE,
        "conversion_unavailable_message": DEFAULT_CONVERSION_UNAVAILABLE_MESSAGE,
        "conversion_not_found_message": DEFAULT_CONVERSION_NOT_FOUND_MESSAGE,
        "inline_coin_message": DEFAULT_INLINE_COIN_MESSAGE,
        "inline_conversion_message": DEFAULT_INLINE_CONVERSION_MESSAGE,
        "inline_help_message": DEFAULT_INLINE_HELP_MESSAGE,
        "alert_home_message": DEFAULT_ALERT_HOME_MESSAGE,
        "alert_search_message": DEFAULT_ALERT_SEARCH_MESSAGE,
        "alert_confirmation_message": DEFAULT_ALERT_CONFIRMATION_MESSAGE,
        "alert_trigger_message": DEFAULT_ALERT_TRIGGER_MESSAGE,
        "alert_help_message": DEFAULT_ALERT_HELP_MESSAGE,
        "private_alerts_message": DEFAULT_PRIVATE_ALERTS_MESSAGE,
        "alert_token_search_unavailable_message": DEFAULT_ALERT_TOKEN_SEARCH_UNAVAILABLE_MESSAGE,
        "alert_token_choices_message": DEFAULT_ALERT_TOKEN_CHOICES_MESSAGE,
        "alert_token_not_found_message": DEFAULT_ALERT_TOKEN_NOT_FOUND_MESSAGE,
        "alert_invalid_target_message": DEFAULT_ALERT_INVALID_TARGET_MESSAGE,
        "alert_continue_message": DEFAULT_ALERT_CONTINUE_MESSAGE,
        "alert_metric_message": DEFAULT_ALERT_METRIC_MESSAGE,
        "alert_selection_unavailable_message": DEFAULT_ALERT_SELECTION_UNAVAILABLE_MESSAGE,
        "alert_live_data_unavailable_message": DEFAULT_ALERT_LIVE_DATA_UNAVAILABLE_MESSAGE,
        "alert_metric_unavailable_message": DEFAULT_ALERT_METRIC_UNAVAILABLE_MESSAGE,
        "alert_direction_message": DEFAULT_ALERT_DIRECTION_MESSAGE,
        "alert_metric_back_message": DEFAULT_ALERT_METRIC_BACK_MESSAGE,
        "alert_target_message": DEFAULT_ALERT_TARGET_MESSAGE,
        "alert_new_target_message": DEFAULT_ALERT_NEW_TARGET_MESSAGE,
        "alert_delete_confirmation_message": DEFAULT_ALERT_DELETE_CONFIRMATION_MESSAGE,
        "alert_list_empty_message": DEFAULT_ALERT_LIST_EMPTY_MESSAGE,
        "alert_list_message": DEFAULT_ALERT_LIST_MESSAGE,
        "alert_detail_message": DEFAULT_ALERT_DETAIL_MESSAGE,
        "alert_issue_message": DEFAULT_ALERT_ISSUE_MESSAGE,
        "alert_note_message": DEFAULT_ALERT_NOTE_MESSAGE,
        "alert_cancelled_note": DEFAULT_ALERT_CANCELLED_NOTE,
        "alert_expired_note": DEFAULT_ALERT_EXPIRED_NOTE,
        "alert_saved_note": DEFAULT_ALERT_SAVED_NOTE,
        "alert_deleted_note": DEFAULT_ALERT_DELETED_NOTE,
        "utya_movement_up_message": DEFAULT_UTYA_MOVEMENT_UP_MESSAGE,
        "utya_movement_down_message": DEFAULT_UTYA_MOVEMENT_DOWN_MESSAGE,
    }
    PUBLIC_MESSAGE_LABELS = {
        "welcome_message": "welcome message",
        "subscription_message": "subscription-required message",
        "help_message": "help message",
        "private_help_message": "private help message",
        "guide_private_only_message": "private-only guide notice",
        "guide_home_message": "guide home",
        "guide_start_message": "getting started guide",
        "guide_research_message": "token research guide",
        "guide_research_search_message": "token search help",
        "guide_safety_message": "safety guide",
        "guide_trading_message": "buying and selling guide",
        "guide_converter_message": "converter guide",
        "guide_inline_message": "inline mode guide",
        "guide_commands_message": "bot commands guide",
        "guide_new_message": "new TON tokens guide",
        "guide_terms_message": "common terms guide",
        "guide_live_channels_message": "live price channels guide",
        "rate_limit_message": "rate-limit message",
        "trending_unavailable_message": "trending unavailable message",
        "pulse_title": "pulse title",
        "pulse_message": "pulse result layout",
        "pulse_event": "pulse event layout",
        "pulse_normal_message": "normal-market pulse message",
        "pulse_unavailable_message": "pulse unavailable message",
        "market_overview_message": "market overview layout",
        "market_overview_price_row": "market overview price row",
        "market_overview_cap_row": "market overview market-cap row",
        "new_message": "newly verified result message",
        "new_row": "newly verified result row",
        "new_usage_message": "/new command instructions",
        "new_empty_message": "newly verified empty result message",
        "new_unavailable_message": "newly verified unavailable message",
        "token_report_unavailable_message": "token report unavailable message",
        "token_choices_message": "token choices message",
        "token_not_found_message": "token not-found message",
        "ath_usage_message": "ATH instructions",
        "ath_result_message": "ATH result message",
        "ath_unavailable_message": "ATH unavailable message",
        "ath_lookup_unavailable_message": "ATH lookup error message",
        "conversion_usage_message": "converter instructions",
        "conversion_unavailable_message": "conversion unavailable message",
        "conversion_not_found_message": "conversion not-found message",
        "inline_coin_message": "inline coin-statistics message",
        "inline_conversion_message": "inline conversion message",
        "inline_help_message": "inline help and error message",
        "alert_home_message": "alerts home message",
        "alert_search_message": "alert token-search prompt",
        "alert_confirmation_message": "alert confirmation message",
        "alert_trigger_message": "triggered-alert notification",
        "alert_help_message": "alert help message",
        "private_alerts_message": "private-alerts notice",
        "alert_token_search_unavailable_message": "alert token-search unavailable message",
        "alert_token_choices_message": "alert token choices message",
        "alert_token_not_found_message": "alert token not-found message",
        "alert_invalid_target_message": "invalid alert target message",
        "alert_continue_message": "alert continue message",
        "alert_metric_message": "alert metric-selection message",
        "alert_selection_unavailable_message": "alert selection unavailable message",
        "alert_live_data_unavailable_message": "alert live-data unavailable message",
        "alert_metric_unavailable_message": "alert metric unavailable message",
        "alert_direction_message": "alert direction message",
        "alert_metric_back_message": "alert metric-back message",
        "alert_target_message": "alert target prompt",
        "alert_new_target_message": "alert new-target prompt",
        "alert_delete_confirmation_message": "alert deletion confirmation",
        "alert_list_empty_message": "empty alert-list message",
        "alert_list_message": "saved alert-list message",
        "alert_detail_message": "alert details message",
        "alert_issue_message": "alert issue line",
        "alert_note_message": "alert status-note format",
        "alert_cancelled_note": "alert cancelled note",
        "alert_expired_note": "alert expired note",
        "alert_saved_note": "alert saved note",
        "alert_deleted_note": "alert deleted note",
        "utya_movement_up_message": "UTYA upward-movement alert",
        "utya_movement_down_message": "UTYA downward-movement alert",
    }
    PUBLIC_MESSAGE_REQUIRED_PLACEHOLDERS = {
        "rate_limit_message": ("[WAIT_SECONDS]",),
        "pulse_message": ("[TITLE]", "[EVENTS]", "[UPDATED_AT]"),
        "pulse_event": ("[TICKER]", "[DETAILS]", "[AGE]"),
        "pulse_normal_message": ("[TITLE]", "[UPDATED_AT]"),
        "market_overview_message": ("[PRICES]", "[MARKET_CAPS]"),
        "market_overview_price_row": ("[TICKER]", "[CHANNEL_URL]", "[PRICE]"),
        "market_overview_cap_row": (
            "[TICKER]",
            "[CHANNEL_URL]",
            "[MARKET_CAP]",
            "[CHANGE_24H]",
        ),
        "new_message": ("[LIST]", "[UPDATED_AT]"),
        "new_row": (
            "[RANK]",
            "[TICKER]",
            "[NAME]",
            "[VALUATION_LABEL]",
            "[MARKET_CAP]",
            "[HOLDERS]",
        ),
        "token_choices_message": ("[QUERY]",),
        "token_not_found_message": ("[QUERY]",),
        "ath_result_message": ("[TOKEN_SYMBOL]", "[ATH_PRICE]"),
        "ath_unavailable_message": ("[TOKEN_SYMBOL]",),
        "conversion_usage_message": ("[ERROR]",),
        "conversion_unavailable_message": ("[ERROR]",),
        "conversion_not_found_message": ("[QUERY]",),
        "inline_coin_message": (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[PRICE]",
            "[CHANGE_24H]",
            "[ATH_PRICE]",
            "[HOLDERS]",
            "[MARKET_CAP]",
        ),
        "inline_conversion_message": (
            "[SOURCE_AMOUNT]",
            "[SOURCE_SYMBOL]",
            "[TARGET_AMOUNT]",
            "[TARGET_SYMBOL]",
        ),
        "inline_help_message": ("[NOTE]",),
        "alert_home_message": ("[ACTIVE_COUNT]", "[MAX_ALERTS]", "[SAVED_COUNT]"),
        "alert_confirmation_message": (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[METRIC]",
            "[CONDITION]",
            "[TARGET]",
            "[CURRENT]",
        ),
        "alert_trigger_message": (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[METRIC]",
            "[CONDITION]",
            "[TARGET]",
            "[CURRENT]",
        ),
        "alert_token_not_found_message": ("[QUERY]",),
        "alert_invalid_target_message": ("[ERROR]",),
        "alert_metric_message": (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[PRICE]",
            "[MARKET_CAP]",
            "[CHANGE_24H]",
        ),
        "alert_metric_unavailable_message": ("[METRIC]",),
        "alert_direction_message": ("[TOKEN_SYMBOL]", "[METRIC]", "[CURRENT]"),
        "alert_metric_back_message": ("[TOKEN_NAME]", "[TOKEN_SYMBOL]"),
        "alert_target_message": (
            "[TOKEN_SYMBOL]",
            "[METRIC]",
            "[CONDITION]",
            "[CURRENT]",
            "[EXAMPLE]",
        ),
        "alert_delete_confirmation_message": ("[TOKEN_SYMBOL]", "[METRIC]", "[TARGET]"),
        "alert_list_message": ("[PAGE]", "[TOTAL_PAGES]", "[TOTAL]"),
        "alert_detail_message": (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[STATUS]",
            "[METRIC]",
            "[CONDITION]",
            "[TARGET]",
            "[LATEST]",
            "[LAST_CHECKED]",
        ),
        "alert_issue_message": ("[ERROR]",),
        "alert_note_message": ("[NOTE]",),
        "utya_movement_up_message": ("[CHANGE_PERCENT]", "[OLD_PRICE]", "[NEW_PRICE]"),
        "utya_movement_down_message": ("[CHANGE_PERCENT]", "[OLD_PRICE]", "[NEW_PRICE]"),
    }
    PUBLIC_MESSAGE_OPTIONAL_PLACEHOLDERS = {
        "inline_conversion_message": ("[UNIT_RATE]",),
        "pulse_event": (
            "[EMOJI]",
            "[COIN_EMOJI]",
            "[SIGNAL_EMOJI]",
            "[SCORE]",
            "[SIGNALS]",
        ),
        "new_row": (
            "[AGE]",
            "[DEX]",
            "[VOLUME_24H]",
            "[LIQUIDITY]",
            "[CONTRACT]",
            "[CHART_URL]",
        ),
    }
    PUBLIC_MESSAGE_GROUPS = {
        "alert_entry": (
            "private_alerts_message",
            "alert_home_message",
            "alert_search_message",
            "alert_help_message",
        ),
        "alert_tokens": (
            "alert_token_search_unavailable_message",
            "alert_token_choices_message",
            "alert_token_not_found_message",
            "alert_metric_message",
            "alert_selection_unavailable_message",
        ),
        "alert_targets": (
            "alert_live_data_unavailable_message",
            "alert_metric_unavailable_message",
            "alert_direction_message",
            "alert_metric_back_message",
            "alert_target_message",
            "alert_new_target_message",
            "alert_invalid_target_message",
            "alert_continue_message",
            "alert_confirmation_message",
        ),
        "alert_saved": (
            "alert_list_empty_message",
            "alert_list_message",
            "alert_detail_message",
            "alert_delete_confirmation_message",
            "alert_trigger_message",
            "alert_issue_message",
        ),
        "alert_notes": (
            "alert_note_message",
            "alert_cancelled_note",
            "alert_expired_note",
            "alert_saved_note",
            "alert_deleted_note",
        ),
    }
    PUBLIC_MESSAGE_GROUP_LABELS = {
        "alert_entry": "🏠 Entry and help",
        "alert_tokens": "🔎 Token selection",
        "alert_targets": "🎯 Conditions and targets",
        "alert_saved": "📋 Saved alerts",
        "alert_notes": "ℹ️ Status notes",
    }
    PUBLIC_MESSAGE_GROUP_DESCRIPTIONS = {
        "alert_entry": "Edit how users enter alerts, see the alert home page, search for tokens, and open help.",
        "alert_tokens": "Edit token-search results, failures, and the first metric-selection screen.",
        "alert_targets": "Edit live-data errors, metric and direction screens, target prompts, and confirmation.",
        "alert_saved": "Edit saved-alert lists, alert details, deletion confirmation, issues, and trigger notifications.",
        "alert_notes": "Edit the reusable note format and the saved, cancelled, expired, and deleted status text.",
    }
    GUIDE_MESSAGE_KEYS = (
        "guide_private_only_message",
        "guide_home_message",
        "guide_start_message",
        "guide_research_message",
        "guide_research_search_message",
        "guide_safety_message",
        "guide_trading_message",
        "guide_converter_message",
        "guide_inline_message",
        "guide_commands_message",
        "guide_new_message",
        "guide_terms_message",
        "guide_live_channels_message",
    )
    INLINE_MESSAGE_KEYS = (
        "inline_coin_message",
        "inline_conversion_message",
        "inline_help_message",
    )
    MESSAGE_EDIT_CALLBACK_KEYS = {
        "edit_welcome_message": "welcome_message",
        "edit_subscription_message": "subscription_message",
        "edit_help_message": "help_message",
        "edit_private_help_message": "private_help_message",
        "edit_rate_limit_message": "rate_limit_message",
        "edit_trending_unavailable_message": "trending_unavailable_message",
        "edit_pulse_title": "pulse_title",
        "edit_pulse_message": "pulse_message",
        "edit_pulse_event": "pulse_event",
        "edit_pulse_normal_message": "pulse_normal_message",
        "edit_pulse_unavailable_message": "pulse_unavailable_message",
        "edit_market_overview_message": "market_overview_message",
        "edit_market_overview_price_row": "market_overview_price_row",
        "edit_market_overview_cap_row": "market_overview_cap_row",
        "edit_new_message": "new_message",
        "edit_new_row": "new_row",
        "edit_new_usage_message": "new_usage_message",
        "edit_new_empty_message": "new_empty_message",
        "edit_new_unavailable_message": "new_unavailable_message",
        "edit_token_report_unavailable_message": "token_report_unavailable_message",
        "edit_token_choices_message": "token_choices_message",
        "edit_token_not_found_message": "token_not_found_message",
        "edit_ath_usage_message": "ath_usage_message",
        "edit_ath_result_message": "ath_result_message",
        "edit_ath_unavailable_message": "ath_unavailable_message",
        "edit_ath_lookup_unavailable_message": "ath_lookup_unavailable_message",
        "edit_conversion_usage_message": "conversion_usage_message",
        "edit_conversion_unavailable_message": "conversion_unavailable_message",
        "edit_conversion_not_found_message": "conversion_not_found_message",
        "edit_trending_title": "trending_title",
        "edit_trending_message": "trending_message",
        "edit_trending_row": "trending_row",
        "edit_utya_movement_up_message": "utya_movement_up_message",
        "edit_utya_movement_down_message": "utya_movement_down_message",
    }
    TEXT_EDIT_KEYS = frozenset(
        set(PUBLIC_MESSAGE_DEFAULTS)
        | {
            "trending_title",
            "trending_message",
            "trending_row",
        }
    )
    VALUE_EDIT_KEYS = frozenset(
        {
            "channel",
            "required_channel",
            "trending_result_limit",
            "trending_min_liquidity_usd",
            "background_refresh_seconds",
            "image_cache_seconds",
            "utya_movement_threshold_percent",
            "market_overview_channel",
            "market_overview_interval_minutes",
        }
    )

    def __init__(self) -> None:
        load_dotenv()
        self.settings_path = OUTPUT_PATH.parent / "bot_settings.json"
        self.settings = self.load_settings()
        self.migrate_new_command_messages()
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        default_channel = os.getenv("CHANNEL", "@memeprice").strip() or "@memeprice"
        self.channel = self.setting_text("channel", default_channel)
        default_required_channel = os.getenv("REQUIRED_CHANNEL", self.channel).strip() or self.channel
        self.required_channel = self.setting_text("required_channel", default_required_channel)
        self.require_private_subscription = self.setting_bool("require_private_subscription", True)
        (
            self.convert_disabled_chat_ids,
            self.convert_disabled_chat_usernames,
        ) = self._parse_chat_restrictions(
            os.getenv("CONVERT_DISABLED_CHATS", "-1002842649515,@ogchat")
        )
        self.admin_ids = self._parse_admin_ids(os.getenv("ADMIN_IDS", "386839171"))
        self.owner_id = int(os.getenv("OWNER_ID", "386839171"))
        self.market_overview_enabled = self.setting_bool("market_overview_enabled", True)
        self.market_overview_channel = self.setting_text("market_overview_channel", self.channel)
        self.market_overview_interval_minutes = self.setting_int(
            "market_overview_interval_minutes",
            60,
            minimum=5,
            maximum=7 * 24 * 60,
        )
        self.market_overview_store = MarketOverviewStateStore(
            OUTPUT_PATH.parent / "market_overview_state.json"
        )
        if float(self.market_overview_store.state.get("next_due_at") or 0) <= 0:
            self.market_overview_store.schedule_after(self.market_overview_interval_minutes)
        self.image_cache_seconds = self.setting_int(
            "image_cache_seconds",
            int(os.getenv("IMAGE_CACHE_SECONDS", "30")),
            minimum=5,
            maximum=3_600,
        )
        self.background_refresh_seconds = self.setting_int(
            "background_refresh_seconds",
            int(os.getenv("BACKGROUND_REFRESH_SECONDS", "60")),
            minimum=30,
            maximum=3_600,
        )
        self.chat_cooldown_seconds = max(3, int(os.getenv("MEME_CHAT_COOLDOWN_SECONDS", "15")))
        self.global_limit_per_minute = max(1, int(os.getenv("MEME_GLOBAL_LIMIT_PER_MINUTE", "20")))
        self.meme_send_concurrency = max(1, int(os.getenv("MEME_SEND_CONCURRENCY", "12")))
        self.offset = 0
        self.renderer = DashboardRenderer()
        self.holder_service = HolderService(
            timeout_seconds=max(3, int(os.getenv("HOLDER_TIMEOUT_SECONDS", "8"))),
            cache_seconds=max(300, int(os.getenv("HOLDER_CACHE_SECONDS", "1800"))),
            concurrency=max(1, int(os.getenv("HOLDER_FETCH_CONCURRENCY", "3"))),
        )
        self.token_card_renderer = TokenCardRenderer()
        self.conversion_renderer = ConversionRenderer()
        self.price_service = PriceService()
        self.pulse_service = PulseService(
            OUTPUT_PATH.parent / "pulse_history.json",
            cache_seconds=max(30, int(os.getenv("PULSE_CACHE_SECONDS", "120"))),
            retention_seconds=max(25 * 60 * 60, int(os.getenv("PULSE_HISTORY_SECONDS", "172800"))),
            minimum_record_interval=max(30, int(os.getenv("PULSE_RECORD_INTERVAL_SECONDS", "45"))),
            persistence_interval=max(60, int(os.getenv("PULSE_PERSIST_INTERVAL_SECONDS", "300"))),
            max_events=max(1, min(8, int(os.getenv("PULSE_MAX_EVENTS", "5")))),
            max_snapshot_age=max(60, int(os.getenv("PULSE_MAX_SNAPSHOT_AGE_SECONDS", "180"))),
            feed_window_seconds=max(15 * 60, int(os.getenv("PULSE_FEED_WINDOW_SECONDS", "3600"))),
            coverage_gap_seconds=max(90, int(os.getenv("PULSE_COVERAGE_GAP_SECONDS", "180"))),
        )
        self.pulse_refresh_seconds = max(
            45,
            int(os.getenv("PULSE_REFRESH_SECONDS", "60")),
        )
        self.pulse_market_service = PulseMarketService(
            self.get_http_session,
            cache_seconds=max(20, int(os.getenv("PULSE_MARKET_CACHE_SECONDS", "60"))),
            stale_seconds=max(60, int(os.getenv("PULSE_MARKET_STALE_SECONDS", "180"))),
            trade_concurrency=max(1, int(os.getenv("PULSE_TRADE_CONCURRENCY", "3"))),
        )
        self.token_report_service = TokenReportService(
            self.get_http_session,
            cache_seconds=max(10, int(os.getenv("TOKEN_REPORT_CACHE_SECONDS", "45"))),
            search_cache_seconds=max(60, int(os.getenv("TOKEN_SEARCH_CACHE_SECONDS", "300"))),
            history_cache_seconds=max(300, int(os.getenv("TOKEN_HISTORY_CACHE_SECONDS", "1800"))),
            max_api_concurrency=max(1, int(os.getenv("TOKEN_REPORT_API_CONCURRENCY", "8"))),
        )
        self.chart_service = DashboardChartService(
            self.token_report_service,
            cache_seconds=max(300, int(os.getenv("DASHBOARD_CHART_CACHE_SECONDS", "1800"))),
            concurrency=max(1, int(os.getenv("DASHBOARD_CHART_CONCURRENCY", "3"))),
        )
        self.trending_service = TrendingService(
            self.get_http_session,
            OUTPUT_PATH.parent / "trending_cache.json",
            refresh_seconds=max(3_600, int(os.getenv("TRENDING_REFRESH_SECONDS", "3600"))),
            minimum_liquidity_usd=self.setting_float(
                "trending_min_liquidity_usd",
                float(os.getenv("TRENDING_MIN_LIQUIDITY_USD", "10000")),
                minimum=0,
                maximum=1_000_000_000,
            ),
            result_limit=self.setting_int(
                "trending_result_limit",
                int(os.getenv("TRENDING_RESULT_LIMIT", "10")),
                minimum=3,
                maximum=20,
            ),
            blocked_symbols=self._parse_csv_values(
                os.getenv("TRENDING_BLOCKED_SYMBOLS", ",".join(sorted(DEFAULT_BLOCKED_SYMBOLS)))
            ),
            blocked_addresses=self._parse_csv_values(
                os.getenv("TRENDING_BLOCKED_ADDRESSES", ",".join(sorted(DEFAULT_BLOCKED_ADDRESSES)))
            ),
        )
        self.new_tokens_service = NewTokensService(
            self.get_http_session,
            OUTPUT_PATH.parent / "newly_verified_tokens_cache.json",
            refresh_seconds=max(3_600, int(os.getenv("NEW_TOKENS_REFRESH_SECONDS", "21600"))),
            market_refresh_seconds=max(
                60,
                int(os.getenv("NEW_TOKENS_MARKET_REFRESH_SECONDS", "300")),
            ),
            minimum_liquidity_usd=max(
                0.0,
                float(os.getenv("NEW_TOKENS_MIN_LIQUIDITY_USD", "0")),
            ),
            result_limit=max(1, min(12, int(os.getenv("NEW_TOKENS_RESULT_LIMIT", "5")))),
            blocked_symbols=self._parse_csv_values(os.getenv("NEW_TOKENS_BLOCKED_SYMBOLS", "")),
            blocked_addresses=self._parse_csv_values(os.getenv("NEW_TOKENS_BLOCKED_ADDRESSES", "")),
        )
        self.render_lock = asyncio.Lock()
        self.pulse_lock = asyncio.Lock()
        self.market_overview_lock = asyncio.Lock()
        self.cached_image_bytes: bytes | None = None
        self.cached_image_at = 0.0
        self.cached_image_hash: str | None = None
        self.cached_photo_file_id: str | None = None
        self.cached_photo_hash: str | None = None
        self.photo_cache_path = OUTPUT_PATH.parent / "telegram_photo_cache.json"
        self.subscribers_path = OUTPUT_PATH.parent / "subscribers.json"
        self.subscriber_ids = self.load_subscribers()
        self.usage_stats_path = OUTPUT_PATH.parent / "usage_stats.json"
        self.usage_stats = self.load_usage_stats()
        if self.bootstrap_usage_users():
            self.save_usage_stats()
        self.usage_stats_dirty = False
        self.usage_stats_save_task: asyncio.Task[None] | None = None
        for key, default in self.PUBLIC_MESSAGE_DEFAULTS.items():
            setattr(self, key, self.setting_text(key, default))
        self.public_button_texts = {
            key: self.setting_text(f"public_button_text:{key}", default)
            for key, default in self.PUBLIC_BUTTON_DEFAULTS.items()
        }
        self.public_button_icons = {
            key: self.setting_text(f"public_button_icon:{key}", "")
            for key in self.PUBLIC_BUTTON_DEFAULTS
        }
        self.pulse_emojis = {
            key: self.normalize_persisted_pulse_emoji(
                self.setting_text(f"{self.PULSE_EMOJI_SETTING_PREFIX}{key}", default),
                default,
            )
            for key, default in self.PULSE_EMOJI_DEFAULTS.items()
        }
        self.pulse_coin_emojis = {
            ticker: self.normalize_persisted_pulse_emoji(
                self.setting_text(
                    f"{self.PULSE_COIN_EMOJI_SETTING_PREFIX}{ticker}",
                    default,
                ),
                default,
            )
            for ticker, default in self.PULSE_COIN_EMOJI_DEFAULTS.items()
        }
        self.trending_title = self.setting_text("trending_title", self.DEFAULT_TRENDING_TITLE)
        self.trending_message = self.setting_text("trending_message", self.DEFAULT_TRENDING_MESSAGE)
        self.trending_row = self.setting_text("trending_row", self.DEFAULT_TRENDING_ROW)
        self.utya_movement_enabled = self.setting_bool("utya_movement_enabled", True)
        self.utya_movement_threshold_percent = self.setting_float(
            "utya_movement_threshold_percent",
            10.0,
            minimum=1.0,
            maximum=100.0,
        )
        self.utya_movement_tracker = MovementTracker(
            OUTPUT_PATH.parent / "utya_movement_alert_state.json"
        )
        self.utya_movement_lock = asyncio.Lock()
        self.pending_text_edits: dict[int, str] = {}
        self.token_image_cache_seconds = max(10, int(os.getenv("TOKEN_IMAGE_CACHE_SECONDS", "60")))
        self.token_alias_prewarm_seconds = max(
            30,
            int(os.getenv("TOKEN_ALIAS_PREWARM_SECONDS", "45")),
        )
        self.token_image_cache: dict[str, tuple[float, bytes, str]] = {}
        self.token_photo_file_ids: dict[str, tuple[str, str]] = {}
        self.token_image_inflight: dict[str, asyncio.Task[tuple[bytes, str]]] = {}
        self.conversion_image_cache_seconds = max(10, int(os.getenv("CONVERSION_IMAGE_CACHE_SECONDS", "60")))
        self.conversion_cache_entries = max(16, int(os.getenv("CONVERSION_CACHE_ENTRIES", "256")))
        self.conversion_image_cache: dict[str, tuple[float, bytes, str]] = {}
        self.conversion_photo_file_ids: dict[str, tuple[str, str]] = {}
        self.pending_conversion_choices: dict[int, tuple[Decimal, float]] = {}
        self.pending_broadcasts: dict[int, str] = {}
        self.alert_store = AlertStore(
            OUTPUT_PATH.parent / "user_alerts.json",
            max_active_per_user=max(1, int(os.getenv("MAX_ACTIVE_ALERTS_PER_USER", "20"))),
        )
        self.pending_alert_inputs: dict[int, AlertDraft] = {}
        self.pending_alert_choices: dict[int, dict[str, str]] = {}
        self.alert_store_lock = asyncio.Lock()
        self.alert_check_seconds = max(15, int(os.getenv("ALERT_CHECK_SECONDS", "30")))
        self.alert_api_semaphore = asyncio.Semaphore(
            max(1, int(os.getenv("ALERT_API_CONCURRENCY", "4")))
        )
        self.gram_value_cache: tuple[float, float | None, float | None] | None = None
        self.inline_snapshot_cache_seconds = max(
            5,
            int(os.getenv("INLINE_SNAPSHOT_CACHE_SECONDS", "20")),
        )
        self.inline_snapshot_cache: tuple[float, tuple[InlineCoin, ...]] | None = None
        self.inline_snapshot_lock = asyncio.Lock()
        self.inline_query_semaphore = asyncio.Semaphore(
            max(1, int(os.getenv("INLINE_QUERY_CONCURRENCY", "8")))
        )
        self.chat_meme_times: dict[int, float] = {}
        self.chat_notice_times: dict[int, float] = {}
        self.membership_cache: dict[int, tuple[bool, float]] = {}
        self.membership_cache_seconds = max(60, int(os.getenv("MEMBERSHIP_CACHE_SECONDS", "600")))
        self.global_meme_times: deque[float] = deque()
        self.update_tasks: set[asyncio.Task] = set()
        self.update_semaphore = asyncio.Semaphore(20)
        self.meme_send_semaphore = asyncio.Semaphore(self.meme_send_concurrency)
        self.token_report_semaphore = asyncio.Semaphore(max(1, int(os.getenv("TOKEN_REPORT_CONCURRENCY", "8"))))
        self.conversion_semaphore = asyncio.Semaphore(max(1, int(os.getenv("CONVERSION_CONCURRENCY", "8"))))
        self.photo_upload_lock = asyncio.Lock()
        self.token_photo_upload_lock = asyncio.Lock()
        self.conversion_photo_upload_locks = [asyncio.Lock() for _ in range(32)]
        self.broadcast_lock = asyncio.Lock()
        self.http_session: aiohttp.ClientSession | None = None

    async def run_forever(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is missing. Create .env from .env.example.")
        try:
            await self.ensure_warm_image()
            tasks = [
                asyncio.create_task(self.startup_telegram_setup()),
                asyncio.create_task(self.poll_updates()),
                asyncio.create_task(self.background_refresh_loop()),
                asyncio.create_task(self.pulse_refresh_loop()),
                asyncio.create_task(self.token_alias_prewarm_loop()),
                asyncio.create_task(self.trending_refresh_loop()),
                asyncio.create_task(self.new_tokens_refresh_loop()),
                asyncio.create_task(self.alert_evaluation_loop()),
                asyncio.create_task(self.market_overview_loop()),
            ]
            await asyncio.gather(*tasks)
        finally:
            if self.usage_stats_dirty:
                self.save_usage_stats()
            await self.close_http_session()

    async def get_http_session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                limit=60,
                limit_per_host=60,
                ttl_dns_cache=600,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=20, sock_connect=5, sock_read=15)
            self.http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self.http_session

    async def close_http_session(self) -> None:
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()

    async def startup_telegram_setup(self) -> None:
        try:
            await self.validate_bot_token()
        except Exception as exc:
            print(f"Bot token validation skipped after {type(exc).__name__}: {exc}", flush=True)
        try:
            await self.setup_commands()
        except Exception as exc:
            print(f"Command setup skipped after {type(exc).__name__}: {exc}", flush=True)

    async def background_refresh_loop(self) -> None:
        await asyncio.sleep(self.background_refresh_seconds)
        while True:
            try:
                await self.refresh_image_cache(force=True)
            except Exception as exc:
                print(f"Background image refresh failed: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(self.background_refresh_seconds)

    async def pulse_refresh_loop(self) -> None:
        refresh_count = 0
        last_coverage: bool | None = None
        while True:
            started = time.monotonic()
            try:
                result = await self.refresh_pulse_cache(force=True)
                refresh_count += 1
                if (
                    refresh_count == 1
                    or refresh_count % 15 == 0
                    or result.coverage_complete != last_coverage
                ):
                    print(
                        f"Pulse feed refreshed events={len(result.events)} "
                        f"coverage={'complete' if result.coverage_complete else 'partial'}",
                        flush=True,
                    )
                last_coverage = result.coverage_complete
            except Exception as exc:
                print(f"Pulse feed refresh failed: {type(exc).__name__}: {exc}", flush=True)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(5.0, self.pulse_refresh_seconds - elapsed))

    async def process_utya_movement_alert(self, values: list[Any]) -> None:
        if not self.utya_movement_enabled:
            return

        utya_value = next(
            (
                value
                for value in values
                if str(getattr(value, "ticker", "") or "").upper() == "UTYA"
            ),
            None,
        )
        if utya_value is None or getattr(utya_value, "price", None) is None:
            return

        async with self.utya_movement_lock:
            observed_at = utc_now_iso()
            had_reference = self.utya_movement_tracker.reference_price is not None
            try:
                event = self.utya_movement_tracker.observe(
                    float(utya_value.price),
                    self.utya_movement_threshold_percent,
                    observed_at,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"UTYA movement observation skipped: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                return

            if not had_reference:
                print(
                    "UTYA movement reference initialized "
                    f"price={self.utya_movement_tracker.reference_price}",
                    flush=True,
                )
            if event is None:
                return

            text = self.render_utya_movement_message(event)
            try:
                result = await self.api(
                    "sendMessage",
                    {
                        "chat_id": self.UTYA_MOVEMENT_CHANNEL,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )
            except Exception as exc:
                print(
                    "UTYA movement alert delivery failed "
                    f"direction={event.direction} change={event.change_percent:+.4f}% "
                    f"error={type(exc).__name__}: {exc}",
                    flush=True,
                )
                return

            if not result.get("ok"):
                print(
                    "UTYA movement alert rejected by Telegram "
                    f"direction={event.direction} change={event.change_percent:+.4f}% "
                    f"error={result.get('error_code')}:{result.get('description')}",
                    flush=True,
                )
                return

            try:
                acknowledged = self.utya_movement_tracker.acknowledge(event)
            except OSError as exc:
                print(
                    "UTYA movement alert was delivered but state persistence failed "
                    f"error={type(exc).__name__}: {exc}",
                    flush=True,
                )
                return
            if acknowledged:
                print(
                    "UTYA movement alert delivered "
                    f"channel={self.UTYA_MOVEMENT_CHANNEL} direction={event.direction} "
                    f"change={event.change_percent:+.4f}%",
                    flush=True,
                )

    def render_utya_movement_message(self, event: MovementEvent) -> str:
        key = (
            "utya_movement_up_message"
            if event.direction == "up"
            else "utya_movement_down_message"
        )
        return self.render_public_message(
            key,
            {
                "[CHANGE_PERCENT]": f"{abs(event.change_percent):.2f}%",
                "[SIGNED_CHANGE_PERCENT]": f"{event.change_percent:+.2f}%",
                "[OLD_PRICE]": format_price(event.reference_price),
                "[NEW_PRICE]": format_price(event.current_price),
                "[DIRECTION]": event.direction.upper(),
                "[THRESHOLD]": f"{self.utya_movement_threshold_percent:g}%",
                "[TIME_UTC]": self.format_stats_timestamp(event.observed_at),
                "[CHANNEL]": self.UTYA_MOVEMENT_CHANNEL,
            },
        )

    async def token_alias_prewarm_loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            await self.prewarm_token_alias_images()
            await asyncio.sleep(self.token_alias_prewarm_seconds)

    async def prewarm_token_alias_images(self) -> None:
        for query in self.TOKEN_REPORT_PREWARM_QUERIES:
            try:
                pair, _ = await self.resolve_token_pair_for_query(query)
                if pair is not None:
                    await self.refresh_token_report_image(pair)
            except Exception as exc:
                print(
                    f"Token alias prewarm failed query={query!r}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

    async def trending_refresh_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await self.trending_service.refresh_if_due()
            except Exception as exc:
                print(f"Trending snapshot refresh failed: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(3_600)

    async def new_tokens_refresh_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.new_tokens_service.refresh_if_due()
            except Exception as exc:
                print(f"New-token snapshot refresh failed: {type(exc).__name__}: {exc}", flush=True)
            snapshot = self.new_tokens_service.current()
            if snapshot is not None and snapshot.tokens:
                try:
                    await self.new_tokens_service.enrich_market(snapshot.tokens)
                except Exception as exc:
                    print(f"New-token market refresh failed: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(
                min(
                    self.new_tokens_service.refresh_seconds,
                    self.new_tokens_service.market_refresh_seconds,
                )
            )

    async def alert_evaluation_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self.evaluate_alerts_once()
            except Exception as exc:
                print(f"Alert evaluation failed: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(self.alert_check_seconds)

    async def evaluate_alerts_once(self) -> None:
        async with self.alert_store_lock:
            active_ids = [alert.alert_id for alert in self.alert_store.active()]
            token_addresses = sorted(
                {
                    self.alert_store.alerts[alert_id].token_address
                    for alert_id in active_ids
                    if alert_id in self.alert_store.alerts
                }
            )

        async def load_pair(token_address: str) -> tuple[str, TokenPair | None, Exception | None]:
            try:
                async with self.alert_api_semaphore:
                    pair = await self.token_report_service.best_pair_for_token(token_address)
                return token_address, pair, None
            except Exception as exc:
                return token_address, None, exc

        pair_results = await asyncio.gather(*(load_pair(address) for address in token_addresses))
        pairs: dict[str, TokenPair | None] = {}
        errors: dict[str, Exception] = {}
        for token_address, pair, error in pair_results:
            pairs[token_address] = pair
            if error is not None:
                errors[token_address] = error

        changed = False
        checked_at = utc_now_iso()
        async with self.alert_store_lock:
            for alert_id in active_ids:
                alert = self.alert_store.alerts.get(alert_id)
                if alert is None or not alert.active:
                    continue
                alert.last_checked_at = checked_at
                error = errors.get(alert.token_address)
                if error is not None:
                    alert.last_error = f"{type(error).__name__}: {error}"[:300]
                    changed = True
                    continue
                pair = pairs.get(alert.token_address)
                if pair is None:
                    alert.last_error = "Live market data is temporarily unavailable."
                    changed = True
                    continue
                current_value = metric_value(pair, alert.metric)
                alert.token_name = pair.name
                alert.token_symbol = pair.symbol.upper()
                alert.pair_address = pair.pair_address
                alert.pair_url = pair.url
                alert.last_value = current_value
                if current_value is None:
                    alert.last_error = f"{metric_label(alert.metric)} data is unavailable."
                    changed = True
                    continue
                alert.last_error = ""
                if threshold_reached(alert.direction, current_value, alert.target_value):
                    alert.active = False
                    alert.triggered_at = checked_at
                    alert.triggered_value = current_value
                    alert.notification_status = "pending"
                    alert.notification_attempts = 0
                    alert.last_notification_attempt_at = ""
                    alert.notification_retry_after_seconds = 0
                    print(
                        "Alert triggered "
                        f"id={alert.alert_id} user={alert.user_id} token={alert.token_symbol} "
                        f"metric={alert.metric} value={current_value} target={alert.target_value}",
                        flush=True,
                    )
                changed = True
            if changed:
                self.alert_store.save()

        await self.deliver_pending_alert_notifications()

    async def deliver_pending_alert_notifications(self) -> None:
        async with self.alert_store_lock:
            pending_ids = [alert.alert_id for alert in self.alert_store.pending_notifications()]

        for alert_id in pending_ids:
            async with self.alert_store_lock:
                alert = self.alert_store.alerts.get(alert_id)
                if alert is None or alert.notification_status != "pending":
                    continue
                if self._alert_notification_retry_wait(alert) > 0:
                    continue
                alert.notification_attempts += 1
                alert.last_notification_attempt_at = utc_now_iso()
                self.alert_store.save()
                notification = UserAlert.from_dict(vars(alert))

            result = await self.send_alert_trigger_notification(notification)
            async with self.alert_store_lock:
                current = self.alert_store.alerts.get(alert_id)
                if current is None or current.notification_status != "pending":
                    continue
                if result.get("ok"):
                    current.notification_status = "sent"
                    current.last_error = ""
                    current.notification_retry_after_seconds = 0
                else:
                    description = str(result.get("description") or "Telegram delivery failed.")
                    error_code = int(result.get("error_code") or 0)
                    current.last_error = description[:300]
                    if error_code == 403:
                        current.notification_status = "failed"
                    elif error_code == 429:
                        parameters = result.get("parameters") if isinstance(result.get("parameters"), dict) else {}
                        current.notification_retry_after_seconds = max(
                            60,
                            int(parameters.get("retry_after") or 0),
                        )
                self.alert_store.save()

    @staticmethod
    def _alert_notification_retry_wait(alert: UserAlert) -> float:
        if not alert.last_notification_attempt_at:
            return 0
        try:
            attempted_at = datetime.fromisoformat(alert.last_notification_attempt_at.replace("Z", "+00:00"))
            attempted_at = attempted_at if attempted_at.tzinfo else attempted_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0
        retry_seconds = max(60, alert.notification_retry_after_seconds)
        return max(0.0, retry_seconds - (datetime.now(timezone.utc) - attempted_at).total_seconds())

    async def send_alert_trigger_notification(self, alert: UserAlert) -> dict[str, Any]:
        text = self.render_public_message(
            "alert_trigger_message",
            {
                "[TOKEN_NAME]": alert.token_name,
                "[TOKEN_SYMBOL]": alert.token_symbol,
                "[METRIC]": metric_label(alert.metric),
                "[CONDITION]": direction_label(alert.direction),
                "[TARGET]": format_metric_value(alert.metric, alert.target_value),
                "[CURRENT]": format_metric_value(alert.metric, alert.triggered_value),
            },
        )
        buttons: list[list[dict[str, str]]] = []
        if alert.pair_url.startswith(("https://", "http://")):
            buttons.append([self.public_button("alert_open_chart", url=alert.pair_url)])
        buttons.append([self.public_button("alert_manage", callback_data="alert:home")])
        try:
            return await self.api(
                "sendMessage",
                {
                    "chat_id": str(alert.user_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    "reply_markup": json.dumps({"inline_keyboard": buttons}),
                },
            )
        except Exception as exc:
            print(
                f"Alert notification failed id={alert.alert_id}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}

    async def poll_updates(self) -> None:
        try:
            await self.api("deleteWebhook", {"drop_pending_updates": "false"}, timeout=10)
        except Exception as exc:
            print(f"deleteWebhook skipped after {type(exc).__name__}: {exc}", flush=True)
        while True:
            try:
                result = await self.api(
                    "getUpdates",
                    {
                        "offset": str(self.offset),
                        "timeout": "10",
                        "allowed_updates": json.dumps(list(self.ALLOWED_UPDATES)),
                    },
                    timeout=15,
                )
                if not result.get("ok"):
                    await asyncio.sleep(3)
                    continue
                for update in result.get("result") or []:
                    self.offset = max(self.offset, int(update.get("update_id") or 0) + 1)
                    task = asyncio.create_task(self.handle_update_guarded(update))
                    self.update_tasks.add(task)
                    task.add_done_callback(self.update_tasks.discard)
            except Exception as exc:
                print(f"Polling error: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(3)

    async def handle_update_guarded(self, update: dict[str, Any]) -> None:
        async with self.update_semaphore:
            try:
                await self.handle_update(update)
            except Exception as exc:
                print(f"Update handling error: {type(exc).__name__}: {exc}", flush=True)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.record_message_usage(update["message"])
            await self.handle_message(update["message"])
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self.record_callback_usage(callback)
            await self.handle_callback(callback)
            return
        inline_query = update.get("inline_query")
        if isinstance(inline_query, dict):
            self.record_inline_usage(inline_query)
            await self.handle_inline_query(inline_query)
            return
        membership = update.get("my_chat_member")
        if isinstance(membership, dict):
            self.record_chat_membership(membership)

    async def handle_inline_query(self, inline_query: dict[str, Any]) -> None:
        query_id = str(inline_query.get("id") or "")
        if not query_id:
            return
        raw_query = str(inline_query.get("query") or "")
        templates = self.inline_message_templates()
        try:
            async with self.inline_query_semaphore:
                coins = self.inline_cached_coin_snapshot()
                if not any(coin.price_usd is not None for coin in coins):
                    coins = await asyncio.wait_for(self.inline_coin_snapshot(), timeout=8)
                results = build_inline_results(raw_query, coins, templates)
        except Exception as exc:
            print(f"Inline query data failed: {type(exc).__name__}: {exc}", flush=True)
            results = [
                help_result(
                    "Live prices are temporarily unavailable. Please try again.",
                    templates,
                )
            ]
        await self.answer_inline_query(query_id, results)

    def inline_message_templates(self) -> InlineMessageTemplates:
        return InlineMessageTemplates(
            coin=str(getattr(self, "inline_coin_message", DEFAULT_INLINE_COIN_MESSAGE)),
            conversion=str(
                getattr(self, "inline_conversion_message", DEFAULT_INLINE_CONVERSION_MESSAGE)
            ),
            help=str(getattr(self, "inline_help_message", DEFAULT_INLINE_HELP_MESSAGE)),
        )

    def inline_cached_coin_snapshot(self) -> tuple[InlineCoin, ...]:
        values = self.price_service.cached_prices()
        holder_counts = self.holder_service.cached_counts(
            [symbol for symbol in FEATURED_INLINE_SYMBOLS if symbol != "GRAM"]
        )
        snapshot = self.build_inline_coins(values, holder_counts)
        if any(coin.price_usd is not None for coin in snapshot):
            self.inline_snapshot_cache = (
                time.monotonic() + self.inline_snapshot_cache_seconds,
                snapshot,
            )
            return snapshot
        cached = self.inline_snapshot_cache
        return cached[1] if cached is not None else snapshot

    async def inline_coin_snapshot(self) -> tuple[InlineCoin, ...]:
        now = time.monotonic()
        cached = self.inline_snapshot_cache
        if cached is not None and cached[0] > now:
            return cached[1]

        async with self.inline_snapshot_lock:
            now = time.monotonic()
            cached = self.inline_snapshot_cache
            if cached is not None and cached[0] > now:
                return cached[1]

            values_result, holders_result = await asyncio.gather(
                self.price_service.fetch_prices(),
                self.holder_service.fetch_counts(
                    [symbol for symbol in FEATURED_INLINE_SYMBOLS if symbol != "GRAM"]
                ),
                return_exceptions=True,
            )
            if isinstance(values_result, BaseException):
                if cached is not None:
                    return cached[1]
                raise RuntimeError("Live price snapshot is unavailable") from values_result

            if isinstance(holders_result, BaseException):
                print(
                    f"Inline holder snapshot failed: "
                    f"{type(holders_result).__name__}: {holders_result}",
                    flush=True,
                )
            holder_counts = holders_result if isinstance(holders_result, dict) else {}
            snapshot = self.build_inline_coins(values_result, holder_counts)
            self.inline_snapshot_cache = (
                time.monotonic() + self.inline_snapshot_cache_seconds,
                snapshot,
            )
            return snapshot

    @staticmethod
    def build_inline_coins(
        values: list[Any],
        holder_counts: dict[str, Any],
    ) -> tuple[InlineCoin, ...]:
        by_symbol = {value.ticker.upper(): value for value in values}
        coins: list[InlineCoin] = []
        for symbol in FEATURED_INLINE_SYMBOLS:
            value = by_symbol.get(symbol)
            holder = holder_counts.get(symbol)
            coins.append(
                InlineCoin(
                    symbol=symbol,
                    name=INLINE_COIN_NAMES[symbol],
                    price_usd=value.price if value is not None else None,
                    change_24h=value.change_24h if value is not None else None,
                    market_cap=value.market_cap if value is not None else None,
                    holders=getattr(holder, "count", None),
                    logo_url=INLINE_LOGO_URLS[symbol],
                    ath_price=getattr(value, "ath_price", None),
                    native=symbol == "GRAM",
                )
            )
        return tuple(coins)

    async def answer_inline_query(
        self,
        query_id: str,
        results: list[dict[str, object]],
    ) -> None:
        response = await self.api(
            "answerInlineQuery",
            {
                "inline_query_id": query_id,
                "results": json.dumps(results, ensure_ascii=False),
                "cache_time": "15",
                "is_personal": "false",
                "button": json.dumps(
                    {
                        "text": INLINE_RESULTS_BUTTON_TEXT,
                        "start_parameter": INLINE_RESULTS_START_PARAMETER,
                    },
                    ensure_ascii=False,
                ),
            },
            timeout=10,
        )
        if not response.get("ok"):
            description = str(response.get("description") or "unknown Telegram error")
            raise RuntimeError(f"Telegram rejected inline results: {description}")

    async def handle_message(self, message: dict[str, Any]) -> None:
        user_id = int((message.get("from") or {}).get("id") or 0)
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        chat_type = str(chat.get("type") or "")
        text = str(message.get("text") or "").strip()
        command = self._command_name(text)
        command_args = self._command_args(text)

        pending_alert_inputs = getattr(self, "pending_alert_inputs", {})
        if chat_type == "private" and user_id in pending_alert_inputs:
            if command == "/cancel":
                pending_alert_inputs.pop(user_id, None)
                await self.send_alert_menu(
                    chat_id,
                    user_id,
                    note=self.render_public_message("alert_cancelled_note"),
                )
                return
            if command in {"/start", "/menu"}:
                pending_alert_inputs.pop(user_id, None)
            elif text and not command:
                await self.handle_alert_text_input(user_id, chat_id, text)
                return

        if chat_type == "private" and user_id in self.pending_text_edits and self.authorized(user_id):
            pending_key = self.pending_text_edits[user_id]
            if command == "/cancel":
                self.pending_text_edits.pop(user_id, None)
                await self.send_message(
                    chat_id,
                    "Editing cancelled.",
                    reply_markup=self.settings_markup_for_key(pending_key),
                )
                return
            if command in {"/start", "/menu"}:
                self.pending_text_edits.pop(user_id, None)
            if text and not command:
                if pending_key == "broadcast_message":
                    await self.save_pending_broadcast(
                        user_id,
                        chat_id,
                        text,
                        message.get("entities") or [],
                    )
                elif pending_key.startswith(
                    (self.PULSE_EMOJI_SETTING_PREFIX, self.PULSE_COIN_EMOJI_SETTING_PREFIX)
                ):
                    await self.save_pending_pulse_emoji_edit(
                        user_id,
                        chat_id,
                        text,
                        message.get("entities") or [],
                    )
                elif pending_key.startswith("public_button:"):
                    await self.save_pending_public_button_edit(
                        user_id,
                        chat_id,
                        text,
                        message.get("entities") or [],
                    )
                elif pending_key in self.TEXT_EDIT_KEYS:
                    await self.save_pending_text_edit(
                        user_id,
                        chat_id,
                        text,
                        message.get("entities") or [],
                    )
                else:
                    await self.save_pending_value_edit(user_id, chat_id, text)
                return

        if command == "/help":
            help_key = "private_help_message" if chat_type == "private" else "help_message"
            help_text = self.render_public_message(help_key)
            await self.send_message(chat_id, help_text)
            return

        if command == "/guide" or (
            command == "/start" and chat_type == "private" and command_args.casefold() == "guide"
        ):
            if chat_type != "private":
                await self.send_message(
                    chat_id,
                    self.render_public_message("guide_private_only_message"),
                    reply_markup=self.guide_private_only_markup(),
                )
                return
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            await self.send_guide_home(chat_id, user_id)
            return

        if command == "/meme":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            if command_args:
                await self.send_token_report(chat_id, command_args)
                return
            await self.send_meme(chat_id)
            return

        if command == "/ath":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            await self.send_ath(chat_id, command_args)
            return

        if command == "/swap":
            if self.conversion_disabled_in_chat(chat):
                return
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            await self.send_conversion(chat_id, user_id, command_args)
            return

        if command == "/trending":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            await self.send_trending(chat_id)
            return

        if command == "/pulse":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            await self.send_pulse(chat_id)
            return

        if command == "/new":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            if command_args:
                await self.send_message(
                    chat_id,
                    self.render_public_message("new_usage_message"),
                )
                return
            await self.send_new_tokens(chat_id)
            return

        if command == "/alert":
            if chat_type != "private":
                await self.send_message(
                    chat_id,
                    self.render_public_message("private_alerts_message"),
                )
                return
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            self.pending_text_edits.pop(user_id, None)
            pending_alert_inputs.pop(user_id, None)
            await self.send_alert_menu(chat_id, user_id)
            return

        if command == "/start" and chat_type == "private":
            if not await self.ensure_subscribed(user_id, chat_id, chat_type):
                return
            if not self.authorized(user_id):
                await self.send_message(chat_id, self.welcome_message)
                return

        if not self.authorized(user_id):
            return

        if chat_type != "private":
            if command in {"/menu", "/preview", "/post", "/status"}:
                await self.send_message(chat_id, "Admin controls are available only in private messages.")
            return

        if command in {"/start", "/menu"}:
            await self.send_menu(chat_id)
        elif command == "/preview":
            await self.send_preview(chat_id)
        elif command == "/post":
            await self.post_once(self.channel)
            await self.send_message(
                chat_id,
                f"Posted dashboard to {self.channel}.",
                reply_markup=self.menu_markup(user_id),
            )
        elif command == "/status":
            await self.send_message(chat_id, self.status_text(), reply_markup=self.menu_markup(user_id))

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        user_id = int((callback.get("from") or {}).get("id") or 0)
        callback_id = str(callback.get("id") or "")
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or user_id)
        message_id = int(message.get("message_id") or 0)
        data = str(callback.get("data") or "")

        if data.startswith("guide:"):
            await self.handle_guide_callback(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=str((message.get("chat") or {}).get("type") or ""),
                message_id=message_id,
                callback_id=callback_id,
                data=data,
            )
            return

        if data == "check_subscription":
            if await self.is_channel_member(user_id, force=True):
                self.remember_subscriber(user_id)
                await self.answer_callback(callback_id, "Subscription confirmed.")
                if self.authorized(user_id):
                    await self.edit_message(chat_id, message_id, self.status_text(), self.menu_markup(user_id))
                else:
                    await self.edit_message(
                        chat_id,
                        message_id,
                        self.welcome_message,
                        json.dumps({"inline_keyboard": []}),
                    )
            else:
                await self.answer_callback(callback_id, "Join @memeprice first, then check again.")
            return

        if data.startswith("coin:"):
            callback_chat_type = str((message.get("chat") or {}).get("type") or "")
            if (
                callback_chat_type == "private"
                and self.require_private_subscription
                and not await self.is_channel_member(user_id)
            ):
                await self.answer_callback(callback_id, "Join @memeprice first, then try again.")
                await self.send_subscription_required(chat_id, private=True)
                return
            await self.answer_callback(callback_id, "Loading token report...")
            await self.edit_token_report(chat_id, message_id, data.removeprefix("coin:"))
            return

        if data.startswith("ath:"):
            callback_chat_type = str((message.get("chat") or {}).get("type") or "")
            if (
                callback_chat_type == "private"
                and self.require_private_subscription
                and not await self.is_channel_member(user_id)
            ):
                await self.answer_callback(callback_id, "Join @memeprice first, then try again.")
                await self.send_subscription_required(chat_id, private=True)
                return
            await self.answer_callback(callback_id, "Loading ATH...")
            await self.edit_ath(chat_id, message_id, data.removeprefix("ath:"))
            return

        if data.startswith("convert:"):
            callback_chat_type = str((message.get("chat") or {}).get("type") or "")
            if (
                callback_chat_type == "private"
                and self.require_private_subscription
                and not await self.is_channel_member(user_id)
            ):
                await self.answer_callback(callback_id, "Join @memeprice first, then try again.")
                await self.send_subscription_required(chat_id, private=True)
                return
            pending = self.pending_conversion_choices.get(user_id)
            if not pending or pending[1] <= time.monotonic():
                self.pending_conversion_choices.pop(user_id, None)
                await self.answer_callback(callback_id, "This conversion expired. Send /swap again.")
                return
            await self.answer_callback(callback_id, "Converting...")
            token_address = data.removeprefix("convert:")
            try:
                pair = await self.token_report_service.best_pair_for_token(token_address)
                if pair is None:
                    raise ValueError("This token no longer has a valid live market price.")
                await self.send_conversion_photo(chat_id, pair, pending[0])
                self.pending_conversion_choices.pop(user_id, None)
            except Exception as exc:
                print(f"Conversion callback failed address={token_address!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(
                    chat_id,
                    self.render_public_message(
                        "conversion_unavailable_message",
                        {"[ERROR]": "Try again in a few seconds."},
                    ),
                )
            return

        if data.startswith("alert:"):
            callback_chat_type = str((message.get("chat") or {}).get("type") or "")
            if callback_chat_type != "private":
                await self.answer_callback(callback_id, "Alerts work only in private chat.")
                return
            if (
                self.require_private_subscription
                and not self.authorized(user_id)
                and not await self.is_channel_member(user_id)
            ):
                await self.answer_callback(callback_id, "Join @memeprice first, then try again.")
                await self.send_subscription_required(chat_id, private=True)
                return
            await self.handle_alert_callback(
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                callback_id=callback_id,
                data=data,
            )
            return

        if not self.authorized(user_id):
            await self.answer_callback(callback_id, "Not authorized.")
            return

        if data.startswith("overview:approve:") or data.startswith("overview:skip:"):
            action, proposal_id = data.split(":", 2)[1:]
            await self.handle_market_overview_action(
                user_id=user_id,
                callback_id=callback_id,
                action=action,
                proposal_id=proposal_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            return

        await self.answer_callback(callback_id)
        if data == "menu":
            self.pending_text_edits.pop(user_id, None)
            await self.edit_message(chat_id, message_id, self.status_text(), self.menu_markup(user_id))
        elif data == "dashboard_settings":
            await self.edit_message(chat_id, message_id, self.dashboard_settings_text(), self.dashboard_settings_markup())
        elif data == "publishing_settings":
            await self.edit_message(chat_id, message_id, self.publishing_settings_text(), self.publishing_settings_markup())
        elif data == "trending_settings":
            await self.edit_message(chat_id, message_id, self.trending_settings_text(), self.trending_settings_markup())
        elif data == "market_overview_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.market_overview_settings_text(),
                self.market_overview_settings_markup(),
            )
        elif data == "toggle_market_overview":
            self.market_overview_enabled = not self.market_overview_enabled
            self.settings["market_overview_enabled"] = "1" if self.market_overview_enabled else "0"
            self.save_settings()
            if self.market_overview_enabled:
                self.market_overview_store.schedule_after(self.market_overview_interval_minutes)
            await self.edit_message(
                chat_id,
                message_id,
                self.market_overview_settings_text(
                    "Scheduling enabled. The first approval preview will be generated after the configured interval."
                    if self.market_overview_enabled
                    else "Scheduling paused. Any already pending approval remains available."
                ),
                self.market_overview_settings_markup(),
            )
        elif data == "generate_market_overview":
            try:
                if await self.resend_pending_market_overview(user_id):
                    note = "The current pending approval was sent to you again."
                else:
                    await self.create_market_overview_proposal()
                    note = "A fresh approval preview was sent to all authorized administrators."
            except Exception as exc:
                print(
                    f"Manual market overview generation failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                note = "The overview could not be generated. The previous state was preserved."
            await self.edit_message(
                chat_id,
                message_id,
                self.market_overview_settings_text(note),
                self.market_overview_settings_markup(),
            )
        elif data == "access_settings":
            await self.edit_message(chat_id, message_id, self.access_settings_text(), self.access_settings_markup())
        elif data == "system_settings":
            await self.edit_message(chat_id, message_id, self.system_settings_text(), self.system_settings_markup())
        elif data == "utya_movement_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.utya_movement_settings_text(),
                self.utya_movement_settings_markup(),
            )
        elif data == "toggle_utya_movement_alerts":
            self.utya_movement_enabled = not self.utya_movement_enabled
            self.settings["utya_movement_enabled"] = "1" if self.utya_movement_enabled else "0"
            self.save_settings()
            if self.utya_movement_enabled:
                self.utya_movement_tracker.reset()
            await self.edit_message(
                chat_id,
                message_id,
                self.utya_movement_settings_text(
                    "Alerts enabled. The next valid UTYA price will become the new reference."
                    if self.utya_movement_enabled
                    else "Alerts disabled."
                ),
                self.utya_movement_settings_markup(),
            )
        elif data == "reset_utya_movement_reference":
            self.utya_movement_tracker.reset()
            await self.edit_message(
                chat_id,
                message_id,
                self.utya_movement_settings_text(
                    "Reference cleared. The next valid UTYA price will become the new reference."
                ),
                self.utya_movement_settings_markup(),
            )
        elif data == "stats":
            await self.edit_message(chat_id, message_id, self.usage_stats_text(), self.usage_stats_markup())
        elif data == "stats_groups":
            await self.edit_message(
                chat_id,
                message_id,
                self.usage_stats_groups_text(),
                self.usage_stats_groups_markup(),
            )
        elif data == "subscription_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.subscription_message_settings_text(),
                self.subscription_message_settings_markup(),
            )
        elif data == "general_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.general_message_settings_text(),
                self.general_message_settings_markup(),
            )
        elif data == "token_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.token_message_settings_text(),
                self.token_message_settings_markup(),
            )
        elif data == "conversion_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.conversion_message_settings_text(),
                self.conversion_message_settings_markup(),
            )
        elif data == "inline_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.inline_message_settings_text(),
                self.inline_message_settings_markup(),
            )
        elif data == "trending_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.trending_message_settings_text(),
                self.trending_message_settings_markup(),
            )
        elif data == "pulse_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.pulse_message_settings_text(),
                self.pulse_message_settings_markup(),
            )
        elif data == "pulse_emoji_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.pulse_emoji_settings_text(),
                self.pulse_emoji_settings_markup(),
            )
        elif data == "pulse_coin_emoji_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.pulse_coin_emoji_settings_text(),
                self.pulse_coin_emoji_settings_markup(),
            )
        elif data.startswith("edit_pulse_emoji:"):
            key = data.removeprefix("edit_pulse_emoji:")
            if key not in self.PULSE_EMOJI_DEFAULTS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.pulse_emoji_settings_text(),
                    self.pulse_emoji_settings_markup(),
                )
            else:
                self.pending_text_edits[user_id] = f"{self.PULSE_EMOJI_SETTING_PREFIX}{key}"
                await self.send_message(
                    chat_id,
                    self.edit_pulse_emoji_prompt(key),
                    reply_markup=self.cancel_edit_markup(),
                )
        elif data.startswith("edit_pulse_coin_emoji:"):
            ticker = data.removeprefix("edit_pulse_coin_emoji:").upper()
            if ticker not in self.PULSE_COIN_EMOJI_DEFAULTS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.pulse_coin_emoji_settings_text(),
                    self.pulse_coin_emoji_settings_markup(),
                )
            else:
                self.pending_text_edits[user_id] = (
                    f"{self.PULSE_COIN_EMOJI_SETTING_PREFIX}{ticker}"
                )
                await self.send_message(
                    chat_id,
                    self.edit_pulse_coin_emoji_prompt(ticker),
                    reply_markup=self.cancel_edit_markup(),
                )
        elif data == "new_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.new_message_settings_text(),
                self.new_message_settings_markup(),
            )
        elif data == "preview_pulse_format":
            await self.send_message(chat_id, self.pulse_format_preview())
        elif data == "preview_new_format":
            await self.send_message(chat_id, self.new_format_preview())
        elif data == "alert_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_message_settings_text(),
                self.alert_message_settings_markup(),
            )
        elif data == "guide_message_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.guide_message_settings_text(),
                self.guide_message_settings_markup(),
            )
        elif data == "public_button_settings":
            await self.edit_message(
                chat_id,
                message_id,
                self.public_button_settings_text(),
                self.public_button_settings_markup(),
            )
        elif data.startswith("public_button_group:"):
            group = data.removeprefix("public_button_group:")
            if group not in self.PUBLIC_BUTTON_GROUPS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.public_button_settings_text(),
                    self.public_button_settings_markup(),
                )
            else:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.public_button_group_text(group),
                    self.public_button_group_markup(group),
                )
        elif data.startswith("edit_public_button:"):
            key = data.removeprefix("edit_public_button:")
            if key not in self.PUBLIC_BUTTON_DEFAULTS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.public_button_settings_text(),
                    self.public_button_settings_markup(),
                )
            else:
                self.pending_text_edits[user_id] = f"public_button:{key}"
                await self.send_message(
                    chat_id,
                    self.edit_public_button_prompt(key),
                    reply_markup=self.cancel_edit_markup(),
                )
        elif data.startswith("public_message_group:"):
            group = data.removeprefix("public_message_group:")
            if group not in self.PUBLIC_MESSAGE_GROUPS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.alert_message_settings_text(),
                    self.alert_message_settings_markup(),
                )
            else:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.public_message_group_text(group),
                    self.public_message_group_markup(group),
                )
        elif data.startswith("edit_public_message:"):
            key = data.removeprefix("edit_public_message:")
            if key not in self.PUBLIC_MESSAGE_DEFAULTS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.message_settings_text(),
                    self.message_settings_markup(),
                )
            else:
                self.pending_text_edits[user_id] = key
                await self.send_message(
                    chat_id,
                    self.edit_message_prompt(key),
                    reply_markup=self.cancel_edit_markup(),
                )
        elif data.startswith("edit_guide_message:"):
            key = data.removeprefix("edit_guide_message:")
            if key not in self.GUIDE_MESSAGE_KEYS:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.guide_message_settings_text(),
                    self.guide_message_settings_markup(),
                )
            else:
                self.pending_text_edits[user_id] = key
                await self.send_message(
                    chat_id,
                    self.edit_message_prompt(key),
                    reply_markup=self.cancel_edit_markup(),
                )
        elif data == "broadcast_settings":
            self.pending_text_edits.pop(user_id, None)
            await self.edit_message(
                chat_id,
                message_id,
                self.broadcast_settings_text(),
                self.broadcast_settings_markup(),
            )
        elif data == "compose_broadcast":
            self.pending_text_edits[user_id] = "broadcast_message"
            await self.send_message(
                chat_id,
                self.broadcast_prompt_text(),
                reply_markup=self.cancel_edit_markup(),
            )
        elif data == "cancel_broadcast":
            self.pending_broadcasts.pop(user_id, None)
            self.pending_text_edits.pop(user_id, None)
            await self.edit_message(
                chat_id,
                message_id,
                self.broadcast_settings_text("Broadcast cancelled."),
                self.broadcast_settings_markup(),
            )
        elif data == "confirm_broadcast":
            draft = self.pending_broadcasts.pop(user_id, None)
            if not draft:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.broadcast_settings_text("This draft is no longer available."),
                    self.broadcast_settings_markup(),
                )
            elif self.broadcast_lock.locked():
                self.pending_broadcasts[user_id] = draft
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.broadcast_settings_text("Another broadcast is currently being sent."),
                    self.broadcast_settings_markup(),
                )
            else:
                await self.edit_message(
                    chat_id,
                    message_id,
                    "📣 <b>Broadcast in progress</b>\n\nSending the approved message safely...",
                    self.back_markup("broadcast_settings"),
                )
                sent, failed, removed = await self.broadcast_message(draft)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.broadcast_result_text(sent, failed, removed),
                    self.broadcast_settings_markup(),
                )
        elif data == "preview":
            try:
                await self.send_preview(chat_id)
            except Exception as exc:
                print(f"Admin preview failed: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(chat_id, "⚠️ <b>Preview unavailable</b>\n\nTry again shortly.")
        elif data == "post":
            try:
                await self.post_once(self.channel)
                await self.send_message(
                    chat_id,
                    f"✅ Posted dashboard to <b>{html.escape(self.channel)}</b>.",
                    reply_markup=self.menu_markup(user_id),
                )
            except Exception as exc:
                print(f"Admin channel post failed: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Post failed</b>\n\nThe dashboard was not posted. Check the channel and try again.",
                    reply_markup=self.publishing_settings_markup(),
                )
        elif data == "status":
            await self.edit_message(chat_id, message_id, self.status_text(), self.menu_markup(user_id))
        elif data == "refresh_dashboard_cache":
            await self.edit_message(
                chat_id,
                message_id,
                "🔄 <b>Refreshing dashboard</b>\n\nLoading current market data...",
                self.back_markup("dashboard_settings"),
            )
            try:
                await self.refresh_image_cache(force=True)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.dashboard_settings_text("Dashboard refreshed successfully."),
                    self.dashboard_settings_markup(),
                )
            except Exception as exc:
                print(f"Manual dashboard refresh failed: {type(exc).__name__}: {exc}", flush=True)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.dashboard_settings_text("Refresh failed; the last valid image remains available."),
                    self.dashboard_settings_markup(),
                )
        elif data == "toggle_private_subscription":
            self.require_private_subscription = not self.require_private_subscription
            self.settings["require_private_subscription"] = "1" if self.require_private_subscription else "0"
            self.save_settings()
            self.membership_cache.clear()
            await self.edit_message(chat_id, message_id, self.access_settings_text(), self.access_settings_markup())
        elif data == "refresh_trending_snapshot":
            await self.edit_message(
                chat_id,
                message_id,
                "🔄 <b>Refreshing trending snapshot</b>\n\nLoading the current TON ranking...",
                self.back_markup("trending_settings"),
            )
            try:
                await self.trending_service.refresh(force=True)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.trending_settings_text("Trending snapshot refreshed successfully."),
                    self.trending_settings_markup(),
                )
            except Exception as exc:
                print(f"Manual trending refresh failed: {type(exc).__name__}: {exc}", flush=True)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.trending_settings_text("Refresh failed; the previous snapshot remains active."),
                    self.trending_settings_markup(),
                )
        elif data == "message_settings":
            self.pending_text_edits.pop(user_id, None)
            await self.edit_message(
                chat_id,
                message_id,
                self.message_settings_text(),
                self.message_settings_markup(),
            )
        elif data in self.MESSAGE_EDIT_CALLBACK_KEYS:
            key = self.MESSAGE_EDIT_CALLBACK_KEYS[data]
            self.pending_text_edits[user_id] = key
            await self.send_message(
                chat_id,
                self.edit_message_prompt(key),
                reply_markup=self.cancel_edit_markup(),
            )
        elif data in {
            "edit_channel",
            "edit_required_channel",
            "edit_trending_count",
            "edit_trending_liquidity",
            "edit_background_refresh",
            "edit_image_cache",
            "edit_utya_movement_threshold",
            "edit_market_overview_channel",
            "edit_market_overview_interval",
        }:
            key = {
                "edit_channel": "channel",
                "edit_required_channel": "required_channel",
                "edit_trending_count": "trending_result_limit",
                "edit_trending_liquidity": "trending_min_liquidity_usd",
                "edit_background_refresh": "background_refresh_seconds",
                "edit_image_cache": "image_cache_seconds",
                "edit_utya_movement_threshold": "utya_movement_threshold_percent",
                "edit_market_overview_channel": "market_overview_channel",
                "edit_market_overview_interval": "market_overview_interval_minutes",
            }[data]
            self.pending_text_edits[user_id] = key
            await self.send_message(
                chat_id,
                self.edit_value_prompt(key),
                reply_markup=self.cancel_edit_markup(),
            )
        elif data == "cancel_message_edit":
            pending_key = self.pending_text_edits.get(user_id, "")
            self.pending_text_edits.pop(user_id, None)
            await self.edit_message(
                chat_id,
                message_id,
                self.settings_text_for_key(pending_key),
                self.settings_markup_for_key(pending_key),
            )

    @staticmethod
    def guide_callback_data(user_id: int, page: str) -> str:
        return f"guide:{user_id}:{page}"

    def guide_private_only_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        self.public_button(
                            "guide_private_open",
                            url="https://t.me/memepricesbot?start=guide",
                        )
                    ]
                ]
            }
        )

    def guide_markup(self, user_id: int, page: str) -> str:
        callback = lambda target: self.guide_callback_data(user_id, target)
        page_rows: dict[str, list[list[dict[str, str]]]] = {
            "home": [
                [self.public_button("guide_start", callback_data=callback("start"))],
                [self.public_button("guide_research", callback_data=callback("research"))],
                [self.public_button("guide_safety", callback_data=callback("safety"))],
                [self.public_button("guide_trading", callback_data=callback("trading"))],
                [self.public_button("guide_commands", callback_data=callback("commands"))],
                [self.public_button("guide_inline", callback_data=callback("inline"))],
                [self.public_button("guide_new", callback_data=callback("new"))],
                [self.public_button("guide_terms", callback_data=callback("terms"))],
                [self.public_button("guide_live_channels", callback_data=callback("live_channels"))],
                [self.public_button("guide_close", callback_data=callback("close"))],
            ],
            "start": [
                [self.public_button("guide_research", callback_data=callback("research"))],
                [self.public_button("guide_commands", callback_data=callback("commands"))],
                [self.public_button("guide_inline", callback_data=callback("inline"))],
            ],
            "research": [
                [self.public_button("guide_search", callback_data=callback("research_search"))],
                [self.public_button("guide_inline", callback_data=callback("inline"))],
                [self.public_button("guide_safety", callback_data=callback("safety"))],
            ],
            "research_search": [
                [self.public_button("guide_inline", callback_data=callback("inline"))],
            ],
            "safety": [
                [self.public_button("guide_research", callback_data=callback("research"))],
                [self.public_button("guide_trading", callback_data=callback("trading"))],
            ],
            "trading": [
                [self.public_button("guide_converter", callback_data=callback("converter"))],
                [self.public_button("guide_safety", callback_data=callback("safety"))],
            ],
            "converter": [
                [self.public_button("guide_inline", callback_data=callback("inline"))],
            ],
            "commands": [
                [self.public_button("guide_search", callback_data=callback("research_search"))],
                [self.public_button("guide_converter", callback_data=callback("converter"))],
                [self.public_button("guide_inline", callback_data=callback("inline"))],
                [self.public_button("guide_trending", callback_data=callback("trending"))],
                [self.public_button("guide_new", callback_data=callback("new"))],
            ],
            "new": [
                [self.public_button("guide_research", callback_data=callback("research"))],
                [self.public_button("guide_safety", callback_data=callback("safety"))],
            ],
            "inline": [
                [self.public_button("guide_converter", callback_data=callback("converter"))],
                [self.public_button("guide_research", callback_data=callback("research"))],
            ],
            "terms": [
                [self.public_button("guide_start", callback_data=callback("start"))],
                [self.public_button("guide_safety", callback_data=callback("safety"))],
            ],
            "live_channels": [
                [
                    self.public_button(
                        "guide_add_channels",
                        url="https://t.me/addlist/njrPpQOLzgAzM2U1",
                    )
                ],
            ],
        }
        rows = list(page_rows.get(page, page_rows["home"]))
        if page != "home":
            rows.append(
                [
                    self.public_button("guide_back", callback_data=callback("home")),
                    self.public_button("guide_close", callback_data=callback("close")),
                ]
            )
        return json.dumps({"inline_keyboard": rows})

    def guide_text(self, page: str) -> str:
        page_keys = {
            "home": "guide_home_message",
            "start": "guide_start_message",
            "research": "guide_research_message",
            "research_search": "guide_research_search_message",
            "safety": "guide_safety_message",
            "trading": "guide_trading_message",
            "converter": "guide_converter_message",
            "inline": "guide_inline_message",
            "commands": "guide_commands_message",
            "new": "guide_new_message",
            "terms": "guide_terms_message",
            "live_channels": "guide_live_channels_message",
        }
        return self.render_public_message(page_keys.get(page, "guide_home_message"))

    async def send_guide_home(self, chat_id: int, user_id: int) -> None:
        await self.send_message(
            chat_id,
            self.guide_text("home"),
            reply_markup=self.guide_markup(user_id, "home"),
        )

    async def handle_guide_callback(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str,
        message_id: int,
        callback_id: str,
        data: str,
    ) -> None:
        if chat_type != "private":
            await self.answer_callback(callback_id, "The guide works only in private messages.")
            return

        parts = data.split(":", 2)
        try:
            owner_id = int(parts[1])
            page = parts[2]
        except (IndexError, TypeError, ValueError):
            await self.answer_callback(callback_id, "This guide button is no longer valid.")
            return

        if owner_id != user_id:
            await self.answer_callback(callback_id, "Open your own guide with /guide.")
            return

        await self.answer_callback(callback_id)
        if not await self.ensure_subscribed(user_id, chat_id, chat_type):
            return

        if page == "close":
            await self.delete_guide_message(chat_id, message_id)
            return
        if page == "trending":
            await self.send_trending(chat_id)
            return

        valid_pages = {
            "home",
            "start",
            "research",
            "research_search",
            "safety",
            "trading",
            "converter",
            "inline",
            "commands",
            "new",
            "terms",
            "live_channels",
        }
        page = page if page in valid_pages else "home"
        await self.edit_guide_message(
            chat_id,
            message_id,
            self.guide_text(page),
            self.guide_markup(user_id, page),
        )

    async def edit_guide_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: str,
    ) -> None:
        if not message_id:
            return
        try:
            result = await self.api(
                "editMessageText",
                {
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    "reply_markup": reply_markup,
                },
            )
            if result.get("ok"):
                return
            description = str(result.get("description") or "")
            if "message is not modified" not in description.casefold():
                print(f"Guide edit skipped: {description or 'unknown Telegram error'}", flush=True)
        except Exception as exc:
            print(f"Guide edit failed: {type(exc).__name__}: {exc}", flush=True)

    async def delete_guide_message(self, chat_id: int, message_id: int) -> None:
        if not message_id:
            return
        try:
            result = await self.api(
                "deleteMessage",
                {"chat_id": str(chat_id), "message_id": str(message_id)},
            )
            if not result.get("ok"):
                description = str(result.get("description") or "unknown Telegram error")
                print(f"Guide close skipped: {description}", flush=True)
        except Exception as exc:
            print(f"Guide close failed: {type(exc).__name__}: {exc}", flush=True)

    async def post_once(self, chat_id: str | int) -> None:
        image = await self.render_image_buffer()
        await self.send_photo(chat_id, image)

    async def send_preview(self, chat_id: int) -> None:
        image = await self.render_image_buffer()
        await self.send_photo(
            chat_id,
            image,
            caption="Preview render with current placeholder values.",
            reply_markup=self.menu_markup(chat_id),
        )

    async def send_meme(self, chat_id: int) -> None:
        started = time.monotonic()
        async with self.meme_send_semaphore:
            file_id = self.current_photo_file_id()
            if file_id:
                try:
                    await self.send_photo_file_id(chat_id, file_id)
                    elapsed = time.monotonic() - started
                    print(f"/meme served chat={chat_id} mode=file_id in {elapsed:.2f}s", flush=True)
                    return
                except Exception as exc:
                    print(f"Cached file_id send failed, uploading image: {type(exc).__name__}: {exc}", flush=True)
                    self.cached_photo_file_id = None
                    self.cached_photo_hash = None

            uploaded_for_this_chat = False
            async with self.photo_upload_lock:
                file_id = self.current_photo_file_id()
                if not file_id:
                    image = await self.fast_image_buffer()
                    file_id = await self.send_photo(chat_id, image)
                    self.remember_photo_file_id(file_id)
                    uploaded_for_this_chat = True

            if uploaded_for_this_chat:
                mode = "upload"
            else:
                await self.send_photo_file_id(chat_id, file_id)
                mode = "file_id_after_wait"
        elapsed = time.monotonic() - started
        print(f"/meme served chat={chat_id} mode={mode} in {elapsed:.2f}s", flush=True)

    async def send_trending(self, chat_id: int) -> None:
        snapshot = self.trending_service.current()
        if snapshot is None:
            try:
                snapshot = await self.trending_service.refresh()
            except Exception as exc:
                print(f"Trending request failed: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(chat_id, self.trending_unavailable_message)
                return
        await self.send_message(chat_id, self.format_trending_message(snapshot))

    async def refresh_pulse_cache(self, *, force: bool = False) -> PulseResult:
        async with self.pulse_lock:
            market_result = await self.pulse_market_service.current(force=force)
            holder_counts = self.holder_service.cached_counts(list(KNOWN_TOKEN_ADDRESSES))
            cached_by_ticker = {
                value.ticker.upper(): value for value in self.price_service.cached_prices()
            }
            values: list[PulseCoinValue] = []
            for observation in market_result.observations:
                ticker = observation.ticker.upper()
                cached = cached_by_ticker.get(ticker)
                holder = holder_counts.get(ticker)
                values.append(
                    PulseCoinValue(
                        ticker=ticker,
                        price=(
                            observation.price_usd
                            if observation.price_usd is not None
                            else getattr(cached, "price", None)
                        ),
                        change_24h=(
                            observation.change_h24
                            if observation.change_h24 is not None
                            else getattr(cached, "change_24h", None)
                        ),
                        market_cap=(
                            observation.market_cap_usd
                            if observation.market_cap_usd is not None
                            else getattr(cached, "market_cap", None)
                        ),
                        holders=(
                            holder.count
                            if holder is not None and holder.count is not None
                            else getattr(cached, "holders", None)
                        ),
                        liquidity=observation.liquidity_usd,
                        change_m5=observation.change_m5,
                        change_h1=observation.change_h1,
                        change_h6=observation.change_h6,
                        volume_m5=observation.volume_m5_usd,
                        volume_h1=observation.volume_h1_usd,
                        volume_h24=observation.volume_h24_usd,
                        buys_m5=observation.buys_m5,
                        sells_m5=observation.sells_m5,
                        buys_h1=observation.buys_h1,
                        sells_h1=observation.sells_h1,
                        largest_buy_usd=observation.largest_buy_usd,
                        largest_buy_at=observation.largest_buy_at,
                        market_observed_at=observation.observed_at,
                        chart_url=observation.chart_url,
                        recent_buys=tuple(
                            PulseBuy(
                                trade_id=trade.trade_id,
                                amount_usd=trade.amount_usd,
                                observed_at=trade.observed_at,
                                pool_address=trade.pool_address,
                                tx_hash=trade.tx_hash,
                            )
                            for trade in observation.recent_buys
                        ),
                    )
                )
            gram = cached_by_ticker.get("GRAM")
            if gram is not None and gram.price is not None:
                values.append(
                    PulseCoinValue(
                        ticker="GRAM",
                        price=gram.price,
                        change_24h=gram.change_24h,
                    )
                )
            await asyncio.to_thread(
                self.pulse_service.record,
                values,
                now=market_result.updated_at,
                source_complete=market_result.coverage_complete,
            )
            result = await asyncio.to_thread(self.pulse_service.current, now=time.time())

        # Evaluate UTYA from the independent one-minute market feed as well as
        # the dashboard refresh path. MovementTracker's lock and acknowledged
        # baseline make concurrent observations idempotent, while this second
        # source keeps chart/holder refresh failures from creating alert gaps.
        await self.process_utya_movement_alert(values)
        return result

    async def send_pulse(self, chat_id: int) -> None:
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(self.pulse_service.current, now=time.time())
            if not result.available:
                result = await self.refresh_pulse_cache(force=True)
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(
                f"Pulse request failed after {elapsed:.2f}s: {type(exc).__name__}: {exc}",
                flush=True,
            )
            await self.send_message(chat_id, self.pulse_unavailable_message)
            return

        if not result.available:
            await self.send_message(chat_id, self.pulse_unavailable_message)
            elapsed = time.monotonic() - started
            print(f"/pulse served chat={chat_id} status=unavailable in {elapsed:.2f}s", flush=True)
            return
        await self.send_message(chat_id, self.format_pulse_message(result))
        elapsed = time.monotonic() - started
        print(
            f"/pulse served chat={chat_id} events={len(result.events)} in {elapsed:.2f}s",
            flush=True,
        )

    def format_pulse_message(self, result: PulseResult) -> str:
        updated_at = datetime.fromtimestamp(result.updated_at, timezone.utc).strftime("%H:%M UTC")
        title = self.render_public_message("pulse_title")
        if not result.events:
            return self.render_public_message(
                "pulse_normal_message",
                {
                    "[TITLE]": title,
                    "[UPDATED_AT]": updated_at,
                },
                trusted_placeholders={"[TITLE]"},
            )

        event_rows: list[str] = []
        for event in result.events:
            safe_details = "\n".join(f"• {html.escape(detail)}" for detail in event.details)
            ticker = html.escape(event.ticker.upper())
            coin_emoji = self.pulse_coin_emoji_for_ticker(event.ticker)
            signal_emoji = self.pulse_emoji_for_event(event)
            if event.chart_url:
                chart_url = html.escape(event.chart_url, quote=True)
                ticker_link = f'<a href="{chart_url}">{ticker}</a>'
                ticker_with_prefix_link = f'<a href="{chart_url}">${ticker}</a>'
            else:
                ticker_link = ticker
                ticker_with_prefix_link = f"${ticker}"
            event_rows.append(
                self.render_public_message(
                    "pulse_event",
                    {
                        "[EMOJI]": coin_emoji,
                        "[COIN_EMOJI]": coin_emoji,
                        "[SIGNAL_EMOJI]": signal_emoji,
                        "$[TICKER]": ticker_with_prefix_link,
                        "[TICKER]": ticker_link,
                        "[DETAILS]": safe_details,
                        "[AGE]": self.format_pulse_age(result.updated_at, event.observed_at),
                        "[SCORE]": f"{event.score:.0f}",
                        "[SIGNALS]": ", ".join(event.signal_types),
                    },
                    trusted_placeholders={
                        "[EMOJI]",
                        "[COIN_EMOJI]",
                        "[SIGNAL_EMOJI]",
                        "$[TICKER]",
                        "[TICKER]",
                        "[DETAILS]",
                    },
                )
            )
        return self.render_public_message(
            "pulse_message",
            {
                "[TITLE]": title,
                "[EVENTS]": "\n\n".join(event_rows),
                "[UPDATED_AT]": updated_at,
            },
            trusted_placeholders={"[TITLE]", "[EVENTS]"},
        )

    def format_market_overview(self, values: list[Any]) -> str:
        by_ticker = {
            str(getattr(value, "ticker", "") or "").upper(): value
            for value in values
        }
        price_rows: list[str] = []
        market_cap_rows: list[str] = []
        for ticker in OVERVIEW_TICKERS:
            value = by_ticker.get(ticker)
            channel = PRICE_CHANNELS[ticker]
            channel_url = f"https://t.me/{channel.removeprefix('@')}"
            price = getattr(value, "price", None) if value is not None else None
            market_cap = getattr(value, "market_cap", None) if value is not None else None
            change_24h = getattr(value, "change_24h", None) if value is not None else None
            common = {
                "[TICKER]": html.escape(ticker),
                "[CHANNEL_URL]": html.escape(channel_url, quote=True),
            }
            price_rows.append(
                self.render_public_message(
                    "market_overview_price_row",
                    {**common, "[PRICE]": "—" if price is None else format_price(price)},
                    trusted_placeholders={"[TICKER]", "[CHANNEL_URL]", "[PRICE]"},
                )
            )
            market_cap_rows.append(
                self.render_public_message(
                    "market_overview_cap_row",
                    {
                        **common,
                        "[MARKET_CAP]": (
                            "—" if market_cap is None else f"${format_compact_number(market_cap)}"
                        ),
                        "[CHANGE_24H]": "—" if change_24h is None else format_change(change_24h),
                    },
                    trusted_placeholders={
                        "[TICKER]",
                        "[CHANNEL_URL]",
                        "[MARKET_CAP]",
                        "[CHANGE_24H]",
                    },
                )
            )
        return self.render_public_message(
            "market_overview_message",
            {
                "[PRICES]": "\n".join(price_rows),
                "[MARKET_CAPS]": "\n".join(market_cap_rows),
            },
            trusted_placeholders={"[PRICES]", "[MARKET_CAPS]"},
        )

    async def market_overview_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                pending = self.market_overview_store.state.get("pending")
                due_at = float(self.market_overview_store.state.get("next_due_at") or 0)
                if self.market_overview_enabled and not pending and time.time() >= due_at:
                    await self.create_market_overview_proposal()
            except Exception as exc:
                print(
                    f"Market overview scheduler failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
            await asyncio.sleep(15)

    async def create_market_overview_proposal(self) -> bool:
        async with self.market_overview_lock:
            if self.market_overview_store.state.get("pending"):
                return False

            values = await self.price_service.fetch_prices()
            if not any(
                str(getattr(value, "ticker", "") or "").upper() in OVERVIEW_TICKERS
                and getattr(value, "price", None) is not None
                for value in values
            ):
                raise RuntimeError("No valid tracked prices are available")

            overview_text = self.format_market_overview(values)
            proposal_id = hashlib.sha256(
                f"{time.time_ns()}:{overview_text}".encode("utf-8")
            ).hexdigest()[:12]
            pending = {
                "id": proposal_id,
                "text": overview_text,
                "channel": self.market_overview_channel,
                "created_at": time.time(),
                "admin_messages": {},
            }
            self.market_overview_store.state["pending"] = pending
            self.market_overview_store.save()

            delivered = 0
            for admin_id in sorted(self.admin_ids):
                try:
                    message_id = await self.send_market_overview_control(admin_id, pending)
                except Exception as exc:
                    print(
                        f"Market overview approval delivery failed admin={admin_id}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    continue
                self.remember_market_overview_admin_message(pending, admin_id, message_id)
                delivered += 1
            self.market_overview_store.save()

            if not delivered:
                self.market_overview_store.state["pending"] = None
                self.market_overview_store.state["next_due_at"] = time.time() + 5 * 60
                self.market_overview_store.save()
                raise RuntimeError("The approval preview could not be delivered to any administrator")
            print(
                f"Market overview proposal created id={proposal_id} admins={delivered}",
                flush=True,
            )
            return True

    async def send_market_overview_control(
        self,
        admin_id: int,
        pending: dict[str, Any],
    ) -> int:
        text = self.market_overview_control_text(pending)
        result = await self.send_message_checked(
            admin_id,
            text,
            reply_markup=self.market_overview_approval_markup(str(pending.get("id") or "")),
        )
        return int((result.get("result") or {}).get("message_id") or 0)

    def market_overview_control_text(self, pending: dict[str, Any]) -> str:
        created_at = datetime.fromtimestamp(
            float(pending.get("created_at") or time.time()), timezone.utc
        ).strftime("%d/%m/%Y %H:%M UTC")
        return (
            f"{pending.get('text') or ''}\n\n"
            "────────────\n"
            "📝 <b>Approval required</b>\n"
            f"Destination: <b>{html.escape(str(pending.get('channel') or self.market_overview_channel))}</b>\n"
            f"Generated: <b>{created_at}</b>\n\n"
            "Nothing is posted until one authorized admin approves this preview."
        )

    @staticmethod
    def market_overview_approval_markup(proposal_id: str) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ Approve and post",
                            "callback_data": f"overview:approve:{proposal_id}",
                        }
                    ],
                    [
                        {
                            "text": "⏭ Skip this overview",
                            "callback_data": f"overview:skip:{proposal_id}",
                        }
                    ],
                ]
            }
        )

    async def send_message_checked(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self.api("sendMessage", payload)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("description") or "Telegram rejected the message"))
        return result

    @staticmethod
    def remember_market_overview_admin_message(
        pending: dict[str, Any],
        admin_id: int,
        message_id: int,
    ) -> None:
        if message_id <= 0:
            return
        admin_messages = pending.setdefault("admin_messages", {})
        existing = admin_messages.get(str(admin_id))
        if isinstance(existing, list):
            message_ids = []
            for value in existing:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    message_ids.append(parsed)
        elif existing:
            message_ids = [int(existing)]
        else:
            message_ids = []
        if message_id not in message_ids:
            message_ids.append(message_id)
        admin_messages[str(admin_id)] = message_ids

    @staticmethod
    def market_overview_admin_message_refs(
        admin_messages: dict[str, Any],
    ) -> set[tuple[int, int]]:
        refs: set[tuple[int, int]] = set()
        for raw_chat_id, raw_message_ids in admin_messages.items():
            values = raw_message_ids if isinstance(raw_message_ids, list) else [raw_message_ids]
            for raw_message_id in values:
                try:
                    chat_id = int(raw_chat_id)
                    message_id = int(raw_message_id)
                except (TypeError, ValueError):
                    continue
                if chat_id and message_id > 0:
                    refs.add((chat_id, message_id))
        return refs

    async def delete_message_safely(self, chat_id: int, message_id: int) -> None:
        try:
            result = await self.api(
                "deleteMessage",
                {"chat_id": str(chat_id), "message_id": str(message_id)},
            )
            if result.get("ok"):
                return
            description = str(result.get("description") or "unknown Telegram error")
            if "message to delete not found" not in description.lower():
                print(
                    f"Market overview preview cleanup skipped chat={chat_id} "
                    f"message={message_id}: {description}",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"Market overview preview cleanup failed chat={chat_id} message={message_id}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    async def resend_pending_market_overview(self, admin_id: int) -> bool:
        async with self.market_overview_lock:
            pending = self.market_overview_store.state.get("pending")
            if not isinstance(pending, dict):
                return False
            message_id = await self.send_market_overview_control(admin_id, pending)
            self.remember_market_overview_admin_message(pending, admin_id, message_id)
            self.market_overview_store.save()
            return True

    async def handle_market_overview_action(
        self,
        *,
        user_id: int,
        callback_id: str,
        action: str,
        proposal_id: str,
        chat_id: int = 0,
        message_id: int = 0,
    ) -> None:
        async with self.market_overview_lock:
            pending = self.market_overview_store.state.get("pending")
            if not isinstance(pending, dict) or str(pending.get("id") or "") != proposal_id:
                await self.answer_callback(callback_id, "This overview was already handled.")
                return

            if action == "approve":
                try:
                    await self.send_message_checked(
                        str(pending.get("channel") or self.market_overview_channel),
                        str(pending.get("text") or ""),
                    )
                except Exception as exc:
                    print(
                        f"Market overview post failed id={proposal_id}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    await self.answer_callback(callback_id, "Post failed. The approval remains pending.")
                    return
                self.market_overview_store.state["last_posted_at"] = time.time()
                self.market_overview_store.state["last_posted_channel"] = str(
                    pending.get("channel") or self.market_overview_channel
                )
                self.market_overview_store.state["last_approved_by"] = user_id
                callback_text = "Market overview posted."
            else:
                callback_text = "Market overview skipped."

            admin_messages = dict(pending.get("admin_messages") or {})
            message_refs = self.market_overview_admin_message_refs(admin_messages)
            if chat_id and message_id:
                message_refs.add((chat_id, message_id))
            self.market_overview_store.state["pending"] = None
            self.market_overview_store.state["next_due_at"] = (
                time.time() + self.market_overview_interval_minutes * 60
            )
            self.market_overview_store.save()

        await self.answer_callback(callback_id, callback_text)
        await asyncio.gather(
            *(
                self.delete_message_safely(admin_chat_id, admin_message_id)
                for admin_chat_id, admin_message_id in message_refs
            )
        )
        print(
            f"Market overview proposal {'posted' if action == 'approve' else 'skipped'} "
            f"id={proposal_id} admin={user_id}",
            flush=True,
        )

    @staticmethod
    def format_pulse_summary(result: PulseResult) -> str:
        return "\n".join(f"• {html.escape(row)}" for row in result.summary)

    @staticmethod
    def format_pulse_coverage(result: PulseResult) -> str:
        return ""

    def pulse_emoji_for_event(self, event: PulseEvent) -> str:
        primary_signal = event.signal_types[0] if event.signal_types else ""
        if primary_signal == "price":
            key = "price_down" if event.direction == "down" or event.emoji == "📉" else "price_up"
        else:
            key = primary_signal
        default = self.PULSE_EMOJI_DEFAULTS.get(key, event.emoji)
        configured = getattr(self, "pulse_emojis", {}).get(key, default)
        return self.normalize_persisted_pulse_emoji(configured, default)

    def pulse_coin_emoji_for_ticker(self, ticker: str) -> str:
        key = str(ticker or "").upper()
        default = self.PULSE_COIN_EMOJI_DEFAULTS.get(key, "🪙")
        configured = getattr(self, "pulse_coin_emojis", {}).get(key, default)
        return self.normalize_persisted_pulse_emoji(configured, default)

    @staticmethod
    def format_pulse_age(updated_at: float, observed_at: float | None) -> str:
        if observed_at is None:
            return "just now"
        seconds = max(0, int(updated_at - observed_at))
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        return f"{hours}h ago"

    def pulse_format_preview(self) -> str:
        now = time.time()
        preview = self.format_pulse_message(
            PulseResult(
                updated_at=now,
                events=(
                    PulseEvent(
                        ticker="UTYA",
                        emoji="🐋",
                        score=94.0,
                        details=(
                            "Large buy: $12,000",
                            "Volume spike: $8.4K in 5M · 4.2× recent pace",
                            "Price spike: +5.8% in 5M",
                            "Holder growth: +37 (+2.1%) in 1H",
                        ),
                        signal_types=("large_buy", "volume", "price", "holders"),
                        observed_at=now - 3 * 60,
                        direction="up",
                        chart_url="https://www.geckoterminal.com/ton/pools/preview-pool",
                    ),
                ),
            )
        )
        return "👁 <b>/pulse format preview</b>\n<i>Sample values only.</i>\n\n" + preview

    def new_format_preview(self) -> str:
        now = datetime.now(timezone.utc)
        sample = NewToken(
            token_address="EQPreviewTokenContractAddress",
            name="Preview Token",
            symbol="TOKEN",
            pool_address="preview-pool",
            dex_name="STON.fi",
            created_at=now - timedelta(hours=3),
            price_usd=0.0012,
            market_cap_usd=120_000,
            fdv_usd=120_000,
            liquidity_usd=18_500,
            volume_24h_usd=42_000,
            holders=640,
            verification="whitelist",
        )
        preview = self.format_new_tokens_message(
            NewTokensSnapshot(updated_at=now, tokens=(sample,)),
            (sample,),
            now=now,
        )
        return "👁 <b>/new format preview</b>\n<i>Sample values only.</i>\n\n" + preview

    async def send_new_tokens(self, chat_id: int) -> None:
        filters = NewTokenFilters()
        snapshot = self.new_tokens_service.current()
        if snapshot is None:
            try:
                snapshot = await self.new_tokens_service.refresh()
            except Exception as exc:
                print(f"New-token request failed: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(chat_id, self.new_unavailable_message)
                return

        candidates = self.new_tokens_service.candidates(snapshot, filters)
        if candidates:
            candidates = await self.new_tokens_service.enrich_market(candidates)
            candidates = await self.new_tokens_service.enrich_holders(candidates)
        tokens = self.new_tokens_service.select(
            NewTokensSnapshot(updated_at=snapshot.updated_at, tokens=candidates),
            filters,
        )
        if not tokens:
            await self.send_message(
                chat_id,
                self.render_public_message("new_empty_message"),
            )
            return
        await self.send_message(
            chat_id,
            self.format_new_tokens_message(snapshot, tokens),
        )

    async def send_alert_menu(self, chat_id: int, user_id: int, note: str = "") -> None:
        await self.send_message(
            chat_id,
            self.alert_home_text(user_id, note),
            reply_markup=self.alert_home_markup(),
        )

    def alert_home_text(self, user_id: int, note: str = "") -> str:
        alerts = self.alert_store.for_user(user_id)
        active_count = sum(1 for alert in alerts if alert.active)
        text = self.render_public_message(
            "alert_home_message",
            {
                "[ACTIVE_COUNT]": active_count,
                "[MAX_ALERTS]": self.alert_store.max_active_per_user,
                "[SAVED_COUNT]": len(alerts),
            },
        )
        return self.append_alert_note(text, note)

    def alert_home_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [self.public_button("alert_create", callback_data="alert:create")],
                    [self.public_button("alert_list", callback_data="alert:list:0")],
                    [self.public_button("alert_help", callback_data="alert:help")],
                    [self.public_button("alert_refresh", callback_data="alert:home")],
                ]
            }
        )

    def alert_cancel_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [self.public_button("alert_cancel_setup", callback_data="alert:cancel")],
                    [self.public_button("alert_home", callback_data="alert:home")],
                ]
            }
        )

    def alert_metric_markup(self, draft: AlertDraft) -> str:
        buttons = [[self.public_button("alert_price", callback_data="alert:metric:price")]]
        if draft.current_value is not None or draft.metric != "market_cap":
            buttons.append([self.public_button("alert_market_cap", callback_data="alert:metric:market_cap")])
        buttons.append([self.public_button("alert_change_24h", callback_data="alert:metric:change_24h")])
        buttons.append([self.public_button("alert_home", callback_data="alert:back:token")])
        buttons.append([self.public_button("alert_cancel", callback_data="alert:cancel")])
        return json.dumps({"inline_keyboard": buttons})

    def alert_direction_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        self.public_button("alert_above", callback_data="alert:direction:above"),
                        self.public_button("alert_below", callback_data="alert:direction:below"),
                    ],
                    [self.public_button("alert_change_metric", callback_data="alert:back:metric")],
                    [self.public_button("alert_cancel", callback_data="alert:cancel")],
                ]
            }
        )

    def alert_target_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [self.public_button("alert_back_direction", callback_data="alert:back:direction")],
                    [self.public_button("alert_cancel", callback_data="alert:cancel")],
                ]
            }
        )

    def alert_confirm_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [self.public_button("alert_save", callback_data="alert:confirm")],
                    [self.public_button("alert_change_target", callback_data="alert:back:target")],
                    [self.public_button("alert_cancel", callback_data="alert:cancel")],
                ]
            }
        )

    async def handle_alert_text_input(self, user_id: int, chat_id: int, text: str) -> None:
        draft = self.pending_alert_inputs.get(user_id)
        if draft is None:
            return
        if draft.step == "token":
            try:
                pair, result = await self.resolve_token_pair_for_query(text)
            except Exception as exc:
                print(f"Alert token resolution failed query={text!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(
                    chat_id,
                    self.render_public_message("alert_token_search_unavailable_message"),
                    reply_markup=self.alert_cancel_markup(),
                )
                return
            if pair is not None:
                await self.prepare_alert_pair(user_id, chat_id, pair)
                return
            if result is not None and result.has_choices:
                choices: dict[str, str] = {}
                rows: list[list[dict[str, str]]] = []
                for index, choice in enumerate((result.choices or [])[:6]):
                    key = str(index)
                    choices[key] = choice.token_address
                    rows.append(
                        [
                            self.public_button(
                                "alert_token_choice",
                                replacements={"[CHOICE]": choice_button_label(choice)},
                                callback_data=f"alert:token:{key}",
                            )
                        ]
                    )
                rows.append([self.public_button("alert_home", callback_data="alert:back:token")])
                rows.append([self.public_button("alert_cancel", callback_data="alert:cancel")])
                self.pending_alert_choices[user_id] = choices
                draft.step = "token_choice"
                await self.send_message(
                    chat_id,
                    self.render_public_message("alert_token_choices_message"),
                    reply_markup=json.dumps({"inline_keyboard": rows}),
                )
                return
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "alert_token_not_found_message",
                    {"[QUERY]": text},
                ),
                reply_markup=self.alert_cancel_markup(),
            )
            return

        if draft.step == "target":
            try:
                draft.target_value = parse_alert_target(text, draft.metric)
            except ValueError as exc:
                await self.send_message(
                    chat_id,
                    self.render_public_message(
                        "alert_invalid_target_message",
                        {"[ERROR]": str(exc)},
                    ),
                    reply_markup=self.alert_target_markup(),
                )
                return
            draft.step = "confirm"
            await self.send_message(
                chat_id,
                self.alert_confirmation_text(draft),
                reply_markup=self.alert_confirm_markup(),
            )
            return

        await self.send_message(
            chat_id,
            self.render_public_message("alert_continue_message"),
            reply_markup=self.alert_cancel_markup(),
        )

    async def prepare_alert_pair(
        self,
        user_id: int,
        chat_id: int,
        pair: TokenPair,
        *,
        message_id: int = 0,
    ) -> None:
        draft = self.pending_alert_inputs.setdefault(user_id, AlertDraft())
        draft.set_pair(pair)
        draft.step = "metric"
        draft.metric = ""
        draft.direction = ""
        draft.target_value = None
        draft.current_value = pair.price_usd
        self.pending_alert_choices.pop(user_id, None)
        text = self.render_public_message(
            "alert_metric_message",
            {
                "[TOKEN_NAME]": pair.name,
                "[TOKEN_SYMBOL]": pair.symbol.upper(),
                "[PRICE]": format_metric_value("price", pair.price_usd),
                "[MARKET_CAP]": format_metric_value("market_cap", pair.market_cap),
                "[CHANGE_24H]": format_metric_value("change_24h", pair.price_change.get("h24")),
            },
        )
        markup = self.alert_metric_markup(draft)
        if message_id:
            await self.edit_message(chat_id, message_id, text, markup)
        else:
            await self.send_message(chat_id, text, reply_markup=markup)

    def alert_confirmation_text(self, draft: AlertDraft) -> str:
        return self.render_public_message(
            "alert_confirmation_message",
            {
                "[TOKEN_NAME]": draft.token_name,
                "[TOKEN_SYMBOL]": draft.token_symbol,
                "[METRIC]": metric_label(draft.metric),
                "[CONDITION]": direction_label(draft.direction),
                "[TARGET]": format_metric_value(draft.metric, draft.target_value),
                "[CURRENT]": format_metric_value(draft.metric, draft.current_value),
            },
        )

    async def handle_alert_callback(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        callback_id: str,
        data: str,
    ) -> None:
        if data == "alert:home":
            self.pending_alert_inputs.pop(user_id, None)
            self.pending_alert_choices.pop(user_id, None)
            await self.answer_callback(callback_id)
            await self.edit_message(chat_id, message_id, self.alert_home_text(user_id), self.alert_home_markup())
            return
        if data == "alert:create":
            self.pending_alert_inputs[user_id] = AlertDraft(step="token")
            self.pending_alert_choices.pop(user_id, None)
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_search_message,
                self.alert_cancel_markup(),
            )
            return
        if data == "alert:cancel":
            self.pending_alert_inputs.pop(user_id, None)
            self.pending_alert_choices.pop(user_id, None)
            await self.answer_callback(callback_id, "Alert setup cancelled.")
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_home_text(
                    user_id,
                    self.render_public_message("alert_cancelled_note"),
                ),
                self.alert_home_markup(),
            )
            return
        if data == "alert:help":
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_help_message,
                json.dumps(
                    {
                        "inline_keyboard": [
                            [self.public_button("alert_home", callback_data="alert:home")]
                        ]
                    }
                ),
            )
            return
        if data.startswith("alert:token:"):
            key = data.removeprefix("alert:token:")
            token_address = self.pending_alert_choices.get(user_id, {}).get(key)
            draft = self.pending_alert_inputs.get(user_id)
            if token_address is None or draft is None:
                await self.answer_callback(callback_id, "This token selection expired. Start again.")
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.alert_home_text(
                        user_id,
                        self.render_public_message("alert_expired_note"),
                    ),
                    self.alert_home_markup(),
                )
                return
            await self.answer_callback(callback_id, "Loading token...")
            try:
                pair = await self.token_report_service.best_pair_for_token(token_address)
            except Exception as exc:
                print(f"Alert token selection failed address={token_address}: {type(exc).__name__}: {exc}", flush=True)
                pair = None
            if pair is None:
                draft.step = "token"
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.render_public_message("alert_selection_unavailable_message"),
                    self.alert_cancel_markup(),
                )
                return
            await self.prepare_alert_pair(user_id, chat_id, pair, message_id=message_id)
            return
        if data.startswith("alert:metric:"):
            draft = self.pending_alert_inputs.get(user_id)
            metric = data.removeprefix("alert:metric:")
            if draft is None or draft.step != "metric" or metric not in {"price", "market_cap", "change_24h"}:
                await self.answer_callback(callback_id, "This setup expired. Start again.")
                return
            await self.answer_callback(callback_id, "Loading current value...")
            try:
                pair = await self.token_report_service.best_pair_for_token(draft.token_address)
            except Exception:
                pair = None
            if pair is None:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.render_public_message("alert_live_data_unavailable_message"),
                    self.alert_metric_markup(draft),
                )
                return
            current_value = metric_value(pair, metric)
            if current_value is None:
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.render_public_message(
                        "alert_metric_unavailable_message",
                        {"[METRIC]": metric_label(metric)},
                    ),
                    self.alert_metric_markup(draft),
                )
                return
            draft.set_pair(pair)
            draft.metric = metric
            draft.current_value = current_value
            draft.step = "direction"
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "alert_direction_message",
                    {
                        "[TOKEN_SYMBOL]": draft.token_symbol,
                        "[METRIC]": metric_label(metric),
                        "[CURRENT]": format_metric_value(metric, current_value),
                    },
                ),
                self.alert_direction_markup(),
            )
            return
        if data == "alert:back:metric":
            draft = self.pending_alert_inputs.get(user_id)
            if draft is None:
                await self.answer_callback(callback_id, "This setup expired.")
                return
            draft.step = "metric"
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "alert_metric_back_message",
                    {
                        "[TOKEN_NAME]": draft.token_name,
                        "[TOKEN_SYMBOL]": draft.token_symbol,
                    },
                ),
                self.alert_metric_markup(draft),
            )
            return
        if data == "alert:back:token":
            if user_id not in self.pending_alert_inputs:
                await self.answer_callback(callback_id, "This setup expired. Start again.")
                return
            self.pending_alert_inputs[user_id] = AlertDraft(step="token")
            self.pending_alert_choices.pop(user_id, None)
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_search_message,
                self.alert_cancel_markup(),
            )
            return
        if data == "alert:back:direction":
            draft = self.pending_alert_inputs.get(user_id)
            if draft is None or not draft.metric or draft.current_value is None:
                await self.answer_callback(callback_id, "This setup expired. Start again.")
                return
            draft.step = "direction"
            draft.direction = ""
            draft.target_value = None
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "alert_direction_message",
                    {
                        "[TOKEN_SYMBOL]": draft.token_symbol,
                        "[METRIC]": metric_label(draft.metric),
                        "[CURRENT]": format_metric_value(draft.metric, draft.current_value),
                    },
                ),
                self.alert_direction_markup(),
            )
            return
        if data.startswith("alert:direction:"):
            draft = self.pending_alert_inputs.get(user_id)
            direction = data.removeprefix("alert:direction:")
            if draft is None or draft.step != "direction" or direction not in {"above", "below"}:
                await self.answer_callback(callback_id, "This setup expired. Start again.")
                return
            draft.direction = direction
            draft.step = "target"
            await self.answer_callback(callback_id)
            example = "-5 or 10%" if draft.metric == "change_24h" else (
                "1.5M" if draft.metric == "market_cap" else "0.025"
            )
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "alert_target_message",
                    {
                        "[TOKEN_SYMBOL]": draft.token_symbol,
                        "[METRIC]": metric_label(draft.metric),
                        "[CONDITION]": direction_label(direction),
                        "[CURRENT]": format_metric_value(draft.metric, draft.current_value),
                        "[EXAMPLE]": example,
                    },
                ),
                self.alert_target_markup(),
            )
            return
        if data == "alert:back:target":
            draft = self.pending_alert_inputs.get(user_id)
            if draft is None:
                await self.answer_callback(callback_id, "This setup expired.")
                return
            draft.step = "target"
            draft.target_value = None
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message("alert_new_target_message"),
                self.alert_target_markup(),
            )
            return
        if data == "alert:confirm":
            draft = self.pending_alert_inputs.get(user_id)
            if draft is None or draft.step != "confirm":
                await self.answer_callback(callback_id, "This setup expired. Start again.")
                return
            try:
                async with self.alert_store_lock:
                    alert = self.alert_store.create(user_id, draft)
                    self.alert_store.save()
            except ValueError as exc:
                await self.answer_callback(callback_id, str(exc))
                return
            self.pending_alert_inputs.pop(user_id, None)
            self.pending_alert_choices.pop(user_id, None)
            await self.answer_callback(callback_id, "Alert saved.")
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_detail_text(
                    alert,
                    note=self.render_public_message("alert_saved_note"),
                ),
                self.alert_detail_markup(alert),
            )
            return
        if data.startswith("alert:list:"):
            try:
                page = max(0, int(data.removeprefix("alert:list:")))
            except ValueError:
                page = 0
            await self.answer_callback(callback_id)
            text, markup = self.alert_list_page(user_id, page)
            await self.edit_message(chat_id, message_id, text, markup)
            return
        if data.startswith("alert:view:"):
            alert_id = data.removeprefix("alert:view:")
            alert = self.alert_store.get_for_user(user_id, alert_id)
            if alert is None:
                await self.answer_callback(callback_id, "Alert not found.")
                return
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_detail_text(alert),
                self.alert_detail_markup(alert),
            )
            return
        if data.startswith("alert:toggle:"):
            alert_id = data.removeprefix("alert:toggle:")
            try:
                async with self.alert_store_lock:
                    alert = self.alert_store.toggle(user_id, alert_id)
                    if alert is not None:
                        self.alert_store.save()
            except ValueError as exc:
                await self.answer_callback(callback_id, str(exc))
                return
            if alert is None:
                await self.answer_callback(callback_id, "Alert not found.")
                return
            await self.answer_callback(callback_id, "Alert resumed." if alert.active else "Alert paused.")
            await self.edit_message(
                chat_id,
                message_id,
                self.alert_detail_text(alert),
                self.alert_detail_markup(alert),
            )
            return
        if data.startswith("alert:delete:"):
            alert_id = data.removeprefix("alert:delete:")
            alert = self.alert_store.get_for_user(user_id, alert_id)
            if alert is None:
                await self.answer_callback(callback_id, "Alert not found.")
                return
            await self.answer_callback(callback_id)
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "alert_delete_confirmation_message",
                    {
                        "[TOKEN_SYMBOL]": alert.token_symbol,
                        "[METRIC]": metric_label(alert.metric),
                        "[TARGET]": format_metric_value(alert.metric, alert.target_value),
                    },
                ),
                json.dumps(
                    {
                        "inline_keyboard": [
                            [
                                self.public_button(
                                    "alert_delete",
                                    callback_data=f"alert:deleteok:{alert.alert_id}",
                                )
                            ],
                            [
                                self.public_button(
                                    "alert_keep",
                                    callback_data=f"alert:view:{alert.alert_id}",
                                )
                            ],
                        ]
                    }
                ),
            )
            return
        if data.startswith("alert:deleteok:"):
            alert_id = data.removeprefix("alert:deleteok:")
            async with self.alert_store_lock:
                deleted = self.alert_store.delete(user_id, alert_id)
                if deleted:
                    self.alert_store.save()
            await self.answer_callback(callback_id, "Alert deleted." if deleted else "Alert not found.")
            text, markup = self.alert_list_page(
                user_id,
                0,
                note=self.render_public_message("alert_deleted_note") if deleted else "",
            )
            await self.edit_message(chat_id, message_id, text, markup)
            return
        await self.answer_callback(callback_id, "Unknown alert action.")

    def alert_list_page(self, user_id: int, page: int, note: str = "") -> tuple[str, str]:
        alerts = self.alert_store.for_user(user_id)
        per_page = 5
        max_page = max(0, (len(alerts) - 1) // per_page)
        page = min(max(0, page), max_page)
        current = alerts[page * per_page : (page + 1) * per_page]
        if not alerts:
            text = self.render_public_message("alert_list_empty_message")
        else:
            text = self.render_public_message(
                "alert_list_message",
                {
                    "[PAGE]": page + 1,
                    "[TOTAL_PAGES]": max_page + 1,
                    "[TOTAL]": len(alerts),
                },
            )
        text = self.append_alert_note(text, note)
        rows: list[list[dict[str, str]]] = []
        for alert in current:
            status = "🟢" if alert.active else ("🔔" if alert.triggered_at else "⏸️")
            button = self.public_button(
                "alert_item",
                replacements={
                    "[STATUS]": status,
                    "[TOKEN_SYMBOL]": alert.token_symbol,
                    "[METRIC]": metric_label(alert.metric),
                    "[TARGET]": format_metric_value(alert.metric, alert.target_value),
                },
                callback_data=f"alert:view:{alert.alert_id}",
            )
            button["text"] = button["text"][:60]
            rows.append([button])
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(
                self.public_button("alert_previous", callback_data=f"alert:list:{page - 1}")
            )
        if page < max_page:
            navigation.append(self.public_button("alert_next", callback_data=f"alert:list:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([self.public_button("alert_create", callback_data="alert:create")])
        rows.append([self.public_button("alert_home", callback_data="alert:home")])
        return text, json.dumps({"inline_keyboard": rows})

    def alert_detail_text(self, alert: UserAlert, note: str = "") -> str:
        status = "🟢 Active" if alert.active else ("🔔 Triggered and paused" if alert.triggered_at else "⏸️ Paused")
        checked = self._format_alert_timestamp(alert.last_checked_at)
        error_line = (
            self.render_public_message(
                "alert_issue_message",
                {"[ERROR]": alert.last_error},
            )
            if alert.last_error
            else ""
        )
        text = self.render_public_message(
            "alert_detail_message",
            {
                "[TOKEN_NAME]": alert.token_name,
                "[TOKEN_SYMBOL]": alert.token_symbol,
                "[STATUS]": status,
                "[METRIC]": metric_label(alert.metric),
                "[CONDITION]": direction_label(alert.direction),
                "[TARGET]": format_metric_value(alert.metric, alert.target_value),
                "[LATEST]": format_metric_value(alert.metric, alert.last_value),
                "[LAST_CHECKED]": checked,
            },
        ) + error_line
        return self.append_alert_note(text, note)

    def alert_note_text(self, note: str = "") -> str:
        if not note:
            return ""
        return self.render_public_message(
            "alert_note_message",
            {"[NOTE]": note},
            trusted_placeholders={"[NOTE]"},
        )

    def append_alert_note(self, text: str, note: str = "") -> str:
        note_text = self.alert_note_text(note).strip()
        if not note_text:
            return text.rstrip()
        return f"{text.rstrip()}\n\n{note_text}"

    @staticmethod
    def _format_alert_timestamp(raw_value: str) -> str:
        if not raw_value:
            return "Waiting for first check"
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return "Unknown"
        return parsed.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    def alert_detail_markup(self, alert: UserAlert) -> str:
        toggle_key = "alert_pause" if alert.active else "alert_rearm"
        rows: list[list[dict[str, str]]] = [
            [self.public_button(toggle_key, callback_data=f"alert:toggle:{alert.alert_id}")],
        ]
        if alert.pair_url.startswith(("https://", "http://")):
            rows.append([self.public_button("alert_open_chart", url=alert.pair_url)])
        rows.extend(
            [
                [self.public_button("alert_delete", callback_data=f"alert:delete:{alert.alert_id}")],
                [self.public_button("alert_home", callback_data="alert:list:0")],
            ]
        )
        return json.dumps({"inline_keyboard": rows})

    def render_public_message(
        self,
        key: str,
        replacements: dict[str, Any] | None = None,
        *,
        trusted_placeholders: set[str] | None = None,
    ) -> str:
        default = self.PUBLIC_MESSAGE_DEFAULTS[key]
        template = str(getattr(self, key, default) or default)
        trusted = trusted_placeholders or set()
        for placeholder, value in (replacements or {}).items():
            replacement = str(value) if placeholder in trusted else html.escape(str(value))
            template = template.replace(placeholder, replacement)
        return template

    def public_button(
        self,
        key: str,
        *,
        replacements: dict[str, Any] | None = None,
        **action: str,
    ) -> dict[str, str]:
        default = self.PUBLIC_BUTTON_DEFAULTS[key]
        templates = getattr(self, "public_button_texts", {})
        template = str(templates.get(key, default) or default)
        for placeholder, value in (replacements or {}).items():
            template = template.replace(placeholder, str(value))

        button = {"text": template, **action}
        icon_id = str(getattr(self, "public_button_icons", {}).get(key, "") or "").strip()
        if icon_id:
            button["icon_custom_emoji_id"] = icon_id
        return button

    def format_trending_message(self, snapshot: TrendingSnapshot) -> str:
        rows: list[str] = []
        eligible_coins = [
            coin
            for coin in snapshot.coins
            if coin.liquidity_usd >= self.trending_service.minimum_liquidity_usd
            and not self.trending_service.is_blocked(coin)
        ][: self.trending_service.result_limit]
        for rank, coin in enumerate(eligible_coins, start=1):
            row = self.trending_row
            wrapper_pattern = re.compile(
                r"<(?P<tag>b|strong|i|em|u|ins|s|strike|del|code)>\s*"
                r"\[CHANGE_24H\]\s*</(?P=tag)>",
                flags=re.IGNORECASE,
            )
            while True:
                normalized_row = wrapper_pattern.sub("[CHANGE_24H]", row)
                if normalized_row == row:
                    break
                row = normalized_row
            ticker = html.escape(coin.symbol.upper())
            chart_url = html.escape(coin.chart_url, quote=True)
            ticker_link = f'<a href="{chart_url}">{ticker}</a>'
            ticker_with_prefix_link = f'<a href="{chart_url}">${ticker}</a>'
            row = row.replace("$[TICKER]", ticker_with_prefix_link)
            replacements = {
                "[RANK]": str(rank),
                "[TICKER]": ticker_link,
                "[NAME]": html.escape(coin.name),
                "[CHANGE_24H]": f"<code>{coin.change_24h:+.2f}%</code>",
            }
            for placeholder, value in replacements.items():
                row = row.replace(placeholder, value)
            rows.append(row)

        updated_at = snapshot.updated_at.strftime("%d/%m/%Y %H:%M UTC")
        return (
            self.trending_message.replace("[TITLE]", self.trending_title)
            .replace("[LIST]", "\n".join(rows))
            .replace("[UPDATED_AT]", updated_at)
        )

    def format_new_tokens_message(
        self,
        snapshot: NewTokensSnapshot,
        tokens: tuple[NewToken, ...],
        *,
        now: datetime | None = None,
    ) -> str:
        current_time = now or datetime.now(timezone.utc)
        rows: list[str] = []
        for rank, token in enumerate(tokens, start=1):
            valuation = token.valuation_usd
            chart_url = html.escape(token.chart_url, quote=True)
            safe_name = html.escape(token.name)
            name_is_already_linked = bool(
                re.search(r'<a\b[^>]*>[^<]*\[NAME\]', self.new_row, re.IGNORECASE)
            )
            linked_name = (
                safe_name
                if name_is_already_linked
                else f'<a href="{chart_url}">{safe_name}</a>'
            )
            replacements = {
                "[RANK]": str(rank),
                "[TICKER]": html.escape(token.symbol.upper()),
                "[NAME]": linked_name,
                "[AGE]": html.escape(self.format_new_token_age(current_time - token.created_at)),
                "[VALUATION_LABEL]": token.valuation_label,
                "[MARKET_CAP]": self.format_new_usd(valuation),
                "[LIQUIDITY]": self.format_new_usd(token.liquidity_usd),
                "[HOLDERS]": f"{token.holders:,}" if token.holders is not None else "—",
                "[VOLUME_24H]": self.format_new_usd(token.volume_24h_usd),
                "[CONTRACT]": html.escape(token.token_address),
                "[DEX]": html.escape(token.dex_name),
                "[CHART_URL]": chart_url,
            }
            row = self.new_row
            for placeholder, value in replacements.items():
                row = row.replace(placeholder, value)
            rows.append(row)

        return self.render_public_message(
            "new_message",
            {
                "[LIST]": "\n\n".join(rows),
                "[UPDATED_AT]": snapshot.updated_at.strftime("%d/%m/%Y %H:%M UTC"),
            },
            trusted_placeholders={"[LIST]"},
        )

    @staticmethod
    def format_new_usd(value: float | None) -> str:
        return "—" if value is None else f"${format_compact_number(value)}"

    @staticmethod
    def format_new_token_age(value: timedelta, *, compact_limit: bool = False) -> str:
        total_seconds = max(0, int(value.total_seconds()))
        if total_seconds < 60:
            return "<1m"
        total_minutes = total_seconds // 60
        if total_minutes < 60:
            return f"{total_minutes}m"
        hours, minutes = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)
        if days:
            parts = [f"{days}d"]
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            return " ".join(parts)
        if not minutes:
            return f"{hours}h"
        return f"{hours}h {minutes}m"

    async def send_token_report(self, chat_id: int, query: str) -> None:
        started = time.monotonic()
        async with self.token_report_semaphore:
            try:
                pair, result = await self.resolve_token_pair_for_query(query)
            except Exception as exc:
                print(f"Token report failed query={query!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(chat_id, self.token_report_unavailable_message)
                return

        if result is not None and result.has_choices and result.choices:
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "token_choices_message",
                    {"[QUERY]": result.query},
                ),
                reply_markup=self.token_choices_markup(result.choices),
            )
        elif pair is not None:
            await self.send_token_report_photo(chat_id, pair)
        else:
            await self.send_message(
                chat_id,
                self.render_public_message("token_not_found_message", {"[QUERY]": query}),
            )

        elapsed = time.monotonic() - started
        print(f"/meme report served chat={chat_id} query={query!r} mode=image in {elapsed:.2f}s", flush=True)

    async def send_ath(self, chat_id: int, query: str) -> None:
        query = str(query or "").strip()
        if not query:
            await self.send_message(chat_id, self.ath_usage_message)
            return

        started = time.monotonic()
        if normalize_query(query).casefold() in self.NATIVE_TON_ATH_QUERIES:
            async with self.token_report_semaphore:
                ath = await self.token_report_service.fetch_coin_ath("the-open-network")
            await self.send_message(chat_id, self.ath_text("GRAM", ath))
            elapsed = time.monotonic() - started
            print(f"/ath served chat={chat_id} query={query!r} native=GRAM in {elapsed:.2f}s", flush=True)
            return

        async with self.token_report_semaphore:
            try:
                pair, result = await self.resolve_token_pair_for_query(query)
            except Exception as exc:
                print(f"ATH lookup failed query={query!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(chat_id, self.ath_lookup_unavailable_message)
                return

        if result is not None and result.has_choices and result.choices:
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "token_choices_message",
                    {"[QUERY]": result.query},
                ),
                reply_markup=self.ath_choices_markup(result.choices),
            )
        elif pair is not None:
            await self.send_message(chat_id, await self.ath_text_for_pair(pair))
        else:
            await self.send_message(
                chat_id,
                self.render_public_message("token_not_found_message", {"[QUERY]": query}),
            )

        elapsed = time.monotonic() - started
        print(f"/ath served chat={chat_id} query={query!r} in {elapsed:.2f}s", flush=True)

    async def edit_ath(self, chat_id: int, message_id: int, token_address: str) -> None:
        async with self.token_report_semaphore:
            try:
                pair = await self.token_report_service.best_pair_for_token(token_address)
                text = (
                    await self.ath_text_for_pair(pair)
                    if pair is not None
                    else self.render_public_message(
                        "token_not_found_message",
                        {"[QUERY]": token_address},
                    )
                )
            except Exception as exc:
                print(
                    f"ATH callback failed address={token_address!r}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                text = self.ath_lookup_unavailable_message
        await self.edit_message(chat_id, message_id, text)

    async def ath_text_for_pair(self, pair: TokenPair) -> str:
        ath = await self.token_report_service.fetch_ath(pair.token_address)
        return self.ath_text(pair.symbol, ath)

    def ath_text(self, symbol: str, ath: TokenAth | None) -> str:
        replacements = {"[TOKEN_SYMBOL]": symbol.upper()}
        if ath is None:
            return self.render_public_message("ath_unavailable_message", replacements)
        replacements["[ATH_PRICE]"] = format_price(ath.price_usd)
        return self.render_public_message("ath_result_message", replacements)

    async def send_conversion(self, chat_id: int, user_id: int, raw_args: str) -> None:
        try:
            query, gram_amount = parse_conversion_request(raw_args)
        except ValueError as exc:
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "conversion_usage_message",
                    {"[ERROR]": str(exc)},
                ),
            )
            return

        started = time.monotonic()
        if query is None:
            try:
                await self.send_gram_usd_conversion_photo(chat_id, gram_amount)
            except ValueError as exc:
                await self.send_message(
                    chat_id,
                    self.render_public_message(
                        "conversion_unavailable_message",
                        {"[ERROR]": str(exc)},
                    ),
                )
                return
            except Exception as exc:
                print(f"GRAM/USD conversion failed: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(
                    chat_id,
                    self.render_public_message(
                        "conversion_unavailable_message",
                        {"[ERROR]": "I could not create the conversion image right now. Try again in a few seconds."},
                    ),
                )
                return
            elapsed = time.monotonic() - started
            print(f"/swap served chat={chat_id} query='USD' gram={gram_amount} in {elapsed:.2f}s", flush=True)
            return

        async with self.token_report_semaphore:
            try:
                pair, result = await self.resolve_token_pair_for_query(query)
            except Exception as exc:
                print(f"Conversion lookup failed query={query!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.send_message(
                    chat_id,
                    self.render_public_message(
                        "conversion_unavailable_message",
                        {"[ERROR]": "I could not load this token's live market price. Try again in a few seconds."},
                    ),
                )
                return

        if result is not None and result.has_choices and result.choices:
            self.pending_conversion_choices[user_id] = (gram_amount, time.monotonic() + 300)
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "token_choices_message",
                    {"[QUERY]": result.query},
                ),
                reply_markup=self.conversion_choices_markup(result.choices),
            )
            return
        if pair is None:
            await self.send_message(chat_id, self.conversion_not_found_text(query))
            return

        try:
            await self.send_conversion_photo(chat_id, pair, gram_amount)
        except ValueError as exc:
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "conversion_unavailable_message",
                    {"[ERROR]": str(exc)},
                ),
            )
            return
        except Exception as exc:
            print(f"Conversion failed query={query!r}: {type(exc).__name__}: {exc}", flush=True)
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "conversion_unavailable_message",
                    {"[ERROR]": "I could not create the conversion image right now. Try again in a few seconds."},
                ),
            )
            return

        elapsed = time.monotonic() - started
        print(
            f"/swap served chat={chat_id} query={query!r} gram={gram_amount} in {elapsed:.2f}s",
            flush=True,
        )

    async def send_conversion_photo(
        self,
        chat_id: int,
        pair: TokenPair,
        gram_amount: Decimal,
    ) -> None:
        async with self.conversion_semaphore:
            gram_price, gram_change_24h = await self.fetch_gram_value()
            if gram_price is None:
                raise ValueError("The live GRAM price is unavailable. No estimate was sent.")
            if pair.price_usd is None:
                raise ValueError("The live token price is unavailable. No estimate was sent.")

            token_amount = calculate_token_amount(gram_amount, gram_price, pair.price_usd)
            cache_key = self.conversion_cache_key(
                pair,
                gram_amount,
                gram_price,
                gram_change_24h,
            )
            now = time.monotonic()
            self.prune_conversion_caches(now)
            cached = self.conversion_image_cache.get(cache_key)
            if cached and cached[0] > now:
                image_bytes, image_hash = cached[1], cached[2]
            else:
                card = ConversionCardData(
                    gram_amount=gram_amount,
                    token_symbol=pair.symbol,
                    token_amount=token_amount,
                    gram_price_usd=gram_price,
                    gram_change_24h=gram_change_24h,
                )
                image = await asyncio.to_thread(self.conversion_renderer.render, card)
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=92, subsampling=0, optimize=True)
                image_bytes = buffer.getvalue()
                image_hash = hashlib.sha256(image_bytes).hexdigest()
                self.conversion_image_cache[cache_key] = (
                    time.monotonic() + self.conversion_image_cache_seconds,
                    image_bytes,
                    image_hash,
                )

            cached_photo = self.conversion_photo_file_ids.get(cache_key)
            if cached_photo and cached_photo[1] == image_hash:
                await self.send_photo_file_id(chat_id, cached_photo[0])
                return

            lock_index = int(image_hash[:8], 16) % len(self.conversion_photo_upload_locks)
            async with self.conversion_photo_upload_locks[lock_index]:
                cached_photo = self.conversion_photo_file_ids.get(cache_key)
                if cached_photo and cached_photo[1] == image_hash:
                    await self.send_photo_file_id(chat_id, cached_photo[0])
                    return
                file_id = await self.send_photo(chat_id, BytesIO(image_bytes))
                if file_id:
                    self.conversion_photo_file_ids[cache_key] = (file_id, image_hash)

    async def send_gram_usd_conversion_photo(
        self,
        chat_id: int,
        gram_amount: Decimal,
    ) -> None:
        async with self.conversion_semaphore:
            gram_price, gram_change_24h = await self.fetch_gram_value()
            if gram_price is None or gram_price <= 0:
                raise ValueError("The live GRAM price is unavailable. No estimate was sent.")
            with localcontext() as context:
                context.prec = 40
                usd_amount = gram_amount * Decimal(str(gram_price))

            cache_key = ":".join(
                (
                    "GRAM-USD",
                    format(gram_amount.normalize(), "f"),
                    format(gram_price, ".16g"),
                    "none" if gram_change_24h is None else format(gram_change_24h, ".16g"),
                )
            )
            now = time.monotonic()
            self.prune_conversion_caches(now)
            cached = self.conversion_image_cache.get(cache_key)
            if cached and cached[0] > now:
                image_bytes, image_hash = cached[1], cached[2]
            else:
                card = ConversionCardData(
                    gram_amount=gram_amount,
                    token_symbol="USD",
                    token_amount=usd_amount,
                    gram_price_usd=gram_price,
                    gram_change_24h=gram_change_24h,
                )
                image = await asyncio.to_thread(self.conversion_renderer.render, card)
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=92, subsampling=0, optimize=True)
                image_bytes = buffer.getvalue()
                image_hash = hashlib.sha256(image_bytes).hexdigest()
                self.conversion_image_cache[cache_key] = (
                    time.monotonic() + self.conversion_image_cache_seconds,
                    image_bytes,
                    image_hash,
                )

            cached_photo = self.conversion_photo_file_ids.get(cache_key)
            if cached_photo and cached_photo[1] == image_hash:
                await self.send_photo_file_id(chat_id, cached_photo[0])
                return

            lock_index = int(image_hash[:8], 16) % len(self.conversion_photo_upload_locks)
            async with self.conversion_photo_upload_locks[lock_index]:
                cached_photo = self.conversion_photo_file_ids.get(cache_key)
                if cached_photo and cached_photo[1] == image_hash:
                    await self.send_photo_file_id(chat_id, cached_photo[0])
                    return
                file_id = await self.send_photo(chat_id, BytesIO(image_bytes))
                if file_id:
                    self.conversion_photo_file_ids[cache_key] = (file_id, image_hash)

    def prune_conversion_caches(self, now: float) -> None:
        expired = [key for key, item in self.conversion_image_cache.items() if item[0] <= now]
        for key in expired:
            self.conversion_image_cache.pop(key, None)
            self.conversion_photo_file_ids.pop(key, None)

        overflow = len(self.conversion_image_cache) - self.conversion_cache_entries
        if overflow <= 0:
            return
        oldest = sorted(self.conversion_image_cache, key=lambda key: self.conversion_image_cache[key][0])
        for key in oldest[:overflow]:
            self.conversion_image_cache.pop(key, None)
            self.conversion_photo_file_ids.pop(key, None)

    def conversion_not_found_text(self, query: str) -> str:
        return self.render_public_message(
            "conversion_not_found_message",
            {"[QUERY]": str(query).strip()},
        )

    @staticmethod
    def conversion_cache_key(
        pair: TokenPair,
        gram_amount: Decimal,
        gram_price: float,
        gram_change_24h: float | None,
    ) -> str:
        return ":".join(
            (
                pair.token_address,
                format(gram_amount.normalize(), "f"),
                format(gram_price, ".16g"),
                "none" if gram_change_24h is None else format(gram_change_24h, ".16g"),
                format(pair.price_usd or 0.0, ".16g"),
            )
        )

    async def edit_token_report(self, chat_id: int, message_id: int, token_address: str) -> None:
        async with self.token_report_semaphore:
            try:
                pair = await self.token_report_service.best_pair_for_token(token_address)
            except Exception as exc:
                print(f"Token callback report failed address={token_address!r}: {type(exc).__name__}: {exc}", flush=True)
                await self.edit_message(
                    chat_id,
                    message_id,
                    self.token_report_unavailable_message,
                )
                return
        if pair is None:
            await self.edit_message(
                chat_id,
                message_id,
                self.render_public_message(
                    "token_not_found_message",
                    {"[QUERY]": token_address},
                ),
            )
            return
        await self.send_token_report_photo(chat_id, pair)

    async def resolve_token_pair_for_query(
        self,
        raw_query: str,
    ) -> tuple[TokenPair | None, TokenReportResult | None]:
        query = normalize_query(raw_query)
        if not query:
            return None, TokenReportResult(text=TokenReportService.not_found_text(raw_query), query=raw_query)

        known_address = KNOWN_TOKEN_ADDRESSES.get(query.lower())
        if known_address:
            pair = await self.token_report_service.best_pair_for_token(known_address)
            if pair is None:
                return None, TokenReportResult(text=TokenReportService.not_found_text(raw_query), query=raw_query)
            return pair, None

        pairs = await self.token_report_service.search_pairs(query)
        if not pairs:
            return None, TokenReportResult(text=TokenReportService.not_found_text(raw_query), query=raw_query)

        selected = select_best_pair(query, pairs)
        if isinstance(selected, list):
            return None, TokenReportResult(
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

        fresh_pair = await self.token_report_service.best_pair_for_token(selected.token_address)
        return fresh_pair or selected, None

    async def send_token_report_photo(self, chat_id: int, pair: TokenPair) -> None:
        token_key = pair.token_address
        image_bytes, image_hash = await self.token_report_image_bytes(pair)
        file_id = self.current_token_photo_file_id(token_key, image_hash)
        if file_id:
            await self.send_photo_file_id(chat_id, file_id)
            return

        async with self.token_photo_upload_lock:
            file_id = self.current_token_photo_file_id(token_key, image_hash)
            if file_id:
                await self.send_photo_file_id(chat_id, file_id)
                return
            uploaded_file_id = await self.send_photo(chat_id, BytesIO(image_bytes))
            self.remember_token_photo_file_id(token_key, image_hash, uploaded_file_id)

    async def token_report_image_bytes(self, pair: TokenPair) -> tuple[bytes, str]:
        token_key = pair.token_address
        now = time.monotonic()
        cached = self.token_image_cache.get(token_key)
        if cached and cached[0] > now:
            return cached[1], cached[2]

        existing = self.token_image_inflight.get(token_key)
        if existing:
            return await existing

        return await self.refresh_token_report_image(pair)

    async def refresh_token_report_image(self, pair: TokenPair) -> tuple[bytes, str]:
        token_key = pair.token_address
        existing = self.token_image_inflight.get(token_key)
        if existing:
            return await existing

        task = asyncio.create_task(self.render_token_report_image(pair))
        self.token_image_inflight[token_key] = task
        try:
            image_bytes, image_hash = await task
        finally:
            if self.token_image_inflight.get(token_key) is task:
                self.token_image_inflight.pop(token_key, None)
        self.token_image_cache[token_key] = (time.monotonic() + self.token_image_cache_seconds, image_bytes, image_hash)
        return image_bytes, image_hash

    async def render_token_report_image(self, pair: TokenPair) -> tuple[bytes, str]:
        history, ath, logo_bytes = await asyncio.gather(
            self.token_report_service.fetch_history(pair.pair_address),
            self.token_report_service.fetch_ath(pair.token_address),
            self.token_logo_bytes(pair),
        )
        data = TokenCardData(
            name=pair.name.title() if pair.name.islower() else pair.name,
            symbol=pair.symbol,
            price=pair.price_usd,
            change_24h=pair.price_change.get("h24"),
            ath_price=ath.price_usd if ath else None,
            chart_points=history.chart_points,
            logo_bytes=logo_bytes,
        )
        image = await asyncio.to_thread(self.token_card_renderer.render, data)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True)
        image_bytes = buffer.getvalue()
        return image_bytes, hashlib.sha256(image_bytes).hexdigest()

    async def token_logo_bytes(self, pair: TokenPair) -> bytes | None:
        for slug, known_address in KNOWN_TOKEN_ADDRESSES.items():
            if not same_ton_address(pair.token_address, known_address):
                continue
            local_path = LOGO_DIR / f"{slug}.png"
            if local_path.is_file():
                try:
                    return await asyncio.to_thread(local_path.read_bytes)
                except OSError:
                    break
        return await self.token_report_service.fetch_logo(pair.image_url)

    async def fetch_gram_value(self) -> tuple[float | None, float | None]:
        now = time.monotonic()
        if self.gram_value_cache and self.gram_value_cache[0] > now:
            return self.gram_value_cache[1], self.gram_value_cache[2]
        try:
            values = await self.price_service.fetch_prices()
        except Exception as exc:
            print(f"GRAM price load failed for token report: {type(exc).__name__}: {exc}", flush=True)
            return None, None
        for value in values:
            if value.ticker.upper() == "GRAM":
                if value.price is not None and value.price > 0:
                    self.gram_value_cache = (time.monotonic() + 60, value.price, value.change_24h)
                return value.price, value.change_24h
        return None, None

    def current_token_photo_file_id(self, token_key: str, image_hash: str) -> str | None:
        cached = self.token_photo_file_ids.get(token_key)
        if not cached:
            return None
        file_id, cached_hash = cached
        return file_id if cached_hash == image_hash else None

    def remember_token_photo_file_id(self, token_key: str, image_hash: str, file_id: str | None) -> None:
        if file_id:
            self.token_photo_file_ids[token_key] = (file_id, image_hash)

    async def ensure_subscribed(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        if chat_type != "private" or self.authorized(user_id):
            return True

        if not self.require_private_subscription:
            self.remember_subscriber(user_id)
            return True

        if user_id > 0 and await self.is_channel_member(user_id):
            self.remember_subscriber(user_id)
            return True

        await self.send_subscription_required(chat_id, private=True)
        return False

    async def is_channel_member(self, user_id: int, *, force: bool = False) -> bool:
        if user_id <= 0:
            return False

        now = time.monotonic()
        cached = self.membership_cache.get(user_id)
        if not force and cached and cached[0] and cached[1] > now:
            return True

        try:
            result = await self.api(
                "getChatMember",
                {
                    "chat_id": self.required_channel,
                    "user_id": str(user_id),
                },
                timeout=8,
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("description") or "membership lookup failed"))

            member = result.get("result") or {}
            status = str(member.get("status") or "")
            allowed = status in {"creator", "administrator", "member"} or (
                status == "restricted" and bool(member.get("is_member"))
            )
            if allowed:
                self.membership_cache[user_id] = (True, now + self.membership_cache_seconds)
            else:
                self.membership_cache.pop(user_id, None)
            return allowed
        except Exception as exc:
            # A stale positive result avoids blocking existing members during a
            # temporary Telegram API failure. Unknown users remain blocked.
            if cached and cached[0]:
                return True
            print(f"Membership check failed for user={user_id}: {type(exc).__name__}: {exc}", flush=True)
            return False

    async def send_subscription_required(self, chat_id: int, *, private: bool) -> None:
        channel_label = self.required_channel if self.required_channel.startswith("@") else "required channel"
        buttons: list[list[dict[str, str]]] = [
            [
                self.public_button(
                    "subscription_join",
                    replacements={"[CHANNEL]": channel_label},
                    url=self.required_channel_url(),
                )
            ],
        ]
        if private:
            buttons.append([self.public_button("subscription_check", callback_data="check_subscription")])
        await self.send_message(
            chat_id,
            self.subscription_message,
            reply_markup=json.dumps({"inline_keyboard": buttons}),
        )

    def required_channel_url(self) -> str:
        channel = self.required_channel.strip()
        if channel.startswith("@"):
            return f"https://t.me/{channel[1:]}"
        if channel.startswith("https://") or channel.startswith("http://"):
            return channel
        return f"https://t.me/{channel.removeprefix('t.me/').lstrip('/')}"

    def current_photo_file_id(self) -> str | None:
        if not self.cached_image_hash:
            return None
        if not self.cached_photo_file_id or self.cached_photo_hash != self.cached_image_hash:
            self.load_cached_photo_file_id()
        if self.cached_photo_file_id and self.cached_photo_hash == self.cached_image_hash:
            return self.cached_photo_file_id
        return None

    async def ensure_warm_image(self) -> None:
        if OUTPUT_PATH.exists() and OUTPUT_PATH.stat().st_size > 0:
            self.set_cached_image_bytes(OUTPUT_PATH.read_bytes())
            self.load_cached_photo_file_id()
            return
        await self.refresh_image_cache(force=True)

    async def fast_image_buffer(self) -> BytesIO:
        if self.cached_image_bytes is None and OUTPUT_PATH.exists():
            self.set_cached_image_bytes(OUTPUT_PATH.read_bytes())
            self.load_cached_photo_file_id()
        if self.cached_image_bytes is not None:
            return BytesIO(self.cached_image_bytes)
        return await self.render_image_buffer()

    async def refresh_image_cache(self, *, force: bool = False) -> bytes:
        if not force:
            now = time.monotonic()
            if self.cached_image_bytes is not None and now - self.cached_image_at <= self.image_cache_seconds:
                return self.cached_image_bytes

        async with self.render_lock:
            if not force:
                now = time.monotonic()
                if self.cached_image_bytes is not None and now - self.cached_image_at <= self.image_cache_seconds:
                    return self.cached_image_bytes

            values, holder_counts, chart_series = await asyncio.gather(
                self.price_service.fetch_prices(),
                self.holder_service.fetch_counts([coin.ticker for coin in COINS]),
                self.chart_service.fetch_series([coin.ticker for coin in COINS]),
            )
            values = [
                replace(
                    value,
                    holders=(holder_counts.get(value.ticker.upper()).count
                             if holder_counts.get(value.ticker.upper()) is not None
                             else value.holders),
                    chart_points=(chart_series.get(value.ticker.upper()).points
                                  if chart_series.get(value.ticker.upper()) is not None
                                  else value.chart_points),
                )
                for value in values
            ]
            try:
                await asyncio.to_thread(self.pulse_service.record, values)
            except Exception as exc:
                print(f"Pulse snapshot save failed: {type(exc).__name__}: {exc}", flush=True)
            await self.process_utya_movement_alert(values)
            new_image_bytes = await asyncio.to_thread(self.render_image_bytes_sync, values)
            old_hash = self.cached_image_hash
            self.set_cached_image_bytes(new_image_bytes)
            if self.cached_image_hash != old_hash:
                self.cached_photo_file_id = None
                self.cached_photo_hash = None
                self.load_cached_photo_file_id()
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_bytes(self.cached_image_bytes)
            return self.cached_image_bytes

    def set_cached_image_bytes(self, image_bytes: bytes) -> None:
        self.cached_image_bytes = image_bytes
        self.cached_image_hash = hashlib.sha256(image_bytes).hexdigest()
        self.cached_image_at = time.monotonic()

    def load_cached_photo_file_id(self) -> None:
        if not self.cached_image_hash:
            return
        try:
            payload = json.loads(self.photo_cache_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("image_sha256") != self.cached_image_hash:
            return
        file_id = payload.get("file_id")
        if not file_id:
            return
        self.cached_photo_file_id = str(file_id)
        self.cached_photo_hash = self.cached_image_hash

    def remember_photo_file_id(self, file_id: str | None) -> None:
        if not file_id or not self.cached_image_hash:
            return
        self.cached_photo_file_id = file_id
        self.cached_photo_hash = self.cached_image_hash
        try:
            self.photo_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.photo_cache_path.write_text(
                json.dumps(
                    {
                        "image_sha256": self.cached_image_hash,
                        "file_id": file_id,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"Telegram photo cache save failed: {type(exc).__name__}: {exc}", flush=True)

    def render_image_bytes_sync(self, values) -> bytes:
        image = self.renderer.render(values)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=92, subsampling=0, optimize=True)
        return buffer.getvalue()

    async def render_image_buffer(self) -> BytesIO:
        return BytesIO(await self.refresh_image_cache())

    async def render_image_bytes(self) -> bytes:
        return await self.refresh_image_cache()

    async def send_photo(
        self,
        chat_id: str | int,
        image: BytesIO,
        caption: str = "",
        reply_markup: str | None = None,
    ) -> str | None:
        last_error: Exception | None = None
        for attempt in range(3):
            image.seek(0)
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            if caption:
                form.add_field("caption", caption)
            if reply_markup:
                form.add_field("reply_markup", reply_markup)
            form.add_field("photo", image, filename="dashboard.jpg", content_type="image/jpeg")
            try:
                result = await self.api_form("sendPhoto", form, timeout=30)
            except Exception as exc:
                last_error = exc
                print(f"sendPhoto attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", flush=True)
                if attempt < 2:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise
            if result.get("ok"):
                return self.extract_photo_file_id(result)
            retry_after = int((result.get("parameters") or {}).get("retry_after") or 0)
            is_rate_limited = int(result.get("error_code") or 0) == 429
            if is_rate_limited and attempt < 2:
                await asyncio.sleep(max(1, retry_after or 3))
                continue
            raise RuntimeError(f"Telegram sendPhoto failed: {result}")
        if last_error:
            raise last_error
        return None

    async def send_photo_file_id(self, chat_id: str | int, file_id: str) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = await self.api(
                    "sendPhoto",
                    {
                        "chat_id": str(chat_id),
                        "photo": file_id,
                    },
                    timeout=20,
                )
            except Exception as exc:
                last_error = exc
                print(f"sendPhoto file_id attempt {attempt + 1} failed: {type(exc).__name__}: {exc}", flush=True)
                if attempt < 2:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise
            if result.get("ok"):
                return
            retry_after = int((result.get("parameters") or {}).get("retry_after") or 0)
            is_rate_limited = int(result.get("error_code") or 0) == 429
            if is_rate_limited and attempt < 2:
                await asyncio.sleep(max(1, retry_after or 3))
                continue
            raise RuntimeError(f"Telegram sendPhoto by file_id failed: {result}")
        if last_error:
            raise last_error

    @staticmethod
    def extract_photo_file_id(result: dict[str, Any]) -> str | None:
        photos = ((result.get("result") or {}).get("photo") or [])
        if not isinstance(photos, list) or not photos:
            return None
        largest = photos[-1]
        if not isinstance(largest, dict):
            return None
        file_id = largest.get("file_id")
        return str(file_id) if file_id else None

    async def send_menu(self, chat_id: int) -> None:
        await self.send_message(chat_id, self.status_text(), reply_markup=self.menu_markup(chat_id))

    def load_settings(self) -> dict[str, str]:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}

    def setting_text(self, key: str, default: str) -> str:
        value = str(self.settings.get(key) or "").strip()
        return value or default

    def setting_int(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(str(self.settings.get(key, default)).strip())
        except (TypeError, ValueError):
            value = int(default)
        return max(minimum, min(maximum, value))

    def setting_float(self, key: str, default: float, *, minimum: float, maximum: float) -> float:
        try:
            value = float(str(self.settings.get(key, default)).strip())
        except (TypeError, ValueError):
            value = float(default)
        return max(minimum, min(maximum, value))

    def setting_bool(self, key: str, default: bool) -> bool:
        raw = str(self.settings.get(key, "1" if default else "0")).strip().lower()
        return raw in {"1", "true", "yes", "on", "enabled"}

    def save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.settings_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self.settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.settings_path)

    def migrate_new_command_messages(self) -> None:
        """Add newer public commands without replacing owner formatting."""
        changed = False
        for key in ("help_message", "private_help_message", "guide_commands_message"):
            current = self.settings.get(key)
            if not isinstance(current, str):
                continue
            updated = current
            if "<code>/new" not in updated:
                lines = updated.splitlines()
                try:
                    trending_index = next(
                        index for index, line in enumerate(lines) if "<code>/trending" in line
                    )
                except StopIteration:
                    continue

                if key == "guide_commands_message":
                    insert_at = trending_index + 1
                    while insert_at < len(lines) and "</blockquote>" not in lines[insert_at]:
                        insert_at += 1
                    if insert_at < len(lines):
                        insert_at += 1
                    addition = [
                        "",
                        "<code>/new</code>",
                        "<blockquote>Discover TON tokens newly verified during the last 7 days.</blockquote>",
                    ]
                else:
                    insert_at = trending_index + 1
                    addition = [
                        "<code>/new</code> — TON tokens verified in the last 7 days",
                    ]
                lines[insert_at:insert_at] = addition
                updated = "\n".join(lines)

            if "<code>/pulse" not in updated:
                lines = updated.splitlines()
                try:
                    trending_index = next(
                        index for index, line in enumerate(lines) if "<code>/trending" in line
                    )
                except StopIteration:
                    trending_index = None

                if trending_index is not None:
                    if key == "guide_commands_message":
                        insert_at = trending_index + 1
                        while insert_at < len(lines) and "</blockquote>" not in lines[insert_at]:
                            insert_at += 1
                        if insert_at < len(lines):
                            insert_at += 1
                        addition = [
                            "",
                            "<code>/pulse</code>",
                            "<blockquote>View significant current activity across tracked TON meme coins.</blockquote>",
                        ]
                    else:
                        insert_at = trending_index + 1
                        addition = [
                            "<code>/pulse</code> — significant activity across tracked TON meme coins",
                        ]
                    lines[insert_at:insert_at] = addition
                    updated = "\n".join(lines)

            updated = updated.replace(
                "<code>/new [filter]</code> — newly launched TON tokens",
                "<code>/new</code> — TON tokens verified in the last 7 days",
            ).replace(
                "<blockquote>Discover newly launched TON tokens by age and minimum valuation.</blockquote>",
                "<blockquote>Discover TON tokens newly verified during the last 7 days.</blockquote>",
            ).replace("<code>/new [filter]</code>", "<code>/new</code>")
            if updated != current:
                self.settings[key] = updated
                changed = True

        custom_new_message = self.settings.get("new_message")
        if isinstance(custom_new_message, str) and "[FILTERS]" in custom_new_message:
            cleaned = "\n".join(
                line for line in custom_new_message.splitlines() if "[FILTERS]" not in line
            ).strip()
            if cleaned and cleaned != custom_new_message:
                self.settings["new_message"] = cleaned
                changed = True

        custom_empty = self.settings.get("new_empty_message")
        if isinstance(custom_empty, str) and "[FILTERS]" in custom_empty:
            cleaned_lines = [
                (
                    "No reviewed TON asset-list additions were found during the last 7 days."
                    if "[FILTERS]" in line
                    else line
                )
                for line in custom_empty.splitlines()
            ]
            cleaned = "\n".join(cleaned_lines).strip()
            if cleaned and cleaned != custom_empty:
                self.settings["new_empty_message"] = cleaned
                changed = True
        if changed:
            self.save_settings()

    @staticmethod
    def default_usage_stats() -> dict[str, Any]:
        return {
            "tracking_started_at": datetime.now(timezone.utc).isoformat(),
            "last_activity": "",
            "users": {},
            "chats": {},
            "total_commands": 0,
            "commands": {
                "dashboard": 0,
                "token_report": 0,
                "ath": 0,
                "conversion": 0,
                "trending": 0,
                "pulse": 0,
                "new": 0,
                "menu": 0,
                "other": 0,
            },
        }

    def load_usage_stats(self) -> dict[str, Any]:
        defaults = self.default_usage_stats()
        try:
            payload = json.loads(self.usage_stats_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, TypeError, json.JSONDecodeError, OSError):
            return defaults
        if not isinstance(payload, dict):
            return defaults

        stats = defaults
        tracking_started = str(payload.get("tracking_started_at") or "").strip()
        last_activity = str(payload.get("last_activity") or "").strip()
        if tracking_started:
            stats["tracking_started_at"] = tracking_started
        stats["last_activity"] = last_activity
        raw_users = payload.get("users") if isinstance(payload.get("users"), dict) else {}
        normalized_users: dict[str, dict[str, Any]] = {}
        for raw_user_id, raw_record in raw_users.items():
            if not isinstance(raw_record, dict):
                continue
            record = dict(raw_record)
            legacy_private = bool(record.get("private"))
            record.setdefault("private_chat", legacy_private)
            record.setdefault("public_chat", not legacy_private)
            record.setdefault("inline_mode", False)
            record["private"] = bool(record.get("private_chat"))
            normalized_users[str(raw_user_id)] = record
        stats["users"] = normalized_users
        stats["chats"] = payload.get("chats") if isinstance(payload.get("chats"), dict) else {}
        try:
            stats["total_commands"] = max(0, int(payload.get("total_commands") or 0))
        except (TypeError, ValueError):
            stats["total_commands"] = 0

        saved_commands = payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
        for key in stats["commands"]:
            try:
                stats["commands"][key] = max(0, int(saved_commands.get(key) or 0))
            except (TypeError, ValueError):
                stats["commands"][key] = 0
        return stats

    def save_usage_stats(self) -> None:
        self.usage_stats_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.usage_stats_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(self.usage_stats, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.usage_stats_path)
        self.usage_stats_dirty = False

    def schedule_usage_stats_save(self) -> None:
        self.usage_stats_dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_usage_stats()
            return
        task = getattr(self, "usage_stats_save_task", None)
        if task is None or task.done():
            self.usage_stats_save_task = loop.create_task(self.flush_usage_stats_later())

    async def flush_usage_stats_later(self) -> None:
        try:
            await asyncio.sleep(1)
            if self.usage_stats_dirty:
                self.save_usage_stats()
        finally:
            self.usage_stats_save_task = None

    def bootstrap_usage_users(self) -> bool:
        changed = False
        for user_id in sorted(self.admin_ids | self.subscriber_ids):
            changed = self.touch_usage_user(user_id, private=True, update_activity=False) or changed
        return changed

    def touch_usage_user(
        self,
        user_id: int,
        *,
        private: bool = False,
        public_chat: bool = False,
        inline_mode: bool = False,
        update_activity: bool = True,
    ) -> bool:
        if user_id <= 0:
            return False
        now = datetime.now(timezone.utc).isoformat()
        users = self.usage_stats.setdefault("users", {})
        key = str(user_id)
        existing = users.get(key) if isinstance(users.get(key), dict) else {}
        was_known = bool(existing)
        changed = not was_known
        if not existing.get("first_seen"):
            existing["first_seen"] = now
            changed = True
        if update_activity:
            existing["last_seen"] = now
            changed = True
        elif not existing.get("last_seen"):
            existing["last_seen"] = existing["first_seen"]
        legacy_private = bool(existing.get("private"))
        existing.setdefault("private_chat", legacy_private)
        existing.setdefault("public_chat", not legacy_private and was_known)
        existing.setdefault("inline_mode", False)
        if private and not bool(existing.get("private_chat")):
            existing["private_chat"] = True
            changed = True
        if public_chat and not bool(existing.get("public_chat")):
            existing["public_chat"] = True
            changed = True
        if inline_mode and not bool(existing.get("inline_mode")):
            existing["inline_mode"] = True
            changed = True
        existing["private"] = bool(existing.get("private_chat"))
        users[key] = existing
        return changed

    def touch_usage_chat(
        self,
        chat: dict[str, Any],
        *,
        active: bool = True,
        update_activity: bool = True,
    ) -> bool:
        chat_type = str(chat.get("type") or "")
        chat_id = int(chat.get("id") or 0)
        if chat_id >= 0 or chat_type not in {"group", "supergroup"}:
            return False

        now = datetime.now(timezone.utc).isoformat()
        chats = self.usage_stats.setdefault("chats", {})
        key = str(chat_id)
        existing = chats.get(key) if isinstance(chats.get(key), dict) else {}
        changed = not bool(existing)
        title = str(chat.get("title") or existing.get("title") or f"Group {chat_id}").strip()
        values = {
            "title": title,
            "type": chat_type,
            "active": bool(active),
        }
        for field, value in values.items():
            if existing.get(field) != value:
                existing[field] = value
                changed = True
        if not existing.get("first_seen"):
            existing["first_seen"] = now
            changed = True
        if update_activity:
            existing["last_seen"] = now
            changed = True
        elif not existing.get("last_seen"):
            existing["last_seen"] = existing["first_seen"]
        chats[key] = existing
        return changed

    def record_message_usage(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = int(sender.get("id") or 0)
        chat_type = str(chat.get("type") or "")
        changed = False
        if not bool(sender.get("is_bot")):
            changed = self.touch_usage_user(
                user_id,
                private=chat_type == "private",
                public_chat=chat_type in {"group", "supergroup"},
            ) or changed
        changed = self.touch_usage_chat(chat, active=True) or changed

        text = str(message.get("text") or "").strip()
        command = self._command_name(text)
        if command:
            if command == "/meme":
                command_key = "token_report" if self._command_args(text) else "dashboard"
            elif command == "/ath":
                command_key = "ath"
            elif command == "/swap":
                command_key = "conversion"
            elif command == "/trending":
                command_key = "trending"
            elif command == "/pulse":
                command_key = "pulse"
            elif command == "/new":
                command_key = "new"
            elif command == "/alert":
                command_key = "alert"
            elif command == "/help":
                command_key = "help"
            elif command in {"/start", "/menu"}:
                command_key = "menu"
            else:
                command_key = "other"
            commands = self.usage_stats.setdefault("commands", {})
            commands[command_key] = int(commands.get(command_key) or 0) + 1
            self.usage_stats["total_commands"] = int(self.usage_stats.get("total_commands") or 0) + 1
            changed = True

        if changed:
            self.usage_stats["last_activity"] = datetime.now(timezone.utc).isoformat()
            self.schedule_usage_stats_save()

    def record_callback_usage(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        changed = False
        if not bool(sender.get("is_bot")):
            changed = self.touch_usage_user(
                int(sender.get("id") or 0),
                private=str(chat.get("type") or "") == "private",
                public_chat=str(chat.get("type") or "") in {"group", "supergroup"},
            ) or changed
        changed = self.touch_usage_chat(chat, active=True) or changed
        if changed:
            self.usage_stats["last_activity"] = datetime.now(timezone.utc).isoformat()
            self.schedule_usage_stats_save()

    def record_inline_usage(self, inline_query: dict[str, Any]) -> None:
        if not isinstance(getattr(self, "usage_stats", None), dict):
            return
        sender = inline_query.get("from") or {}
        if bool(sender.get("is_bot")):
            return
        if self.touch_usage_user(
            int(sender.get("id") or 0),
            inline_mode=True,
        ):
            self.usage_stats["last_activity"] = datetime.now(timezone.utc).isoformat()
            self.schedule_usage_stats_save()

    def record_chat_membership(self, membership: dict[str, Any]) -> None:
        chat = membership.get("chat") or {}
        new_status = str((membership.get("new_chat_member") or {}).get("status") or "")
        active = new_status not in {"left", "kicked"}
        if self.touch_usage_chat(chat, active=active):
            self.usage_stats["last_activity"] = datetime.now(timezone.utc).isoformat()
            self.schedule_usage_stats_save()

    def load_subscribers(self) -> set[int]:
        try:
            payload = json.loads(self.subscribers_path.read_text(encoding="utf-8-sig"))
            raw_users = payload.get("users", []) if isinstance(payload, dict) else payload
        except (FileNotFoundError, TypeError, json.JSONDecodeError, OSError):
            return set()
        users: set[int] = set()
        for raw_user_id in raw_users if isinstance(raw_users, list) else []:
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                users.add(user_id)
        return users.difference(self.admin_ids)

    def save_subscribers(self) -> None:
        self.subscribers_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.subscribers_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps({"users": sorted(self.subscriber_ids)}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.subscribers_path)

    def remember_subscriber(self, user_id: int) -> None:
        if user_id <= 0 or self.authorized(user_id) or user_id in self.subscriber_ids:
            return
        self.subscriber_ids.add(user_id)
        self.save_subscribers()

    def remove_subscribers(self, user_ids: set[int]) -> None:
        if not user_ids:
            return
        self.subscriber_ids.difference_update(user_ids)
        self.save_subscribers()

    async def save_pending_text_edit(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        key = self.pending_text_edits.get(user_id)
        if key not in self.TEXT_EDIT_KEYS:
            self.pending_text_edits.pop(user_id, None)
            return
        if len(text) > 4096:
            await self.send_message(chat_id, "The message is too long. Telegram allows up to 4,096 characters.")
            return

        saved_text = self.rich_text_to_html(text, entities or [])
        validation_error = self.validate_message_template(key, saved_text)
        if validation_error:
            await self.send_message(
                chat_id,
                f"❌ <b>Message not saved</b>\n\n{html.escape(validation_error)}",
                reply_markup=self.cancel_edit_markup(),
            )
            return
        preview = await self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": saved_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not preview.get("ok"):
            description = html.escape(str(preview.get("description") or "Telegram rejected the message."))
            await self.send_message(
                chat_id,
                f"❌ <b>Message not saved</b>\n\n{description}\n\nFix the formatting and send it again.",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        self.settings[key] = saved_text
        self.save_settings()
        if key in self.PUBLIC_MESSAGE_DEFAULTS:
            setattr(self, key, saved_text)
            label = self.PUBLIC_MESSAGE_LABELS[key].title()
        elif key == "trending_title":
            self.trending_title = saved_text
            label = "Trending title"
        elif key == "trending_message":
            self.trending_message = saved_text
            label = "Trending message"
        else:
            self.trending_row = saved_text
            label = "Trending row"
        self.pending_text_edits.pop(user_id, None)
        await self.send_message(
            chat_id,
            f"✅ <b>{label} saved</b>\n\nUsers will see the preview above.",
            reply_markup=self.settings_markup_for_key(key),
        )

    async def save_pending_broadcast(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        if len(text) > 4_096:
            await self.send_message(chat_id, "The broadcast is too long. Telegram allows up to 4,096 characters.")
            return

        saved_text = self.rich_text_to_html(text, entities or [])
        preview = await self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": saved_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not preview.get("ok"):
            description = html.escape(str(preview.get("description") or "Telegram rejected the message."))
            await self.send_message(
                chat_id,
                f"❌ <b>Broadcast not saved</b>\n\n{description}\n\nFix it and send it again.",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        self.pending_text_edits.pop(user_id, None)
        self.pending_broadcasts[user_id] = saved_text
        await self.send_message(
            chat_id,
            "📣 <b>Broadcast preview ready</b>\n\n"
            f"Audience: <b>{len(self.subscriber_ids)} subscribed bot users</b>\n\n"
            "The exact message appears above. Confirm only when it is ready.",
            reply_markup=self.confirm_broadcast_markup(),
        )

    async def broadcast_message(self, text: str) -> tuple[int, int, int]:
        async with self.broadcast_lock:
            targets = sorted(self.subscriber_ids)
            sent = 0
            failed = 0
            unreachable: set[int] = set()
            batch_size = 20
            for start in range(0, len(targets), batch_size):
                batch = targets[start : start + batch_size]
                results = await asyncio.gather(
                    *(self.send_broadcast_target(user_id, text) for user_id in batch),
                    return_exceptions=True,
                )
                for user_id, result in zip(batch, results):
                    if isinstance(result, Exception):
                        print(
                            f"Broadcast delivery failed user={user_id}: {type(result).__name__}: {result}",
                            flush=True,
                        )
                        failed += 1
                    elif result == "sent":
                        sent += 1
                    elif result == "unreachable":
                        failed += 1
                        unreachable.add(user_id)
                    else:
                        failed += 1
                if start + batch_size < len(targets):
                    await asyncio.sleep(1.05)

            self.remove_subscribers(unreachable)
            return sent, failed, len(unreachable)

    async def send_broadcast_target(self, user_id: int, text: str) -> str:
        for attempt in range(2):
            result = await self.api(
                "sendMessage",
                {
                    "chat_id": str(user_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )
            if result.get("ok"):
                return "sent"

            error_code = int(result.get("error_code") or 0)
            description = str(result.get("description") or "").lower()
            if error_code == 429 and attempt == 0:
                retry_after = int((result.get("parameters") or {}).get("retry_after") or 1)
                await asyncio.sleep(max(1, retry_after))
                continue
            if error_code == 403 or any(
                marker in description
                for marker in ("bot was blocked", "user is deactivated", "chat not found")
            ):
                return "unreachable"
            return "failed"
        return "failed"

    @staticmethod
    def broadcast_result_text(sent: int, failed: int, removed: int) -> str:
        return (
            "✅ <b>Broadcast completed</b>\n\n"
            f"Delivered: <b>{sent}</b>\n"
            f"Failed: <b>{failed}</b>\n"
            f"Unreachable users removed: <b>{removed}</b>"
        )

    async def save_pending_value_edit(self, user_id: int, chat_id: int, raw_value: str) -> None:
        key = self.pending_text_edits.get(user_id)
        if key not in self.VALUE_EDIT_KEYS:
            self.pending_text_edits.pop(user_id, None)
            return

        try:
            stored_value, display_value = self.apply_value_setting(key, raw_value)
        except ValueError as exc:
            await self.send_message(
                chat_id,
                f"❌ <b>Setting not saved</b>\n\n{html.escape(str(exc))}",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        self.settings[self.persisted_key_for_value_edit(key)] = stored_value
        self.save_settings()
        self.pending_text_edits.pop(user_id, None)
        await self.send_message(
            chat_id,
            f"✅ <b>Setting saved</b>\n\n{html.escape(display_value)}",
            reply_markup=self.settings_markup_for_key(key),
        )

    def apply_value_setting(self, key: str, raw_value: str) -> tuple[str, str]:
        value = str(raw_value or "").strip()
        if key == "channel":
            channel = self.normalize_chat_reference(value, require_public=False)
            self.channel = channel
            return channel, f"Posting channel: {channel}"
        if key == "required_channel":
            channel = self.normalize_chat_reference(value, require_public=True)
            self.required_channel = channel
            self.membership_cache.clear()
            return channel, f"Required channel: {channel}"
        if key == "trending_result_limit":
            try:
                count = int(value)
            except ValueError as exc:
                raise ValueError("Send a whole number between 3 and 20.") from exc
            if not 3 <= count <= 20:
                raise ValueError("The trending list must contain between 3 and 20 coins.")
            self.trending_service.result_limit = count
            return str(count), f"Trending list size: {count} coins"
        if key == "trending_min_liquidity_usd":
            cleaned = value.replace("$", "").replace(",", "").strip()
            try:
                liquidity = float(cleaned)
            except ValueError as exc:
                raise ValueError("Send a USD amount between 0 and 1,000,000,000.") from exc
            if not 0 <= liquidity <= 1_000_000_000:
                raise ValueError("Minimum liquidity must be between $0 and $1,000,000,000.")
            self.trending_service.minimum_liquidity_usd = liquidity
            return format(liquidity, ".2f"), f"Minimum trending liquidity: ${liquidity:,.2f}"
        if key == "background_refresh_seconds":
            seconds = self.parse_bounded_integer(value, 30, 3_600, "Price refresh")
            self.background_refresh_seconds = seconds
            return str(seconds), f"Price refresh: every {self.format_duration(seconds)}"
        if key == "image_cache_seconds":
            seconds = self.parse_bounded_integer(value, 5, 3_600, "Image cache")
            self.image_cache_seconds = seconds
            return str(seconds), f"Image cache lifetime: {self.format_duration(seconds)}"
        if key == "utya_movement_threshold_percent":
            cleaned = value.replace("%", "").strip()
            try:
                threshold = float(cleaned)
            except ValueError as exc:
                raise ValueError("Send a percentage from 1 to 100, for example 10.") from exc
            if not 1 <= threshold <= 100:
                raise ValueError("The UTYA movement threshold must be from 1% to 100%.")
            self.utya_movement_threshold_percent = threshold
            self.utya_movement_tracker.reset()
            return (
                format(threshold, ".8g"),
                f"UTYA movement threshold: {threshold:g}%. The reference was reset.",
            )
        if key == "market_overview_channel":
            channel = self.normalize_chat_reference(value, require_public=False)
            self.market_overview_channel = channel
            return channel, f"Market overview channel: {channel}"
        if key == "market_overview_interval_minutes":
            minutes = parse_interval_minutes(value)
            self.market_overview_interval_minutes = minutes
            self.market_overview_store.schedule_after(minutes)
            return str(minutes), f"Market overview interval: {format_interval(minutes)}"
        raise ValueError("This setting is not supported.")

    @staticmethod
    def normalize_chat_reference(value: str, *, require_public: bool) -> str:
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
            return value
        if not require_public and re.fullmatch(r"-100\d{6,16}", value):
            return value
        if require_public:
            raise ValueError("Send a public channel username such as @memeprice.")
        raise ValueError("Send a public @channel username or a numeric -100... channel ID.")

    @staticmethod
    def parse_bounded_integer(value: str, minimum: int, maximum: int, label: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a whole number from {minimum} to {maximum} seconds.") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{label} must be from {minimum} to {maximum} seconds.")
        return parsed

    @staticmethod
    def persisted_key_for_value_edit(key: str) -> str:
        return key

    def edit_value_prompt(self, key: str) -> str:
        prompts = {
            "channel": (
                "📡 <b>Change posting channel</b>\n\n"
                f"Current: <b>{html.escape(self.channel)}</b>\n\n"
                "Send a public @channel username or a numeric -100... channel ID."
            ),
            "required_channel": (
                "🔐 <b>Change required channel</b>\n\n"
                f"Current: <b>{html.escape(self.required_channel)}</b>\n\n"
                "Send the public channel username users must join, for example @memeprice."
            ),
            "trending_result_limit": (
                "📊 <b>Change trending list size</b>\n\n"
                f"Current: <b>{self.trending_service.result_limit} coins</b>\n\n"
                "Send a whole number from 3 to 20."
            ),
            "trending_min_liquidity_usd": (
                "💧 <b>Change minimum liquidity</b>\n\n"
                f"Current: <b>${self.trending_service.minimum_liquidity_usd:,.2f}</b>\n\n"
                "Send the minimum USD liquidity required for a coin to appear."
            ),
            "background_refresh_seconds": (
                "🔄 <b>Change price refresh</b>\n\n"
                f"Current: <b>{self.format_duration(self.background_refresh_seconds)}</b>\n\n"
                "Send a number of seconds from 30 to 3,600."
            ),
            "image_cache_seconds": (
                "⚡ <b>Change image cache lifetime</b>\n\n"
                f"Current: <b>{self.format_duration(self.image_cache_seconds)}</b>\n\n"
                "Send a number of seconds from 5 to 3,600."
            ),
            "utya_movement_threshold_percent": (
                "📈 <b>Change UTYA movement threshold</b>\n\n"
                f"Current: <b>{self.utya_movement_threshold_percent:g}%</b>\n\n"
                "Send a percentage from 1 to 100. Changing it resets the reference price "
                "so an old movement cannot trigger immediately."
            ),
            "market_overview_channel": (
                "📡 <b>Change overview channel</b>\n\n"
                f"Current: <b>{html.escape(self.market_overview_channel)}</b>\n\n"
                "Send a public @channel username or a numeric -100... channel ID. "
                "The bot must be allowed to post there."
            ),
            "market_overview_interval_minutes": (
                "⏱ <b>Change overview interval</b>\n\n"
                f"Current: <b>{format_interval(self.market_overview_interval_minutes)}</b>\n\n"
                "Send a value such as 30m, 2h, or 1d. A new approval preview is generated "
                "after this interval; it is never posted without approval."
            ),
        }
        return prompts.get(key, "Send the new value.") + "\n\nUse /cancel to keep the current setting."

    def edit_message_prompt(self, key: str) -> str:
        if key in self.PUBLIC_MESSAGE_DEFAULTS:
            label = self.PUBLIC_MESSAGE_LABELS[key]
            current = str(getattr(self, key, self.PUBLIC_MESSAGE_DEFAULTS[key]))
            required = self.PUBLIC_MESSAGE_REQUIRED_PLACEHOLDERS.get(key, ())
            placeholder_help = (
                "\n\n<b>Required:</b> "
                + ", ".join(f"<code>{placeholder}</code>" for placeholder in required)
                if required
                else ""
            )
            optional = self.PUBLIC_MESSAGE_OPTIONAL_PLACEHOLDERS.get(key, ())
            if optional:
                placeholder_help += (
                    "\n<b>Optional:</b> "
                    + ", ".join(f"<code>{placeholder}</code>" for placeholder in optional)
                )
            if key in {"utya_movement_up_message", "utya_movement_down_message"}:
                placeholder_help += (
                    "\n<b>Optional:</b> <code>[SIGNED_CHANGE_PERCENT]</code>, "
                    "<code>[DIRECTION]</code>, <code>[THRESHOLD]</code>, "
                    "<code>[TIME_UTC]</code>, <code>[CHANNEL]</code>"
                )
        elif key == "trending_title":
            label = "trending title"
            current = self.trending_title
            placeholder_help = ""
        elif key == "trending_message":
            label = "trending message"
            current = self.trending_message
            placeholder_help = (
                "\n\n<b>Required:</b> <code>[TITLE]</code>, <code>[LIST]</code>\n"
                "<b>Optional:</b> <code>[UPDATED_AT]</code>"
            )
        else:
            label = "trending list row"
            current = self.trending_row
            placeholder_help = (
                "\n\n<b>Required:</b> <code>[RANK]</code>, <code>[TICKER]</code>, "
                "<code>[CHANGE_24H]</code>\n"
                "<b>Optional:</b> <code>[NAME]</code>"
            )
        return (
            f"✏️ <b>Edit {label}</b>\n\n"
            f"<b>Current message:</b>\n{current}\n\n"
            "Send the complete replacement text now. Telegram formatting is preserved. "
            "To use a custom emoji, insert it directly from Telegram's emoji panel—the bot stores "
            "its emoji ID automatically, so you do not need to enter HTML or an ID."
            f"{placeholder_help}\n\n"
            "Use /cancel or the button below to keep the current text."
        )

    @classmethod
    def normalize_persisted_pulse_emoji(cls, value: str, default: str) -> str:
        raw = str(value or "").strip()
        custom = re.fullmatch(
            r'<tg-emoji emoji-id="(?P<id>[0-9]{1,32})">(?P<fallback>[^<>]{1,64})</tg-emoji>',
            raw,
        )
        if custom:
            fallback = html.unescape(custom.group("fallback")).strip()
            if fallback and len(fallback) <= 24 and not any(character.isspace() for character in fallback):
                return raw

        plain = html.unescape(raw)
        if (
            not plain
            or len(plain) > 24
            or any(character.isspace() for character in plain)
            or any(character in plain for character in "<>&")
        ):
            return html.escape(default)
        return html.escape(plain)

    @classmethod
    def parse_pulse_emoji_input(
        cls,
        text: str,
        entities: list[dict[str, Any]],
    ) -> str:
        custom_entities = [
            entity
            for entity in entities
            if str(entity.get("type") or "") == "custom_emoji"
        ]
        if len(custom_entities) > 1:
            raise ValueError("Send exactly one emoji, not multiple custom emojis.")
        if custom_entities:
            entity = custom_entities[0]
            emoji_id = str(entity.get("custom_emoji_id") or "").strip()
            if not emoji_id.isdigit():
                raise ValueError("Telegram did not provide a valid custom emoji ID.")
            try:
                start_units = int(entity.get("offset") or 0)
                end_units = start_units + int(entity.get("length") or 0)
                offset_map = cls.utf16_offset_map(text)
                start = offset_map[start_units]
                end = offset_map[end_units]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("The custom emoji position could not be read.") from exc
            if (text[:start] + text[end:]).strip():
                raise ValueError("Send only the custom emoji, without a label or other text.")
            fallback = text[start:end].strip()
            if not fallback:
                raise ValueError("The custom emoji could not be read.")
            return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(fallback)}</tg-emoji>'

        emoji = text.strip()
        if not emoji or len(emoji) > 24 or any(character.isspace() for character in emoji):
            raise ValueError("Send exactly one regular emoji or one Telegram custom emoji.")
        if any(character in emoji for character in "<>&"):
            raise ValueError("Send an emoji directly from Telegram's emoji panel.")
        return emoji

    async def save_pending_pulse_emoji_edit(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        entities: list[dict[str, Any]],
    ) -> None:
        pending_key = self.pending_text_edits.get(user_id, "")
        is_coin = pending_key.startswith(self.PULSE_COIN_EMOJI_SETTING_PREFIX)
        prefix = (
            self.PULSE_COIN_EMOJI_SETTING_PREFIX
            if is_coin
            else self.PULSE_EMOJI_SETTING_PREFIX
        )
        defaults = self.PULSE_COIN_EMOJI_DEFAULTS if is_coin else self.PULSE_EMOJI_DEFAULTS
        key = pending_key.removeprefix(prefix)
        if not pending_key.startswith(prefix) or key not in defaults:
            self.pending_text_edits.pop(user_id, None)
            return
        try:
            parsed = self.parse_pulse_emoji_input(text, entities)
            saved = self.normalize_persisted_pulse_emoji(
                parsed,
                defaults[key],
            )
        except ValueError as exc:
            await self.send_message(
                chat_id,
                f"❌ <b>Emoji not saved</b>\n\n{html.escape(str(exc))}",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        label = key if is_coin else self.PULSE_EMOJI_LABELS[key]
        preview = await self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": f"👁 <b>Pulse emoji preview</b>\n\n{saved} {html.escape(label)}",
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not preview.get("ok"):
            description = html.escape(
                str(preview.get("description") or "Telegram rejected this emoji.")
            )
            await self.send_message(
                chat_id,
                f"❌ <b>Emoji not saved</b>\n\n{description}",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        setting_key = f"{prefix}{key}"
        self.settings[setting_key] = saved
        self.save_settings()
        target = self.pulse_coin_emojis if is_coin else self.pulse_emojis
        target[key] = saved
        self.pending_text_edits.pop(user_id, None)
        await self.send_message(
            chat_id,
            f"✅ <b>{html.escape(label)} emoji saved</b>\n\n"
            "The formatted /pulse preview now uses this emoji.",
            reply_markup=(
                self.pulse_coin_emoji_settings_markup()
                if is_coin
                else self.pulse_emoji_settings_markup()
            ),
        )

    def edit_public_button_prompt(self, key: str) -> str:
        default = self.PUBLIC_BUTTON_DEFAULTS[key]
        current = str(getattr(self, "public_button_texts", {}).get(key, default) or default)
        icon_id = str(getattr(self, "public_button_icons", {}).get(key, "") or "").strip()
        required = self.PUBLIC_BUTTON_REQUIRED_PLACEHOLDERS.get(key, ())
        placeholder_help = (
            "\n\n<b>Required:</b> "
            + ", ".join(f"<code>{placeholder}</code>" for placeholder in required)
            if required
            else ""
        )
        icon_status = f"Custom emoji ID: <code>{icon_id}</code>" if icon_id else "Custom emoji: none"
        return (
            f"✏️ <b>Edit {html.escape(self.PUBLIC_BUTTON_LABELS[key])}</b>\n\n"
            f"<b>Current label:</b> <code>{html.escape(current)}</code>\n"
            f"{icon_status}\n\n"
            "Send the complete new button label. You may include normal emojis directly.\n\n"
            "To use a Telegram custom emoji, select exactly one custom emoji and type the label beside it. "
            "The custom emoji becomes the icon before the button text.\n\n"
            "Sending a label without a custom emoji removes the current custom icon."
            f"{placeholder_help}\n\n"
            "Use /cancel to keep the current button."
        )

    @classmethod
    def parse_public_button_input(
        cls,
        text: str,
        entities: list[dict[str, Any]],
    ) -> tuple[str, str]:
        custom_entities = [
            entity
            for entity in entities
            if str(entity.get("type") or "") == "custom_emoji"
        ]
        if len(custom_entities) > 1:
            raise ValueError("Use no more than one custom emoji in a button.")
        if not custom_entities:
            return text.strip(), ""

        entity = custom_entities[0]
        icon_id = str(entity.get("custom_emoji_id") or "").strip()
        if not icon_id.isdigit():
            raise ValueError("Telegram did not provide a valid custom emoji ID.")
        try:
            start_units = int(entity.get("offset") or 0)
            end_units = start_units + int(entity.get("length") or 0)
            offset_map = cls.utf16_offset_map(text)
            start = offset_map[start_units]
            end = offset_map[end_units]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The custom emoji position could not be read.") from exc
        return (text[:start] + text[end:]).strip(), icon_id

    async def save_pending_public_button_edit(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        entities: list[dict[str, Any]],
    ) -> None:
        pending_key = self.pending_text_edits.get(user_id, "")
        key = pending_key.removeprefix("public_button:")
        if not pending_key.startswith("public_button:") or key not in self.PUBLIC_BUTTON_DEFAULTS:
            self.pending_text_edits.pop(user_id, None)
            return

        try:
            label, icon_id = self.parse_public_button_input(text, entities)
            if not label:
                raise ValueError("The button label cannot be empty.")
            if len(label) > 64:
                raise ValueError("The button label must be 64 characters or fewer.")
            missing = [
                placeholder
                for placeholder in self.PUBLIC_BUTTON_REQUIRED_PLACEHOLDERS.get(key, ())
                if placeholder not in label
            ]
            if missing:
                raise ValueError(f"Missing required placeholder(s): {', '.join(missing)}")
        except ValueError as exc:
            await self.send_message(
                chat_id,
                f"❌ <b>Button not saved</b>\n\n{html.escape(str(exc))}",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        preview_button = {
            "text": label,
            "callback_data": "public_button_preview",
        }
        if icon_id:
            preview_button["icon_custom_emoji_id"] = icon_id
        preview = await self.api(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": "🔘 <b>Button preview</b>",
                "parse_mode": "HTML",
                "reply_markup": json.dumps({"inline_keyboard": [[preview_button]]}),
            },
        )
        if not preview.get("ok"):
            description = html.escape(str(preview.get("description") or "Telegram rejected the button."))
            await self.send_message(
                chat_id,
                f"❌ <b>Button not saved</b>\n\n{description}",
                reply_markup=self.cancel_edit_markup(),
            )
            return

        texts = getattr(self, "public_button_texts", {})
        icons = getattr(self, "public_button_icons", {})
        texts[key] = label
        icons[key] = icon_id
        self.public_button_texts = texts
        self.public_button_icons = icons
        self.settings[f"public_button_text:{key}"] = label
        if icon_id:
            self.settings[f"public_button_icon:{key}"] = icon_id
        else:
            self.settings.pop(f"public_button_icon:{key}", None)
        self.save_settings()
        self.pending_text_edits.pop(user_id, None)
        await self.send_message(
            chat_id,
            f"✅ <b>{html.escape(self.PUBLIC_BUTTON_LABELS[key].title())} saved</b>\n\n"
            "The preview above uses the new public button.",
            reply_markup=self.public_button_group_markup(self.public_button_group_for_key(key)),
        )

    def validate_message_template(self, key: str, value: str) -> str | None:
        required = self.PUBLIC_MESSAGE_REQUIRED_PLACEHOLDERS.get(key) or {
            "trending_message": ("[TITLE]", "[LIST]"),
            "trending_row": ("[RANK]", "[TICKER]", "[CHANGE_24H]"),
        }.get(key, ())
        missing = [placeholder for placeholder in required if placeholder not in value]
        if missing:
            return f"Missing required placeholder(s): {', '.join(missing)}"

        if key in {"trending_title", "trending_message", "trending_row"}:
            title_template = value if key == "trending_title" else self.trending_title
            message_template = value if key == "trending_message" else self.trending_message
            row_template = value if key == "trending_row" else self.trending_row
            estimated_length = len(
                message_template.replace("[TITLE]", title_template).replace("[LIST]", "")
            ) + (10 * len(row_template))
            if estimated_length > 4_000:
                return "The completed 10-row trending message would be too long for Telegram."
        if key in {
            "market_overview_message",
            "market_overview_price_row",
            "market_overview_cap_row",
        }:
            message_template = (
                value
                if key == "market_overview_message"
                else str(
                    getattr(
                        self,
                        "market_overview_message",
                        self.DEFAULT_MARKET_OVERVIEW_MESSAGE,
                    )
                )
            )
            price_row = (
                value
                if key == "market_overview_price_row"
                else str(
                    getattr(
                        self,
                        "market_overview_price_row",
                        self.DEFAULT_MARKET_OVERVIEW_PRICE_ROW,
                    )
                )
            )
            cap_row = (
                value
                if key == "market_overview_cap_row"
                else str(
                    getattr(
                        self,
                        "market_overview_cap_row",
                        self.DEFAULT_MARKET_OVERVIEW_CAP_ROW,
                    )
                )
            )
            estimated_length = len(
                message_template.replace("[PRICES]", "\n".join([price_row] * 9)).replace(
                    "[MARKET_CAPS]", "\n".join([cap_row] * 9)
                )
            )
            if estimated_length > 3_800:
                return "The completed nine-coin market overview would be too long for Telegram."
        return None

    @classmethod
    def rich_text_to_html(cls, text: str, entities: list[dict[str, Any]]) -> str:
        supported = {
            "bold",
            "italic",
            "underline",
            "strikethrough",
            "spoiler",
            "code",
            "pre",
            "text_link",
            "text_mention",
            "blockquote",
            "expandable_blockquote",
            "custom_emoji",
        }
        rich_entities = [entity for entity in entities if str(entity.get("type") or "") in supported]
        if not rich_entities:
            # Plain input keeps supporting manually entered Telegram HTML.
            return text

        offset_map = cls.utf16_offset_map(text)
        spans: list[tuple[int, int, str, str, int]] = []
        for order, entity in enumerate(rich_entities):
            try:
                start_units = int(entity.get("offset") or 0)
                end_units = start_units + int(entity.get("length") or 0)
                start = offset_map[start_units]
                end = offset_map[end_units]
            except (KeyError, TypeError, ValueError):
                continue
            if start >= end:
                continue
            tags = cls.entity_html_tags(entity)
            if tags is None:
                continue
            opening, closing = tags
            priority = 100 if entity.get("type") == "custom_emoji" else order
            spans.append((start, end, opening, closing, priority))

        if not spans:
            return html.escape(text)

        boundaries = sorted({0, len(text), *(span[0] for span in spans), *(span[1] for span in spans)})
        output: list[str] = []
        active_before: list[tuple[int, int, str, str, int]] = []
        for start, end in zip(boundaries, boundaries[1:]):
            active = [span for span in spans if span[0] <= start and span[1] >= end]
            active.sort(key=lambda span: (span[0], -span[1], span[4]))

            common = 0
            while (
                common < len(active_before)
                and common < len(active)
                and active_before[common] == active[common]
            ):
                common += 1
            for span in reversed(active_before[common:]):
                output.append(span[3])
            for span in active[common:]:
                output.append(span[2])
            output.append(html.escape(text[start:end]))
            active_before = active

        for span in reversed(active_before):
            output.append(span[3])
        return "".join(output)

    @staticmethod
    def utf16_offset_map(text: str) -> dict[int, int]:
        mapping = {0: 0}
        offset = 0
        for index, character in enumerate(text):
            offset += 2 if ord(character) > 0xFFFF else 1
            mapping[offset] = index + 1
        return mapping

    @staticmethod
    def entity_html_tags(entity: dict[str, Any]) -> tuple[str, str] | None:
        entity_type = str(entity.get("type") or "")
        simple_tags = {
            "bold": ("<b>", "</b>"),
            "italic": ("<i>", "</i>"),
            "underline": ("<u>", "</u>"),
            "strikethrough": ("<s>", "</s>"),
            "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
            "code": ("<code>", "</code>"),
            "blockquote": ("<blockquote>", "</blockquote>"),
            "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
        }
        if entity_type in simple_tags:
            return simple_tags[entity_type]
        if entity_type == "pre":
            language = str(entity.get("language") or "").strip()
            if language:
                safe_language = html.escape(language, quote=True)
                return (f'<pre><code class="language-{safe_language}">', "</code></pre>")
            return ("<pre>", "</pre>")
        if entity_type == "text_link":
            url = html.escape(str(entity.get("url") or ""), quote=True)
            return (f'<a href="{url}">', "</a>") if url else None
        if entity_type == "text_mention":
            user_id = int((entity.get("user") or {}).get("id") or 0)
            return (f'<a href="tg://user?id={user_id}">', "</a>") if user_id else None
        if entity_type == "custom_emoji":
            emoji_id = html.escape(str(entity.get("custom_emoji_id") or ""), quote=True)
            return (f'<tg-emoji emoji-id="{emoji_id}">', "</tg-emoji>") if emoji_id else None
        return None

    def message_settings_text(self) -> str:
        return (
            "✏️ <b>Messages</b>\n\n"
            "Edit every regular message that public users can receive. Messages are grouped by feature for clarity.\n\n"
            f"📣 Broadcast audience: <b>{len(self.subscriber_ids)} subscribed bot users</b>\n"
            "✨ Telegram formatting and custom emojis are preserved."
        )

    @staticmethod
    def general_message_settings_text() -> str:
        return (
            "💬 <b>General Public Messages</b>\n\n"
            "Edit the public and private /help messages, plus the request-limit message."
        )

    @staticmethod
    def token_message_settings_text() -> str:
        return (
            "🪙 <b>Token Report Messages</b>\n\n"
            "Edit token-search errors, multiple-result prompts, ATH responses, and not-found responses."
        )

    @staticmethod
    def conversion_message_settings_text() -> str:
        return (
            "🔄 <b>Converter Messages</b>\n\n"
            "Edit /swap instructions, unavailable responses, and token not-found responses."
        )

    @staticmethod
    def inline_message_settings_text() -> str:
        return (
            "🪄 <b>Inline-Mode Messages</b>\n\n"
            "Edit every message users can insert into any chat from @memepricesbot: coin statistics, "
            "conversion results, and help or error guidance.\n\n"
            "Live-value placeholders are validated before saving. Telegram formatting and custom emojis are preserved."
        )

    @staticmethod
    def subscription_message_settings_text() -> str:
        return (
            "🔐 <b>Subscription Messages</b>\n\n"
            "Edit the message shown after access is confirmed and the prompt shown when a private user "
            "must join the required channel."
        )

    @staticmethod
    def trending_message_settings_text() -> str:
        return (
            "🔥 <b>Trending Messages</b>\n\n"
            "Edit the /trending title, complete layout, repeated coin row, and unavailable response.\n\n"
            "Placeholders are checked before a format is saved."
        )

    @staticmethod
    def pulse_message_settings_text() -> str:
        return (
            "⚡️ <b>Pulse Messages</b>\n\n"
            "Edit the /pulse title, result layout, repeated event block, "
            "normal-market response, and unavailable response.\n\n"
            "Keep <code>[EMOJI]</code> in the event layout where each coin's own icon should appear. "
            "Use <b>Edit coin emojis</b> to assign a regular or Telegram custom emoji to every tracked "
            "coin. Signal icons remain available through <code>[SIGNAL_EMOJI]</code>. Use the preview "
            "button to check the completed layout."
        )

    def pulse_emoji_settings_text(self) -> str:
        rows = []
        configured = getattr(self, "pulse_emojis", {})
        for key, default in self.PULSE_EMOJI_DEFAULTS.items():
            emoji = self.normalize_persisted_pulse_emoji(configured.get(key, default), default)
            rows.append(f"{emoji} <b>{html.escape(self.PULSE_EMOJI_LABELS[key])}</b>")
        return (
            "🎨 <b>Pulse Signal Emojis</b>\n\n"
            "Each movement type has its own emoji. Select one below, then send exactly one regular "
            "emoji or one Telegram custom emoji.\n\n"
            + "\n".join(rows)
            + "\n\nThe <code>[EMOJI]</code> placeholder inserts the correct configured emoji at runtime."
        )

    def edit_pulse_emoji_prompt(self, key: str) -> str:
        default = self.PULSE_EMOJI_DEFAULTS[key]
        current = self.normalize_persisted_pulse_emoji(
            getattr(self, "pulse_emojis", {}).get(key, default),
            default,
        )
        label = html.escape(self.PULSE_EMOJI_LABELS[key])
        return (
            f"🎨 <b>Edit {label} emoji</b>\n\n"
            f"Current: {current}\n\n"
            "Send exactly one regular emoji, or select exactly one Telegram custom emoji from the "
            "emoji panel. Do not add a label, HTML, or an emoji ID.\n\n"
            "Use /cancel to keep the current emoji."
        )

    def pulse_coin_emoji_settings_text(self) -> str:
        rows = []
        configured = getattr(self, "pulse_coin_emojis", {})
        for ticker, default in self.PULSE_COIN_EMOJI_DEFAULTS.items():
            emoji = self.normalize_persisted_pulse_emoji(
                configured.get(ticker, default),
                default,
            )
            rows.append(f"{emoji} <b>{html.escape(ticker)}</b>")
        return (
            "🪙 <b>Pulse Coin Emojis</b>\n\n"
            "Every tracked coin has its own /pulse emoji. Select a coin below, then send exactly one "
            "regular emoji or one Telegram custom emoji.\n\n"
            + "\n".join(rows)
            + "\n\nThe <code>[EMOJI]</code> and <code>[COIN_EMOJI]</code> placeholders insert the "
            "configured coin emoji."
        )

    def edit_pulse_coin_emoji_prompt(self, ticker: str) -> str:
        default = self.PULSE_COIN_EMOJI_DEFAULTS[ticker]
        current = self.normalize_persisted_pulse_emoji(
            getattr(self, "pulse_coin_emojis", {}).get(ticker, default),
            default,
        )
        return (
            f"🪙 <b>Edit {html.escape(ticker)} emoji</b>\n\n"
            f"Current: {current}\n\n"
            "Send exactly one regular emoji, or select exactly one Telegram custom emoji from the "
            "emoji panel. Do not add a label, HTML, or an emoji ID.\n\n"
            "Use /cancel to keep the current emoji."
        )

    @staticmethod
    def new_message_settings_text() -> str:
        return (
            "🆕 <b>Newly Verified Messages</b>\n\n"
            "Edit the /new result layout, repeated token row, command instructions, empty result, and "
            "unavailable response.\n\n"
            "Live-data placeholders are checked before a format is saved. Insert custom emojis directly "
            "from Telegram's emoji panel; their IDs are captured automatically. Use the preview button "
            "to check the completed layout."
        )

    @staticmethod
    def alert_message_settings_text() -> str:
        return (
            "🔔 <b>Alert Messages</b>\n\n"
            "Every public message in the /alert workflow is editable. Choose a section below to keep "
            "the editor clear and manageable.\n\n"
            "Dynamic placeholders are validated before saving. Telegram formatting and custom emojis are preserved."
        )

    @staticmethod
    def guide_message_settings_text() -> str:
        return (
            "🏳️ <b>Guide Messages</b>\n\n"
            "Edit the private-only notice and every page in the beginner guide.\n\n"
            "Telegram formatting, links, and custom emojis are preserved."
        )

    @staticmethod
    def public_button_settings_text() -> str:
        return (
            "🔘 <b>Public Buttons</b>\n\n"
            "Edit every inline button visible to public users without changing what the button does.\n\n"
            "Send a regular emoji as part of the label, or select one Telegram custom emoji to use as "
            "the button icon."
        )

    def public_button_group_text(self, group: str) -> str:
        label = self.PUBLIC_BUTTON_GROUP_LABELS.get(group, "Public buttons")
        count = len(self.PUBLIC_BUTTON_GROUPS.get(group, ()))
        return (
            f"{label}\n\n"
            f"Editable buttons: <b>{count}</b>\n\n"
            "Button actions, callback identifiers, and links remain unchanged."
        )

    def public_message_group_text(self, group: str) -> str:
        label = self.PUBLIC_MESSAGE_GROUP_LABELS.get(group, "Public messages")
        description = self.PUBLIC_MESSAGE_GROUP_DESCRIPTIONS.get(
            group,
            "Edit the public messages in this section.",
        )
        count = len(self.PUBLIC_MESSAGE_GROUPS.get(group, ()))
        return (
            f"{label}\n\n"
            f"{html.escape(description)}\n\n"
            f"Editable messages: <b>{count}</b>\n"
            "Formatting, links, and custom emojis are preserved."
        )

    def broadcast_settings_text(self, note: str = "") -> str:
        suffix = f"\n\nℹ️ {html.escape(note)}" if note else ""
        return (
            "📣 <b>Broadcast</b>\n\n"
            f"Audience: <b>{len(self.subscriber_ids)} subscribed bot users</b>\n"
            f"Status: <b>{'Sending' if self.broadcast_lock.locked() else 'Ready'}</b>\n\n"
            "Create a message, review its exact preview, and confirm delivery. Custom emojis, links, "
            "bold, italic, underline, spoilers, and other Telegram formatting are preserved.\n\n"
            "Only private users who have interacted with this bot can receive broadcasts."
            f"{suffix}"
        )

    def broadcast_prompt_text(self) -> str:
        return (
            "✍️ <b>Create Broadcast</b>\n\n"
            f"Current audience: <b>{len(self.subscriber_ids)} subscribed bot users</b>\n\n"
            "Send the complete message now. You can use Telegram formatting, links, and custom emojis. "
            "Nothing is sent to users until you approve the preview.\n\n"
            "Use /cancel to stop."
        )

    async def send_message(self, chat_id: int, text: str, reply_markup: str | None = None) -> None:
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.api("sendMessage", payload)

    async def maybe_send_rate_limit_notice(self, chat_id: int, wait_seconds: float) -> None:
        now = time.monotonic()
        last_notice = self.chat_notice_times.get(chat_id, 0.0)
        if now - last_notice < 30:
            return
        self.chat_notice_times[chat_id] = now
        wait = max(1, int(wait_seconds))
        try:
            await self.send_message(
                chat_id,
                self.render_public_message(
                    "rate_limit_message",
                    {"[WAIT_SECONDS]": wait},
                ),
            )
        except Exception as exc:
            print(f"Rate-limit notice failed: {type(exc).__name__}: {exc}", flush=True)

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: str | None = None) -> None:
        payload = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self.api("editMessageText", payload)
        if not result.get("ok"):
            description = str(result.get("description") or "").lower()
            if "message is not modified" in description:
                return
            await self.send_message(chat_id, text, reply_markup=reply_markup)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:180]
        await self.api("answerCallbackQuery", payload)

    async def setup_commands(self) -> None:
        public_commands = [
            {"command": "meme", "description": "Send the latest meme price image"},
            {"command": "ath", "description": "Show a TON token's all-time high"},
            {"command": "swap", "description": "Convert GRAM to USD or a TON token"},
            {"command": "trending", "description": "Show TON meme trends for the last 24h"},
            {"command": "pulse", "description": "Show live TON meme market movements"},
            {"command": "new", "description": "Show TON tokens verified this week"},
            {"command": "help", "description": "Show all public bot functions"},
        ]
        private_commands = [
            {"command": "meme", "description": "Send the latest meme price image"},
            {"command": "ath", "description": "Show a TON token's all-time high"},
            {"command": "swap", "description": "Convert GRAM to USD or a TON token"},
            {"command": "trending", "description": "Show TON meme trends for the last 24h"},
            {"command": "pulse", "description": "Show live TON meme market movements"},
            {"command": "new", "description": "Show TON tokens verified this week"},
            {"command": "alert", "description": "Create and manage private token alerts"},
            {"command": "guide", "description": "Open the beginner TON meme coin guide"},
            {"command": "help", "description": "Show all public bot functions"},
        ]
        admin_commands = [
            {"command": "meme", "description": "Send the latest meme price image"},
            {"command": "ath", "description": "Show a TON token's all-time high"},
            {"command": "swap", "description": "Convert GRAM to USD or a TON token"},
            {"command": "trending", "description": "Show TON meme trends for the last 24h"},
            {"command": "pulse", "description": "Show live TON meme market movements"},
            {"command": "new", "description": "Show TON tokens verified this week"},
            {"command": "alert", "description": "Create and manage private token alerts"},
            {"command": "guide", "description": "Open the beginner TON meme coin guide"},
            {"command": "help", "description": "Show all public bot functions"},
            {"command": "menu", "description": "Open control menu"},
            {"command": "preview", "description": "Send dashboard preview"},
            {"command": "post", "description": "Post dashboard to channel"},
            {"command": "status", "description": "Show bot status"},
        ]
        await self.api("setMyCommands", {"commands": json.dumps(public_commands)}, timeout=60)
        await self.api(
            "setMyCommands",
            {
                "commands": json.dumps(private_commands),
                "scope": json.dumps({"type": "all_private_chats"}),
            },
            timeout=60,
        )
        for admin_id in self.admin_ids:
            try:
                await self.api(
                    "setMyCommands",
                    {
                        "commands": json.dumps(admin_commands),
                        "scope": json.dumps({"type": "chat", "chat_id": admin_id}),
                    },
                    timeout=60,
                )
            except Exception as exc:
                print(f"Admin command setup failed for {admin_id}: {type(exc).__name__}: {exc}", flush=True)

    async def validate_bot_token(self) -> None:
        result = await self.api("getMe")
        if not result.get("ok"):
            raise RuntimeError(f"Bot token rejected by Telegram: {result}")

    async def api(self, method: str, payload: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        session = await self.get_http_session()
        request_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=5, sock_read=max(5, timeout - 2))
        async with session.post(url, data=payload or {}, timeout=request_timeout) as response:
            return await response.json()

    async def api_form(self, method: str, form: aiohttp.FormData, timeout: int = 60) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        session = await self.get_http_session()
        request_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=4, sock_read=max(10, timeout - 2))
        async with session.post(url, data=form, timeout=request_timeout) as response:
            return await response.json()

    def menu_markup(self, user_id: int | None = None) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {"text": "🖼 Preview", "callback_data": "preview"},
                        {"text": "🚀 Post now", "callback_data": "post"},
                    ],
                    [
                        {"text": "📤 Publishing", "callback_data": "publishing_settings"},
                        {"text": "🔥 Trending", "callback_data": "trending_settings"},
                    ],
                    [{"text": "📊 Market Overview", "callback_data": "market_overview_settings"}],
                    [{"text": "📈 UTYA movement alerts", "callback_data": "utya_movement_settings"}],
                    [
                        {"text": "✏️ Messages", "callback_data": "message_settings"},
                        {"text": "🔐 Access", "callback_data": "access_settings"},
                    ],
                    [
                        {"text": "⚙️ System", "callback_data": "system_settings"},
                        {"text": "📊 Stats", "callback_data": "stats"},
                    ],
                    [{"text": "🔄 Refresh", "callback_data": "status"}],
                ]
            }
        )

    @staticmethod
    def usage_stats_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "💬 Active groups", "callback_data": "stats_groups"}],
                    [{"text": "🔄 Refresh stats", "callback_data": "stats"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    @staticmethod
    def usage_stats_groups_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "🔄 Refresh groups", "callback_data": "stats_groups"}],
                    [{"text": "⬅️ Back to stats", "callback_data": "stats"}],
                ]
            }
        )

    @staticmethod
    def message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "🔐 Subscription messages", "callback_data": "subscription_message_settings"}],
                    [{"text": "💬 General messages", "callback_data": "general_message_settings"}],
                    [{"text": "🪙 Token report messages", "callback_data": "token_message_settings"}],
                    [{"text": "🔄 Converter messages", "callback_data": "conversion_message_settings"}],
                    [{"text": "🪄 Inline-mode messages", "callback_data": "inline_message_settings"}],
                    [{"text": "🔥 Trending messages", "callback_data": "trending_message_settings"}],
                    [{"text": "⚡️ Pulse messages", "callback_data": "pulse_message_settings"}],
                    [{"text": "📊 Market overview messages", "callback_data": "market_overview_settings"}],
                    [{"text": "🆕 Newly verified messages", "callback_data": "new_message_settings"}],
                    [{"text": "🔔 Alert messages", "callback_data": "alert_message_settings"}],
                    [{"text": "🏳️ Guide messages", "callback_data": "guide_message_settings"}],
                    [{"text": "🔘 Public buttons", "callback_data": "public_button_settings"}],
                    [{"text": "📣 Broadcast", "callback_data": "broadcast_settings"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    @staticmethod
    def subscription_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "👋 Edit welcome message", "callback_data": "edit_welcome_message"}],
                    [{"text": "🔒 Edit subscription prompt", "callback_data": "edit_subscription_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def general_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "❓ /help", "callback_data": "edit_help_message"}],
                    [{"text": "🔒 Private /help", "callback_data": "edit_private_help_message"}],
                    [{"text": "⏳ Request limit", "callback_data": "edit_rate_limit_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def token_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "⚠️ Report unavailable", "callback_data": "edit_token_report_unavailable_message"}],
                    [{"text": "🔎 Multiple results", "callback_data": "edit_token_choices_message"}],
                    [{"text": "🚫 Token not found", "callback_data": "edit_token_not_found_message"}],
                    [{"text": "📖 ATH instructions", "callback_data": "edit_ath_usage_message"}],
                    [{"text": "🏆 ATH result", "callback_data": "edit_ath_result_message"}],
                    [{"text": "➖ ATH unavailable", "callback_data": "edit_ath_unavailable_message"}],
                    [{"text": "⚠️ ATH lookup error", "callback_data": "edit_ath_lookup_unavailable_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def conversion_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "📖 Instructions", "callback_data": "edit_conversion_usage_message"}],
                    [{"text": "⚠️ Unavailable", "callback_data": "edit_conversion_unavailable_message"}],
                    [{"text": "🚫 Token not found", "callback_data": "edit_conversion_not_found_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def inline_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "🪙 Coin statistics",
                            "callback_data": "edit_public_message:inline_coin_message",
                        }
                    ],
                    [
                        {
                            "text": "🔄 Conversion result",
                            "callback_data": "edit_public_message:inline_conversion_message",
                        }
                    ],
                    [
                        {
                            "text": "❓ Help and errors",
                            "callback_data": "edit_public_message:inline_help_message",
                        }
                    ],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def trending_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "✏️ Edit title", "callback_data": "edit_trending_title"}],
                    [{"text": "📝 Edit message layout", "callback_data": "edit_trending_message"}],
                    [{"text": "📊 Edit coin row", "callback_data": "edit_trending_row"}],
                    [{"text": "⚠️ Edit unavailable message", "callback_data": "edit_trending_unavailable_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @staticmethod
    def pulse_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "👁 Preview formatted pulse", "callback_data": "preview_pulse_format"}],
                    [{"text": "🪙 Edit coin emojis", "callback_data": "pulse_coin_emoji_settings"}],
                    [{"text": "🎨 Edit signal emojis", "callback_data": "pulse_emoji_settings"}],
                    [{"text": "✏️ Edit title", "callback_data": "edit_pulse_title"}],
                    [{"text": "📝 Edit result layout", "callback_data": "edit_pulse_message"}],
                    [{"text": "⚡️ Edit event layout", "callback_data": "edit_pulse_event"}],
                    [{"text": "😴 Edit normal-market message", "callback_data": "edit_pulse_normal_message"}],
                    [{"text": "⚠️ Edit unavailable message", "callback_data": "edit_pulse_unavailable_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    def pulse_emoji_settings_markup(self) -> str:
        rows = [
            [{"text": "👁 Preview formatted pulse", "callback_data": "preview_pulse_format"}]
        ]
        configured = getattr(self, "pulse_emojis", {})
        for key, default in self.PULSE_EMOJI_DEFAULTS.items():
            emoji = self.normalize_persisted_pulse_emoji(configured.get(key, default), default)
            custom = re.fullmatch(
                r'<tg-emoji emoji-id="[0-9]{1,32}">(?P<fallback>[^<>]{1,64})</tg-emoji>',
                emoji,
            )
            plain = html.unescape(custom.group("fallback") if custom else emoji)
            rows.append(
                [
                    {
                        "text": f"{plain} {self.PULSE_EMOJI_LABELS[key]}",
                        "callback_data": f"edit_pulse_emoji:{key}",
                    }
                ]
            )
        rows.append([{"text": "⬅️ Back to Pulse messages", "callback_data": "pulse_message_settings"}])
        return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)

    def pulse_coin_emoji_settings_markup(self) -> str:
        rows = [
            [{"text": "👁 Preview formatted pulse", "callback_data": "preview_pulse_format"}]
        ]
        configured = getattr(self, "pulse_coin_emojis", {})
        for ticker, default in self.PULSE_COIN_EMOJI_DEFAULTS.items():
            emoji = self.normalize_persisted_pulse_emoji(
                configured.get(ticker, default),
                default,
            )
            custom = re.fullmatch(
                r'<tg-emoji emoji-id="[0-9]{1,32}">(?P<fallback>[^<>]{1,64})</tg-emoji>',
                emoji,
            )
            plain = html.unescape(custom.group("fallback") if custom else emoji)
            rows.append(
                [
                    {
                        "text": f"{plain} {ticker}",
                        "callback_data": f"edit_pulse_coin_emoji:{ticker}",
                    }
                ]
            )
        rows.append([{"text": "⬅️ Back to Pulse messages", "callback_data": "pulse_message_settings"}])
        return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)

    @staticmethod
    def new_message_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "👁 Preview formatted result", "callback_data": "preview_new_format"}],
                    [{"text": "📝 Edit result layout", "callback_data": "edit_new_message"}],
                    [{"text": "🪙 Edit token row", "callback_data": "edit_new_row"}],
                    [{"text": "⚠️ Edit command instructions", "callback_data": "edit_new_usage_message"}],
                    [{"text": "🔎 Edit empty result", "callback_data": "edit_new_empty_message"}],
                    [{"text": "📡 Edit unavailable message", "callback_data": "edit_new_unavailable_message"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    @classmethod
    def alert_message_settings_markup(cls) -> str:
        rows = [
            [
                {
                    "text": cls.PUBLIC_MESSAGE_GROUP_LABELS[group],
                    "callback_data": f"public_message_group:{group}",
                }
            ]
            for group in cls.PUBLIC_MESSAGE_GROUPS
        ]
        rows.append([{"text": "⬅️ Back to messages", "callback_data": "message_settings"}])
        return json.dumps({"inline_keyboard": rows})

    @classmethod
    def public_message_group_markup(cls, group: str) -> str:
        rows: list[list[dict[str, str]]] = []
        for key in cls.PUBLIC_MESSAGE_GROUPS.get(group, ()):
            label = cls.PUBLIC_MESSAGE_LABELS.get(key, key.replace("_", " ")).title()
            rows.append(
                [
                    {
                        "text": f"✏️ {label}"[:60],
                        "callback_data": f"edit_public_message:{key}",
                    }
                ]
            )
        rows.extend(
            [
                [{"text": "⬅️ Back to alert messages", "callback_data": "alert_message_settings"}],
                [{"text": "🏠 Back to menu", "callback_data": "menu"}],
            ]
        )
        return json.dumps(
            {"inline_keyboard": rows}
        )

    @classmethod
    def guide_message_settings_markup(cls) -> str:
        rows: list[list[dict[str, str]]] = []
        for key in cls.GUIDE_MESSAGE_KEYS:
            label = cls.PUBLIC_MESSAGE_LABELS.get(key, key.replace("_", " ")).title()
            rows.append(
                [
                    {
                        "text": f"✏️ {label}"[:60],
                        "callback_data": f"edit_guide_message:{key}",
                    }
                ]
            )
        rows.append([{"text": "⬅️ Back to messages", "callback_data": "message_settings"}])
        return json.dumps({"inline_keyboard": rows})

    @classmethod
    def public_button_settings_markup(cls) -> str:
        rows = [
            [
                {
                    "text": cls.PUBLIC_BUTTON_GROUP_LABELS[group],
                    "callback_data": f"public_button_group:{group}",
                }
            ]
            for group in cls.PUBLIC_BUTTON_GROUPS
        ]
        rows.append([{"text": "⬅️ Back to messages", "callback_data": "message_settings"}])
        return json.dumps({"inline_keyboard": rows})

    @classmethod
    def public_button_group_markup(cls, group: str) -> str:
        rows: list[list[dict[str, str]]] = []
        for key in cls.PUBLIC_BUTTON_GROUPS.get(group, ()):
            label = cls.PUBLIC_BUTTON_LABELS.get(key, key.replace("_", " ")).title()
            rows.append(
                [
                    {
                        "text": f"✏️ {label}"[:60],
                        "callback_data": f"edit_public_button:{key}",
                    }
                ]
            )
        rows.extend(
            [
                [{"text": "⬅️ Back to public buttons", "callback_data": "public_button_settings"}],
                [{"text": "🏠 Back to menu", "callback_data": "menu"}],
            ]
        )
        return json.dumps({"inline_keyboard": rows})

    def broadcast_settings_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "✍️ Create broadcast", "callback_data": "compose_broadcast"}],
                    [{"text": "⬅️ Back to messages", "callback_data": "message_settings"}],
                ]
            }
        )

    def confirm_broadcast_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": f"📤 Send to {len(self.subscriber_ids)} users", "callback_data": "confirm_broadcast"}],
                    [{"text": "❌ Cancel", "callback_data": "cancel_broadcast"}],
                ]
            }
        )

    def publishing_settings_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "📡 Change posting channel", "callback_data": "edit_channel"}],
                    [{"text": "🚀 Post now", "callback_data": "post"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    def market_overview_settings_markup(self) -> str:
        toggle = "⏸ Pause scheduling" if self.market_overview_enabled else "▶️ Enable scheduling"
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": toggle, "callback_data": "toggle_market_overview"}],
                    [{"text": "📝 Generate approval now", "callback_data": "generate_market_overview"}],
                    [
                        {"text": "📡 Channel", "callback_data": "edit_market_overview_channel"},
                        {"text": "⏱ Interval", "callback_data": "edit_market_overview_interval"},
                    ],
                    [{"text": "✏️ Edit full layout", "callback_data": "edit_market_overview_message"}],
                    [{"text": "💵 Edit price row", "callback_data": "edit_market_overview_price_row"}],
                    [{"text": "📈 Edit market-cap row", "callback_data": "edit_market_overview_cap_row"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    def trending_settings_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {"text": "🔢 Coin count", "callback_data": "edit_trending_count"},
                        {"text": "💧 Liquidity", "callback_data": "edit_trending_liquidity"},
                    ],
                    [{"text": "🔄 Refresh snapshot", "callback_data": "refresh_trending_snapshot"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    def utya_movement_settings_markup(self) -> str:
        toggle = (
            "⏸️ Disable movement alerts"
            if self.utya_movement_enabled
            else "▶️ Enable movement alerts"
        )
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": toggle, "callback_data": "toggle_utya_movement_alerts"}],
                    [
                        {
                            "text": "🎯 Change threshold",
                            "callback_data": "edit_utya_movement_threshold",
                        },
                        {
                            "text": "🔄 Reset reference",
                            "callback_data": "reset_utya_movement_reference",
                        },
                    ],
                    [
                        {
                            "text": "🚀 Edit rise alert",
                            "callback_data": "edit_utya_movement_up_message",
                        },
                        {
                            "text": "🔻 Edit fall alert",
                            "callback_data": "edit_utya_movement_down_message",
                        },
                    ],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    def access_settings_markup(self) -> str:
        toggle = "🔓 Disable subscription check" if self.require_private_subscription else "🔒 Enable subscription check"
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": toggle, "callback_data": "toggle_private_subscription"}],
                    [{"text": "📢 Required channel", "callback_data": "edit_required_channel"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    def system_settings_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "🔄 Refresh dashboard image", "callback_data": "refresh_dashboard_cache"}],
                    [
                        {"text": "📡 Price refresh", "callback_data": "edit_background_refresh"},
                        {"text": "⚡ Image cache", "callback_data": "edit_image_cache"},
                    ],
                    [{"text": "🖼 Dashboard details", "callback_data": "dashboard_settings"}],
                    [{"text": "⬅️ Back to menu", "callback_data": "menu"}],
                ]
            }
        )

    @staticmethod
    def dashboard_settings_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {"text": "🖼 Preview", "callback_data": "preview"},
                        {"text": "🚀 Post now", "callback_data": "post"},
                    ],
                    [{"text": "🔄 Refresh image", "callback_data": "refresh_dashboard_cache"}],
                    [{"text": "⬅️ Back to system", "callback_data": "system_settings"}],
                ]
            }
        )

    @staticmethod
    def back_markup(callback_data: str = "menu") -> str:
        return json.dumps(
            {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": callback_data}]]}
        )

    @staticmethod
    def cancel_edit_markup() -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "Cancel", "callback_data": "cancel_message_edit"}],
                ]
            }
        )

    def token_choices_markup(self, choices) -> str:
        rows = []
        for choice in choices:
            rows.append(
                [
                    self.public_button(
                        "token_choice",
                        replacements={"[CHOICE]": choice_button_label(choice)},
                        callback_data=f"coin:{choice.token_address}",
                    )
                ]
            )
        return json.dumps({"inline_keyboard": rows})

    def ath_choices_markup(self, choices) -> str:
        rows = []
        for choice in choices:
            rows.append(
                [
                    self.public_button(
                        "token_choice",
                        replacements={"[CHOICE]": choice_button_label(choice)},
                        callback_data=f"ath:{choice.token_address}",
                    )
                ]
            )
        return json.dumps({"inline_keyboard": rows})

    def conversion_choices_markup(self, choices) -> str:
        rows = []
        for choice in choices:
            rows.append(
                [
                    self.public_button(
                        "conversion_choice",
                        replacements={"[CHOICE]": choice_button_label(choice)},
                        callback_data=f"convert:{choice.token_address}",
                    )
                ]
            )
        return json.dumps({"inline_keyboard": rows})

    def status_text(self) -> str:
        snapshot = self.trending_service.current()
        trending_count = self.current_trending_count(snapshot)
        trending_updated = self.snapshot_updated_text(snapshot)
        new_snapshot = self.new_tokens_service.current()
        new_count = len(new_snapshot.tokens) if new_snapshot is not None else 0
        new_updated = self.snapshot_updated_text(new_snapshot)
        image_status = "Ready" if self.cached_image_bytes else "Loading"
        subscription = self.required_channel if self.require_private_subscription else "Disabled"
        return (
            "🤖 <b>Meme Prices Control</b>\n\n"
            "🟢 <b>Bot online</b>\n"
            f"📡 Channel: <b>{html.escape(self.channel)}</b>\n"
            f"🖼 Dashboard: <b>{image_status}</b>\n"
            f"🔥 Trending: <b>{trending_count} coins</b> · {trending_updated}\n"
            f"🆕 Newly verified feed: <b>{new_count} tokens</b> · {new_updated}\n"
            f"📈 UTYA movement alerts: <b>{'Enabled' if self.utya_movement_enabled else 'Disabled'}</b>\n"
            f"📊 Market overview: <b>{'Enabled' if self.market_overview_enabled else 'Paused'}</b>\n"
            f"🔐 Private access: <b>{html.escape(subscription)}</b>\n\n"
            "Choose a section below. Changes are saved immediately and remain after restarts."
        )

    def usage_stats_text(self) -> str:
        users = self.usage_stats.get("users") if isinstance(self.usage_stats.get("users"), dict) else {}
        chats = self.usage_stats.get("chats") if isinstance(self.usage_stats.get("chats"), dict) else {}
        commands = (
            self.usage_stats.get("commands")
            if isinstance(self.usage_stats.get("commands"), dict)
            else {}
        )
        active_groups = sum(1 for record in chats.values() if isinstance(record, dict) and record.get("active"))
        private_users = 0
        public_chat_users = 0
        inline_users = 0
        multi_mode_users = 0
        for record in users.values():
            if not isinstance(record, dict):
                continue
            private = bool(record.get("private_chat", record.get("private")))
            public_chat = bool(record.get("public_chat"))
            inline_mode = bool(record.get("inline_mode"))
            private_users += int(private)
            public_chat_users += int(public_chat)
            inline_users += int(inline_mode)
            multi_mode_users += int(sum((private, public_chat, inline_mode)) > 1)
        tracking_started = self.format_stats_timestamp(self.usage_stats.get("tracking_started_at"))
        last_activity = self.format_stats_timestamp(self.usage_stats.get("last_activity"), fallback="No activity yet")
        return (
            "📊 <b>Bot Statistics</b>\n\n"
            "👥 <b>Unique Audience</b>\n"
            f"All unique users: <b>{len(users):,}</b>\n"
            f"Private-message users: <b>{private_users:,}</b>\n"
            f"Public/group-chat users: <b>{public_chat_users:,}</b>\n"
            f"Inline-mode users: <b>{inline_users:,}</b>\n"
            f"Users active in multiple modes: <b>{multi_mode_users:,}</b>\n"
            f"Broadcast subscribers: <b>{len(self.subscriber_ids):,}</b>\n\n"
            "💬 <b>Groups</b>\n"
            f"Active groups: <b>{active_groups:,}</b>\n"
            f"Groups seen: <b>{len(chats):,}</b>\n\n"
            "⚡ <b>Command Usage</b>\n"
            f"All commands: <b>{int(self.usage_stats.get('total_commands') or 0):,}</b>\n"
            f"Dashboard: <b>{int(commands.get('dashboard') or 0):,}</b>\n"
            f"Token reports: <b>{int(commands.get('token_report') or 0):,}</b>\n"
            f"ATH lookups: <b>{int(commands.get('ath') or 0):,}</b>\n"
            f"Conversions: <b>{int(commands.get('conversion') or 0):,}</b>\n"
            f"Trending: <b>{int(commands.get('trending') or 0):,}</b>\n"
            f"Pulse: <b>{int(commands.get('pulse') or 0):,}</b>\n"
            f"Newly verified searches: <b>{int(commands.get('new') or 0):,}</b>\n\n"
            f"🕒 Tracking since: <b>{tracking_started}</b>\n"
            f"Last activity: <b>{last_activity}</b>\n\n"
            "<i>Each mode is deduplicated separately, so one person can appear in more than one mode.</i>"
        )

    def usage_stats_groups_text(self) -> str:
        chats = self.usage_stats.get("chats") if isinstance(self.usage_stats.get("chats"), dict) else {}
        active_groups = [
            (chat_id, record)
            for chat_id, record in chats.items()
            if isinstance(record, dict) and bool(record.get("active"))
        ]
        active_groups.sort(key=lambda item: str(item[1].get("title") or "").casefold())
        if active_groups:
            lines = [
                f"{index}. <b>{html.escape(str(record.get('title') or f'Group {chat_id}'))}</b>"
                for index, (chat_id, record) in enumerate(active_groups[:30], start=1)
            ]
            if len(active_groups) > 30:
                lines.append(f"\n…and {len(active_groups) - 30:,} more active groups.")
            group_list = "\n".join(lines)
        else:
            group_list = "No active groups have been observed yet."
        return (
            "💬 <b>Active Groups</b>\n\n"
            f"Current count: <b>{len(active_groups):,}</b>\n\n"
            f"{group_list}\n\n"
            "A group becomes active when the bot receives activity there or Telegram reports that the bot joined. "
            "It becomes inactive when Telegram reports that the bot left or was removed."
        )

    @staticmethod
    def format_stats_timestamp(value: Any, fallback: str = "Now") -> str:
        raw = str(value or "").strip()
        if not raw:
            return fallback
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    def dashboard_settings_text(self, note: str = "") -> str:
        if self.cached_image_at > 0:
            cache_age = self.format_duration(max(0, int(time.monotonic() - self.cached_image_at))) + " ago"
        else:
            cache_age = "Not rendered yet"
        upload_status = "Ready" if self.current_photo_file_id() else "Uploads on next request"
        suffix = f"\n\n✅ {html.escape(note)}" if note else ""
        return (
            "🖼 <b>Dashboard</b>\n\n"
            f"📡 Destination: <b>{html.escape(self.channel)}</b>\n"
            f"🕒 Last render: <b>{cache_age}</b>\n"
            f"⚡ Telegram cache: <b>{upload_status}</b>\n\n"
            "Preview the current image, post it to the channel, or rebuild it from current market data."
            f"{suffix}"
        )

    def publishing_settings_text(self) -> str:
        return (
            "📤 <b>Publishing</b>\n\n"
            f"Channel: <b>{html.escape(self.channel)}</b>\n"
            "Mode: <b>Manual posting only</b>\n\n"
            "Use <b>Post now</b> whenever you want to publish the latest dashboard image."
        )

    def market_overview_settings_text(self, note: str = "") -> str:
        state = self.market_overview_store.state
        pending = state.get("pending")
        if isinstance(pending, dict):
            schedule_status = "Awaiting admin approval"
        elif self.market_overview_enabled:
            due_at = float(state.get("next_due_at") or 0)
            schedule_status = datetime.fromtimestamp(due_at, timezone.utc).strftime(
                "%d/%m/%Y %H:%M UTC"
            )
        else:
            schedule_status = "Paused"

        last_posted_at = float(state.get("last_posted_at") or 0)
        last_posted = (
            datetime.fromtimestamp(last_posted_at, timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
            if last_posted_at > 0
            else "Never"
        )
        suffix = f"\n\n✅ {html.escape(note)}" if note else ""
        return (
            "📊 <b>Market Overview</b>\n\n"
            f"Status: <b>{'Enabled' if self.market_overview_enabled else 'Paused'}</b>\n"
            f"Destination: <b>{html.escape(self.market_overview_channel)}</b>\n"
            f"Interval: <b>{format_interval(self.market_overview_interval_minutes)}</b>\n"
            f"Next action: <b>{schedule_status}</b>\n"
            f"Last posted: <b>{last_posted}</b>\n\n"
            "At each interval the bot prepares a live nine-coin overview and sends it privately "
            "to authorized admins. It is posted only after one admin approves it. A second admin "
            "cannot post the same proposal twice."
            f"{suffix}"
        )

    def trending_settings_text(self, note: str = "") -> str:
        snapshot = self.trending_service.current()
        suffix = f"\n\n✅ {html.escape(note)}" if note else ""
        return (
            "🔥 <b>Trending Settings</b>\n\n"
            f"Title: {self.trending_title}\n"
            f"Coins shown: <b>{self.trending_service.result_limit}</b>\n"
            f"Minimum liquidity: <b>${self.trending_service.minimum_liquidity_usd:,.2f}</b>\n"
            f"Snapshot: <b>{self.current_trending_count(snapshot)} eligible coins</b>\n"
            f"Updated: <b>{self.snapshot_updated_text(snapshot)}</b>\n\n"
            "The ranking refreshes hourly or when you use the manual refresh button."
            f"{suffix}"
        )

    def utya_movement_settings_text(self, note: str = "") -> str:
        tracker = self.utya_movement_tracker
        state = "🟢 Enabled" if self.utya_movement_enabled else "🔴 Disabled"
        reference = (
            format_price(tracker.reference_price)
            if tracker.reference_price is not None
            else "Waiting for next price"
        )
        latest = (
            format_price(tracker.last_observed_price)
            if tracker.last_observed_price is not None
            else "Not checked yet"
        )
        last_check = self.format_stats_timestamp(
            tracker.last_observed_at,
            fallback="Not checked yet",
        )
        last_alert = self.format_stats_timestamp(
            tracker.last_alert_at,
            fallback="No alerts sent yet",
        )
        suffix = f"\n\n✅ {html.escape(note)}" if note else ""
        return (
            "📈 <b>UTYA Movement Alerts</b>\n\n"
            f"Status: <b>{state}</b>\n"
            f"Destination: <b>{self.UTYA_MOVEMENT_CHANNEL}</b>\n"
            f"Trigger: <b>±{self.utya_movement_threshold_percent:g}%</b>\n"
            f"Reference price: <b>{reference}</b>\n"
            f"Latest price: <b>{latest}</b>\n"
            f"Last check: <b>{last_check}</b>\n"
            f"Last alert: <b>{last_alert}</b>\n"
            f"Alerts delivered: <b>{tracker.alerts_sent}</b>\n\n"
            "A rise or fall is measured from the last successfully delivered alert price. "
            "The reference is persisted across restarts."
            f"{suffix}"
        )

    def access_settings_text(self) -> str:
        state = "🟢 Required" if self.require_private_subscription else "🔴 Disabled"
        return (
            "🔐 <b>Access Settings</b>\n\n"
            f"Private subscription check: <b>{state}</b>\n"
            f"Required channel: <b>{html.escape(self.required_channel)}</b>\n"
            "Group commands: <b>Available without a subscription check</b>\n"
            "Administrators: <b>Always allowed</b>\n\n"
            "This setting affects private chats only."
        )

    def system_settings_text(self) -> str:
        return (
            "⚙️ <b>System Settings</b>\n\n"
            "🟢 Service: <b>Online</b>\n"
            f"📡 Price refresh: <b>{self.format_duration(self.background_refresh_seconds)}</b>\n"
            f"⚡ Image cache: <b>{self.format_duration(self.image_cache_seconds)}</b>\n"
            f"🚀 Parallel image sends: <b>{self.meme_send_concurrency}</b>\n"
            f"👥 Administrators: <b>{len(self.admin_ids)}</b>\n\n"
            "The cached Telegram image keeps public responses fast during simultaneous requests."
        )

    def current_trending_count(self, snapshot: TrendingSnapshot | None) -> int:
        if snapshot is None:
            return 0
        return len(
            [
                coin
                for coin in snapshot.coins
                if coin.liquidity_usd >= self.trending_service.minimum_liquidity_usd
                and not self.trending_service.is_blocked(coin)
            ][: self.trending_service.result_limit]
        )

    @staticmethod
    def snapshot_updated_text(snapshot: TrendingSnapshot | None) -> str:
        if snapshot is None:
            return "Not available"
        return snapshot.updated_at.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    @staticmethod
    def format_duration(seconds: int | float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        hours, remainder = divmod(total, 3_600)
        minutes = remainder // 60
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"

    def settings_markup_for_key(self, key: str) -> str:
        if key.startswith(self.PULSE_COIN_EMOJI_SETTING_PREFIX):
            return self.pulse_coin_emoji_settings_markup()
        if key.startswith(self.PULSE_EMOJI_SETTING_PREFIX):
            return self.pulse_emoji_settings_markup()
        if key.startswith("public_button:"):
            button_key = key.removeprefix("public_button:")
            return self.public_button_group_markup(self.public_button_group_for_key(button_key))
        if key in {
            "utya_movement_up_message",
            "utya_movement_down_message",
            "utya_movement_threshold_percent",
        }:
            return self.utya_movement_settings_markup()
        public_group = self.public_message_group_for_key(key)
        if public_group:
            return self.public_message_group_markup(public_group)
        if key in self.GUIDE_MESSAGE_KEYS:
            return self.guide_message_settings_markup()
        if key in self.INLINE_MESSAGE_KEYS:
            return self.inline_message_settings_markup()
        if key in {"welcome_message", "subscription_message"}:
            return self.subscription_message_settings_markup()
        if key in {"help_message", "private_help_message", "rate_limit_message"}:
            return self.general_message_settings_markup()
        if key in {
            "token_report_unavailable_message",
            "token_choices_message",
            "token_not_found_message",
            "ath_usage_message",
            "ath_result_message",
            "ath_unavailable_message",
            "ath_lookup_unavailable_message",
        }:
            return self.token_message_settings_markup()
        if key in {"conversion_usage_message", "conversion_unavailable_message", "conversion_not_found_message"}:
            return self.conversion_message_settings_markup()
        if key in {"trending_title", "trending_message", "trending_row", "trending_unavailable_message"}:
            return self.trending_message_settings_markup()
        if key in {
            "pulse_title",
            "pulse_message",
            "pulse_event",
            "pulse_normal_message",
            "pulse_unavailable_message",
        }:
            return self.pulse_message_settings_markup()
        if key in {
            "market_overview_message",
            "market_overview_price_row",
            "market_overview_cap_row",
            "market_overview_channel",
            "market_overview_interval_minutes",
        }:
            return self.market_overview_settings_markup()
        if key in {
            "new_message",
            "new_row",
            "new_usage_message",
            "new_empty_message",
            "new_unavailable_message",
        }:
            return self.new_message_settings_markup()
        if key in {
            "alert_home_message",
            "alert_search_message",
            "alert_confirmation_message",
            "alert_trigger_message",
            "alert_help_message",
        }:
            return self.alert_message_settings_markup()
        if key == "broadcast_message":
            return self.broadcast_settings_markup()
        if key == "channel":
            return self.publishing_settings_markup()
        if key == "required_channel":
            return self.access_settings_markup()
        if key in {"trending_result_limit", "trending_min_liquidity_usd"}:
            return self.trending_settings_markup()
        if key in {"background_refresh_seconds", "image_cache_seconds"}:
            return self.system_settings_markup()
        return self.menu_markup()

    def settings_text_for_key(self, key: str) -> str:
        if key.startswith(self.PULSE_COIN_EMOJI_SETTING_PREFIX):
            return self.pulse_coin_emoji_settings_text()
        if key.startswith(self.PULSE_EMOJI_SETTING_PREFIX):
            return self.pulse_emoji_settings_text()
        if key.startswith("public_button:"):
            button_key = key.removeprefix("public_button:")
            return self.public_button_group_text(self.public_button_group_for_key(button_key))
        if key in {
            "utya_movement_up_message",
            "utya_movement_down_message",
            "utya_movement_threshold_percent",
        }:
            return self.utya_movement_settings_text()
        public_group = self.public_message_group_for_key(key)
        if public_group:
            return self.public_message_group_text(public_group)
        if key in self.GUIDE_MESSAGE_KEYS:
            return self.guide_message_settings_text()
        if key in self.INLINE_MESSAGE_KEYS:
            return self.inline_message_settings_text()
        if key in {"welcome_message", "subscription_message"}:
            return self.subscription_message_settings_text()
        if key in {"help_message", "private_help_message", "rate_limit_message"}:
            return self.general_message_settings_text()
        if key in {
            "token_report_unavailable_message",
            "token_choices_message",
            "token_not_found_message",
            "ath_usage_message",
            "ath_result_message",
            "ath_unavailable_message",
            "ath_lookup_unavailable_message",
        }:
            return self.token_message_settings_text()
        if key in {"conversion_usage_message", "conversion_unavailable_message", "conversion_not_found_message"}:
            return self.conversion_message_settings_text()
        if key in {"trending_title", "trending_message", "trending_row", "trending_unavailable_message"}:
            return self.trending_message_settings_text()
        if key in {
            "pulse_title",
            "pulse_message",
            "pulse_event",
            "pulse_normal_message",
            "pulse_unavailable_message",
        }:
            return self.pulse_message_settings_text()
        if key in {
            "market_overview_message",
            "market_overview_price_row",
            "market_overview_cap_row",
            "market_overview_channel",
            "market_overview_interval_minutes",
        }:
            return self.market_overview_settings_text()
        if key in {
            "new_message",
            "new_row",
            "new_usage_message",
            "new_empty_message",
            "new_unavailable_message",
        }:
            return self.new_message_settings_text()
        if key in {
            "alert_home_message",
            "alert_search_message",
            "alert_confirmation_message",
            "alert_trigger_message",
            "alert_help_message",
        }:
            return self.alert_message_settings_text()
        if key == "broadcast_message":
            return self.broadcast_settings_text()
        if key == "channel":
            return self.publishing_settings_text()
        if key == "required_channel":
            return self.access_settings_text()
        if key in {"trending_result_limit", "trending_min_liquidity_usd"}:
            return self.trending_settings_text()
        if key in {"background_refresh_seconds", "image_cache_seconds"}:
            return self.system_settings_text()
        return self.status_text()

    @classmethod
    def public_message_group_for_key(cls, key: str) -> str:
        for group, keys in cls.PUBLIC_MESSAGE_GROUPS.items():
            if key in keys:
                return group
        return ""

    @classmethod
    def public_button_group_for_key(cls, key: str) -> str:
        for group, keys in cls.PUBLIC_BUTTON_GROUPS.items():
            if key in keys:
                return group
        return ""

    def authorized(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def meme_rate_limit_wait(self, chat_id: int) -> float:
        now = time.monotonic()

        last_chat_request = self.chat_meme_times.get(chat_id, 0.0)
        chat_wait = self.chat_cooldown_seconds - (now - last_chat_request)
        if chat_wait > 0:
            return chat_wait

        while self.global_meme_times and now - self.global_meme_times[0] >= 60:
            self.global_meme_times.popleft()
        if len(self.global_meme_times) >= self.global_limit_per_minute:
            return 60 - (now - self.global_meme_times[0])

        return 0.0

    def record_meme_request(self, chat_id: int) -> None:
        now = time.monotonic()
        self.chat_meme_times[chat_id] = now
        self.global_meme_times.append(now)

    @staticmethod
    def _command_name(text: str) -> str:
        if not text.startswith("/"):
            return ""
        token = text.split(maxsplit=1)[0].strip().lower()
        return token.split("@", 1)[0]

    @staticmethod
    def _command_args(text: str) -> str:
        if not text.startswith("/"):
            return ""
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def conversion_disabled_in_chat(self, chat: dict[str, Any]) -> bool:
        if str(chat.get("type") or "") not in {"group", "supergroup"}:
            return False
        chat_id = int(chat.get("id") or 0)
        username = str(chat.get("username") or "").strip().lstrip("@").casefold()
        disabled_ids = getattr(self, "convert_disabled_chat_ids", set())
        disabled_usernames = getattr(self, "convert_disabled_chat_usernames", set())
        return chat_id in disabled_ids or bool(username and username in disabled_usernames)

    @staticmethod
    def _parse_chat_restrictions(raw: str) -> tuple[set[int], set[str]]:
        chat_ids: set[int] = set()
        usernames: set[str] = set()
        for part in str(raw or "").replace(";", ",").split(","):
            value = part.strip()
            if not value:
                continue
            try:
                chat_ids.add(int(value))
            except ValueError:
                username = value.lstrip("@").casefold()
                if username:
                    usernames.add(username)
        return chat_ids, usernames

    @staticmethod
    def _parse_csv_values(raw: str) -> set[str]:
        return {
            value.strip()
            for value in str(raw or "").replace(";", ",").split(",")
            if value.strip()
        }

    @staticmethod
    def _parse_admin_ids(raw: str) -> set[int]:
        ids: set[int] = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        return ids


async def async_main() -> None:
    bot = TelegramDashboardBot()
    await bot.run_forever()


if __name__ == "__main__":
    asyncio.run(async_main())
