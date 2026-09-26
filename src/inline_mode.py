from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from .formatting import format_change, format_compact_number, format_price


FEATURED_INLINE_SYMBOLS = (
    "GRAM",
    "UTYA",
    "REDO",
    "SCAT",
    "YODA",
    "CHERRY",
    "BCHERRY",
    "MTONGA",
    "GROYP",
    "GRAMMING",
    "GRM",
)
INLINE_ATH_HIDDEN_SYMBOLS = frozenset({"BCHERRY"})
INLINE_COIN_NAMES = {
    "GRAM": "Gram",
    "UTYA": "Utya",
    "REDO": "Resistance Dog",
    "SCAT": "Scared Cats",
    "YODA": "Baby Yoda",
    "CHERRY": "Hot Cherry",
    "BCHERRY": "black cherry",
    "MTONGA": "Make TON Great Again",
    "GROYP": "Groyper",
    "GRAMMING": "gramming",
    "GRM": "Grm",
}
MP_LOGO_URL = (
    "https://raw.githubusercontent.com/dev-nic-codes/meme-price-bot/"
    "main/assets/mp-logo-square-v2.png"
)
MP_LOGO_RESULT_VERSION = "mp5"
INLINE_LOGO_URLS = {
    "GRAM": (
        "https://coin-images.coingecko.com/coins/images/17980/small/"
        "Gram_Circular_Badge.png?1781524778"
    ),
    "UTYA": (
        "https://cdn.dexscreener.com/cms/images/"
        "7ec9939bd49d2e0fc45ce358b6791378521ccfdfc3d9ca0e26f66091158a5e14"
        "?width=256&height=256&quality=95&format=png"
    ),
    "REDO": (
        "https://cdn.dexscreener.com/cms/images/"
        "42d2fbacb4665333ff1056ee4568c9d396dd922f833d560260ee05579c8b673c"
        "?width=256&height=256&quality=95&format=png"
    ),
    "SCAT": (
        "https://cdn.dexscreener.com/cms/images/uAPtctTcFMxscYo1"
        "?width=256&height=256&quality=95&format=png"
    ),
    "YODA": (
        "https://cdn.dexscreener.com/cms/images/9aYWxnLONSmiWQxB"
        "?width=256&height=256&quality=95&format=png"
    ),
    "CHERRY": (
        "https://cdn.dexscreener.com/cms/images/"
        "929873cb43a8273107fffbaf1e430c6ccc52a1c4d8f67bc7429ec42f24a839b6"
        "?width=256&height=256&quality=95&format=png"
    ),
    "BCHERRY": (
        "https://cdn.dexscreener.com/cms/images/DgC7qun2zFdyM9UP"
        "?width=256&height=256&quality=95&format=auto"
    ),
    "MTONGA": (
        "https://cdn.dexscreener.com/cms/images/1WQ1hE3OJZ-Yc9sC"
        "?width=256&height=256&quality=95&format=png"
    ),
    "GROYP": (
        "https://cdn.dexscreener.com/cms/images/xEyfj0HXpH4Fmbd8"
        "?width=256&height=256&quality=95&format=png"
    ),
    "GRAMMING": (
        "https://cdn.dexscreener.com/cms/images/8-n2CVGAIP8YlA7y"
        "?width=256&height=256&quality=95&format=png"
    ),
    "GRM": "https://gramcoin.org/img/icon.png",
}
INLINE_TOKEN_ALIASES = {
    "gram": "GRAM",
    "ton": "GRAM",
    "toncoin": "GRAM",
    "utya": "UTYA",
    "redo": "REDO",
    "resistance": "REDO",
    "resistance dog": "REDO",
    "scat": "SCAT",
    "scared cat": "SCAT",
    "scared cats": "SCAT",
    "yoda": "YODA",
    "baby yoda": "YODA",
    "cherry": "CHERRY",
    "hot cherry": "CHERRY",
    "bcherry": "BCHERRY",
    "black cherry": "BCHERRY",
    "mtonga": "MTONGA",
    "make ton great again": "MTONGA",
    "groyp": "GROYP",
    "groyper": "GROYP",
    "gramming": "GRAMMING",
    "grm": "GRM",
    "usd": "USD",
}
MAX_INLINE_CONVERSION_AMOUNT = Decimal("1000000000000000")
CONVERSION_RE = re.compile(
    r"^\s*(?P<amount>[0-9][0-9_,]*(?:\.[0-9]+)?)\s+"
    r"(?P<source>\$?[A-Za-z][A-Za-z0-9_-]*)\s+"
    r"(?:to|into|in|->|=)\s+"
    r"(?P<target>\$?[A-Za-z][A-Za-z0-9_-]*)\s*$",
    flags=re.IGNORECASE,
)
CONVERSION_HINT_RE = re.compile(r"(?:\s(?:to|into|in)\s|\s*->\s*|\s*=\s*)", flags=re.IGNORECASE)

DEFAULT_INLINE_COIN_MESSAGE = (
    "💎 <b>[TOKEN_NAME] ([TOKEN_SYMBOL])</b>\n\n"
    "💵 Price: <code>[PRICE]</code>\n"
    "📊 24h: <b>[CHANGE_24H]</b>\n"
    "🏆 ATH: <b>[ATH_PRICE]</b>\n"
    "👥 Holders: <b>[HOLDERS]</b>\n"
    "💰 Market cap: <b>[MARKET_CAP]</b>\n\n"
    "<i>Live market snapshot via @memesbot</i>"
)
DEFAULT_INLINE_CONVERSION_MESSAGE = (
    "🔄 <b>Conversion</b>\n\n"
    "<code>[SOURCE_AMOUNT] [SOURCE_SYMBOL]</code> = "
    "<code>[TARGET_AMOUNT] [TARGET_SYMBOL]</code>"
)
DEFAULT_INLINE_HELP_MESSAGE = (
    "💎 <b>Meme Prices inline mode</b>\n\n"
    "Type <code>@memesbot</code> to view GRAM, UTYA, REDO, SCAT, YODA, "
    "CHERRY, BCHERRY, MTONGA, GROYP, GRAMMING, and GRM.\n\n"
    "Conversion examples:\n"
    "• <code>@memesbot 100 TON to UTYA</code>\n"
    "• <code>@memesbot 1000 UTYA to REDO</code>\n"
    "• <code>@memesbot 50 USD to GRAM</code>\n"
    "• <code>@memesbot 50 GROYP to USD</code>\n\n"
    "ℹ️ [NOTE]"
)


@dataclass(frozen=True)
class InlineCoin:
    symbol: str
    name: str
    price_usd: float | None
    change_24h: float | None
    market_cap: float | None
    holders: int | None
    logo_url: str
    ath_price: float | None = None
    native: bool = False


@dataclass(frozen=True)
class InlineConversion:
    amount: Decimal
    source: str
    target: str


@dataclass(frozen=True)
class InlineMessageTemplates:
    coin: str = DEFAULT_INLINE_COIN_MESSAGE
    conversion: str = DEFAULT_INLINE_CONVERSION_MESSAGE
    help: str = DEFAULT_INLINE_HELP_MESSAGE


DEFAULT_INLINE_MESSAGE_TEMPLATES = InlineMessageTemplates()


def build_inline_results(
    raw_query: str,
    coins: tuple[InlineCoin, ...],
    templates: InlineMessageTemplates | None = None,
) -> list[dict[str, object]]:
    message_templates = templates or DEFAULT_INLINE_MESSAGE_TEMPLATES
    query = " ".join(str(raw_query or "").strip().split())
    by_symbol = {coin.symbol.upper(): coin for coin in coins}

    if not query:
        return [
            coin_result(by_symbol[symbol], message_templates)
            for symbol in FEATURED_INLINE_SYMBOLS
            if symbol in by_symbol
        ] or [
            help_result(
                "Live prices are temporarily unavailable. Please try again.",
                message_templates,
            )
        ]

    if CONVERSION_HINT_RE.search(query):
        try:
            conversion = parse_inline_conversion(query)
            return [conversion_result(conversion, by_symbol, message_templates)]
        except ValueError as exc:
            return [help_result(str(exc), message_templates)]

    normalized = normalize_inline_symbol(query)
    if normalized in by_symbol:
        return [coin_result(by_symbol[normalized], message_templates)]

    matches = [
        coin
        for coin in coins
        if query.casefold() in coin.symbol.casefold()
        or query.casefold() in coin.name.casefold()
    ]
    if matches:
        order = {symbol: index for index, symbol in enumerate(FEATURED_INLINE_SYMBOLS)}
        return [
            coin_result(coin, message_templates)
            for coin in sorted(matches, key=lambda coin: order.get(coin.symbol, 99))
        ]

    return [
        help_result(
            "Search a supported coin or enter a conversion.",
            message_templates,
        )
    ]


def parse_inline_conversion(raw_query: str) -> InlineConversion:
    match = CONVERSION_RE.fullmatch(str(raw_query or ""))
    if match is None:
        raise ValueError("Use a format like: 100 GRAM to UTYA")

    amount_text = match.group("amount").replace(",", "").replace("_", "")
    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise ValueError("The conversion amount must be a valid number.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("The conversion amount must be greater than zero.")
    if amount > MAX_INLINE_CONVERSION_AMOUNT:
        raise ValueError("The conversion amount is too large.")

    source = normalize_inline_symbol(match.group("source"))
    target = normalize_inline_symbol(match.group("target"))
    if source is None or target is None:
        raise ValueError(
            "Conversions support GRAM, UTYA, REDO, SCAT, YODA, CHERRY, BCHERRY, MTONGA, "
            "GROYP, GRAMMING, GRM, and USD."
        )
    return InlineConversion(amount=amount, source=source, target=target)


def normalize_inline_symbol(raw_value: str) -> str | None:
    normalized = " ".join(str(raw_value or "").strip().lstrip("$").casefold().split())
    return INLINE_TOKEN_ALIASES.get(normalized)


def coin_result(
    coin: InlineCoin,
    templates: InlineMessageTemplates | None = None,
) -> dict[str, object]:
    message_templates = templates or DEFAULT_INLINE_MESSAGE_TEMPLATES
    price = format_price(coin.price_usd)
    change = change_text(coin.change_24h)
    holders = holders_text(coin)
    market_cap = market_cap_text(coin.market_cap)
    ath = format_price(coin.ath_price)
    coin_template = message_templates.coin
    omitted_placeholders: list[str] = []
    if coin.native:
        omitted_placeholders.extend(("[HOLDERS]", "[MARKET_CAP]"))
    if coin.symbol.upper() in INLINE_ATH_HIDDEN_SYMBOLS:
        omitted_placeholders.append("[ATH_PRICE]")
    if omitted_placeholders:
        coin_template = omit_placeholder_lines(
            coin_template,
            tuple(omitted_placeholders),
        )
    message = render_inline_message(
        coin_template,
        {
            "[TOKEN_NAME]": coin.name,
            "[TOKEN_SYMBOL]": coin.symbol.upper(),
            "[PRICE]": price,
            "[CHANGE_24H]": change,
            "[ATH_PRICE]": ath,
            "[HOLDERS]": holders,
            "[MARKET_CAP]": market_cap,
        },
    )
    description = f"24h {format_change(coin.change_24h)}"
    if coin.symbol.upper() not in INLINE_ATH_HIDDEN_SYMBOLS:
        description += f" · ATH {ath}"
    if not coin.native:
        description += f" · MCAP {market_cap} · Holders {holders}"
    return article_result(
        result_id=f"coin:{coin.symbol.casefold()}",
        title=f"{coin.symbol.upper()} · {price}",
        description=description,
        message=message,
        logo_url=coin.logo_url,
    )


def conversion_result(
    conversion: InlineConversion,
    by_symbol: dict[str, InlineCoin],
    templates: InlineMessageTemplates | None = None,
) -> dict[str, object]:
    message_templates = templates or DEFAULT_INLINE_MESSAGE_TEMPLATES
    source_price = conversion_price(conversion.source, by_symbol)
    target_price = conversion_price(conversion.target, by_symbol)
    if source_price is None or target_price is None or source_price <= 0 or target_price <= 0:
        raise ValueError("A live price required for this conversion is temporarily unavailable.")

    with localcontext() as context:
        context.prec = 50
        converted = conversion.amount * source_price / target_price
        unit_rate = source_price / target_price

    source_amount = format_decimal_amount(conversion.amount)
    target_amount = format_decimal_amount(converted)
    unit_amount = format_decimal_amount(unit_rate)
    source = conversion.source
    target = conversion.target
    message = render_inline_message(
        message_templates.conversion,
        {
            "[SOURCE_AMOUNT]": source_amount,
            "[SOURCE_SYMBOL]": source,
            "[TARGET_AMOUNT]": target_amount,
            "[TARGET_SYMBOL]": target,
            "[UNIT_RATE]": unit_amount,
        },
    )
    logo_url = conversion_logo_url(conversion, by_symbol)
    digest = hashlib.sha256(
        f"{conversion.amount}:{source}:{target}:{source_price}:{target_price}".encode("utf-8")
    ).hexdigest()[:28]
    result_version = f"{MP_LOGO_RESULT_VERSION}:" if logo_url == MP_LOGO_URL else ""
    return article_result(
        result_id=f"convert:{result_version}{digest}",
        title=f"{source_amount} {source} → {target_amount} {target}",
        description=f"Convert {source} to {target}",
        message=message,
        logo_url=logo_url,
    )


def conversion_price(symbol: str, by_symbol: dict[str, InlineCoin]) -> Decimal | None:
    if symbol == "USD":
        return Decimal("1")
    coin = by_symbol.get(symbol)
    if coin is None or coin.price_usd is None or coin.price_usd <= 0:
        return None
    return Decimal(str(coin.price_usd))


def conversion_logo_url(
    conversion: InlineConversion,
    by_symbol: dict[str, InlineCoin],
) -> str:
    for symbol in (conversion.target, conversion.source):
        coin = by_symbol.get(symbol)
        if coin is not None and coin.logo_url:
            return coin.logo_url
    return MP_LOGO_URL


def help_result(
    note: str,
    templates: InlineMessageTemplates | None = None,
) -> dict[str, object]:
    message_templates = templates or DEFAULT_INLINE_MESSAGE_TEMPLATES
    safe_note = str(note or "").strip() or "Choose a coin or enter a conversion."
    message = render_inline_message(
        message_templates.help,
        {"[NOTE]": safe_note},
    )
    digest = hashlib.sha256(safe_note.encode("utf-8")).hexdigest()[:28]
    return article_result(
        result_id=f"help:{MP_LOGO_RESULT_VERSION}:{digest}",
        title="Inline prices and conversions",
        description=safe_note,
        message=message,
        logo_url=MP_LOGO_URL,
    )


def render_inline_message(template: str, replacements: dict[str, object]) -> str:
    rendered = str(template or "")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, html.escape(str(value)))
    return rendered


def omit_placeholder_lines(template: str, placeholders: tuple[str, ...]) -> str:
    retained_placeholders = tuple(
        placeholder
        for placeholder in (
            "[TOKEN_NAME]",
            "[TOKEN_SYMBOL]",
            "[PRICE]",
            "[CHANGE_24H]",
            "[ATH_PRICE]",
        )
        if placeholder not in placeholders
    )
    field_patterns = {
        "[ATH_PRICE]": re.compile(
            r"(?:🏆\s*)?ath\s*:?\s*(?:<[^>]+>\s*)*"
            r"\[ATH_PRICE\](?:\s*</[^>]+>)*",
            flags=re.IGNORECASE,
        ),
        "[HOLDERS]": re.compile(
            r"(?:👥\s*)?holders?\s*:?\s*\[HOLDERS\]",
            flags=re.IGNORECASE,
        ),
        "[MARKET_CAP]": re.compile(
            r"(?:💰\s*)?(?:market\s*cap|mcap)\s*:?\s*\[MARKET_CAP\]",
            flags=re.IGNORECASE,
        ),
    }
    rendered_lines: list[str] = []
    for line in str(template or "").splitlines():
        if not any(placeholder in line for placeholder in placeholders):
            rendered_lines.append(line)
            continue
        if not any(placeholder in line for placeholder in retained_placeholders):
            continue

        rendered_line = line
        for placeholder in placeholders:
            pattern = field_patterns.get(placeholder)
            if pattern is not None:
                rendered_line = pattern.sub("", rendered_line)
            rendered_line = rendered_line.replace(placeholder, "")
        rendered_line = re.sub(
            r"\s*(?:[|·•])\s*(?=(?:[|·•])|$)",
            "",
            rendered_line,
        ).rstrip()
        rendered_line = re.sub(r"(?<=\S)([|·•])", r" \1", rendered_line)
        if rendered_line:
            rendered_lines.append(rendered_line)
    return "\n".join(rendered_lines)


def article_result(
    *,
    result_id: str,
    title: str,
    description: str,
    message: str,
    logo_url: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "article",
        "id": result_id[:64],
        "title": title[:256],
        "description": description[:512],
        "input_message_content": {
            "message_text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    }
    if str(logo_url or "").startswith("https://"):
        result["thumbnail_url"] = logo_url
        result["thumbnail_width"] = 96
        result["thumbnail_height"] = 96
    return result


def change_text(value: float | None) -> str:
    if value is None:
        return "⚪ —"
    if value > 0:
        return f"🟢 ▲ {format_change(value)}"
    if value < 0:
        return f"🔴 ▼ {format_change(value)}"
    return "⚪ • 0.00%"


def holders_text(coin: InlineCoin) -> str:
    if coin.native:
        return "Native coin"
    if coin.holders is None:
        return "Unavailable"
    return f"{coin.holders:,}"


def market_cap_text(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${format_compact_number(value)}"


def format_decimal_amount(value: Decimal) -> str:
    absolute = abs(value)
    if absolute >= 1_000:
        places = 2
    elif absolute >= 1:
        places = 6
    elif absolute >= Decimal("0.0001"):
        places = 8
    else:
        places = 12
    rendered = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    if rendered == "0" and value != 0:
        return "<0.000000000001"
    return rendered
