# Meme Price Dashboard Bot

Generates a 1920x1080 PNG crypto dashboard with Pillow and can post it to Telegram.

## Local render

```bash
pip install -r requirements.txt
python main.py
```

Output:

```text
output/dashboard.png
```

## Assets

Put real logos here:

```text
assets/logos/utya.png
assets/logos/redo.png
assets/logos/scat.png
assets/logos/yoda.png
assets/logos/cherry.png
assets/logos/mtonga.png
```

Put fonts here:

```text
assets/fonts/Inter-Regular.ttf
assets/fonts/Inter-Bold.ttf
```

If assets are missing, the renderer uses safe fallbacks so the project still runs.

## Telegram

Create `.env` from `.env.example`, then run:

```bash
python -m src.telegram_bot
```

## Inline mode

Enable inline mode for `@memesbot` with BotFather's `/setinline` command.
Once enabled:

```text
@memesbot
@memesbot utya
@memesbot 100 gram to utya
@memesbot 1000 utya to redo
@memesbot 100 usd to groyp
@memesbot 100 groyp to usd
```

An empty query returns GRAM first, followed by UTYA, REDO, SCAT, YODA, CHERRY,
BCHERRY, MTONGA, GROYP, GRAMMING, and GRM. Selecting a coin sends its current USD
price, 24-hour change, provider-recorded ATH, holder count, and market cap.
BCHERRY omits ATH because no verified ATH information is available for it.
Every inline result list has a native Telegram header button labeled
`What can this bot do?`; it opens the bot privately and starts the normal welcome flow.
Conversions support every listed coin plus USD in either direction. Inline queries use the latest persisted price
and holder snapshots immediately, so a slow or rate-limited provider does not
block the response. Generic help/error results use the MP logo rather than a
coin logo. The square, crop-safe MP thumbnail is served from this repository so
Telegram can fetch it without depending on a profile-photo mirror.
`TON` and `TONCOIN` are treated as aliases for GRAM.

Admins can customize every message inserted by inline mode from
`/menu` → `Messages` → `Inline-mode messages`. The three templates cover coin
statistics, conversion results, and help/errors. Required live-data placeholders
are validated before saving, and Telegram formatting and custom emojis are
preserved.

Inline mode is also documented in both public and private `/help`, and as a
dedicated `Inline Mode` category in `/guide`. The guide page covers empty-query
prices, coin search, shared statistics, every conversion direction, and the
TON/TONCOIN aliases. Its page text and navigation-button label are editable from
the existing admin message and public-button editors.

## Newly verified TON tokens

`/new` shows the five newest tokens added to Tonkeeper's reviewed TON asset list during the
rolling seven-day window, ordered by verification-list addition time. It does
not use pool launch time. Each compact result links the token name to its
strongest live pool, shows provider-reported market cap (or clearly labeled FDV
when market cap is unavailable), and includes the holder count when TONAPI makes it available. The default row
does not repeat verification age or a separate DEX/link line.

Raw jetton-master addresses are converted to canonical friendly TON addresses
before market lookup. Holder counts come from TONAPI's per-jetton metadata
endpoint because its bulk metadata response can lag for newly indexed tokens.

The command has one public form: `/new`. It always covers the last seven days,
has no user-supplied age or valuation filters, has no minimum-liquidity cutoff,
and returns up to five results. A reviewed-list addition starts as verified;
TONAPI is then used for current metadata and to remove any token now marked
`graylist` or `blacklist`.
Stablecoin/native-asset lookalikes are also excluded. Provider verification is
a classification, not a guarantee that a token is safe.

The bot reconstructs the seven-day verification window from the official
`tonkeeper/ton-assets` Git history every six hours. Pool charts, liquidity, and
aggregate 24-hour volume refresh independently every five minutes from
DexScreener with GeckoTerminal fallback; TONAPI supplies verification status and
holders. The last valid market fields survive temporary provider failures instead
of being replaced with dashes. A week with no new list additions
is stored as a valid empty snapshot. `/new` is registered in public, private,
and admin Telegram command menus, documented in `/help` and a dedicated
`/guide` page, and counted in usage statistics. Its result layout, repeated
token row, command-usage response, empty state, and unavailable state are editable from
`/menu` → `Messages` → `Newly verified messages`.

## TON meme pulse

`/pulse` returns a compact, market-wide snapshot of significant activity across
the bot's tracked TON meme coins. It is deliberately separate from `/trending`:
trending ranks discovery candidates, while pulse looks for current events in the
existing verified dashboard registry.

GRAM is also tracked as a price-only pulse asset. It can produce price-spike or
price-drop events from its USD price history, but it is excluded from holder,
buyer, whale-buy, volume, liquidity, market-cap, and 24-hour high/low signals.

The bot now builds one shared market feed continuously instead of starting a
market search for each command. The background pulse cycle refreshes once per
minute by default, records a rolling two-hour event timeline, and combines price
changes, transaction counts, volume, liquidity, market cap, actual pool trades,
and the authoritative holder cache. `/pulse` reads that completed shared feed,
so its data is identical in private messages, groups, and channels and normally
returns without waiting on an upstream provider.

For each tracked token, the collector uses the strongest pool plus one additional
meaningful pool when its liquidity is sufficient. Activity and liquidity are
aggregated across those pools while price and chart identity remain anchored to
the strongest pool. Recent trades retain provider trade and transaction IDs, are
deduplicated across refreshes, and are grouped by transaction before large-buy
totals are reported. The engine retains noteworthy price, volume, buy-pressure,
holder, high/low, market-cap, and large-buy events after the instant signal has
cooled, then combines them with a current market-breadth and trading-activity
summary. Large-buy amounts use full comma-separated USD values (for example,
`$2,300`). Dynamic liquidity and volume thresholds reduce noise from tiny pools.

Continuous coverage is still tracked internally across the entire feed window,
but transient collector-coverage notices are not included in the public result.

The one-minute market collector also evaluates the shared UTYA movement alert
for `@utyachat`. The dashboard price refresh remains a second observation path;
the persisted delivered-alert baseline and a shared lock prevent duplicate posts.

The persisted `output/pulse_history.json` retains 48 hours by default for
historical comparisons. Related signals are grouped into one event per token,
scored to select the strongest set, then displayed newest to oldest and marked
with their age. Current market data is cached briefly
to coalesce concurrent commands, and stale snapshots are rejected rather than
presented as live.

The title, result layout, event block, market snapshot, quiet-market response,
and unavailable response are editable from `/menu` → `Messages` → `Pulse messages`. The same
admin area includes a sample-data preview. Insert Telegram custom emojis directly
while editing; their IDs are captured and stored automatically. `/new` has the
same preview and custom-emoji workflow under `Newly verified messages`.
`[NAME]` is automatically linked to the selected strongest-pool chart in `/new`.
`[CHART_URL]` remains optional for owners who also want to place the raw chart URL
elsewhere in a custom row.
Each tracked token has an independent regular or Telegram custom emoji configured
through `Edit coin emojis`. `[EMOJI]` and `[COIN_EMOJI]` insert that token-specific
icon. Pulse signal icons remain configurable through `Edit signal emojis`, with
independent regular or Telegram custom emojis for large buys, buy pressure,
volume, holder growth, upward and downward price movements, 24-hour highs/lows,
and market-cap crossings; `[SIGNAL_EMOJI]` inserts the signal-specific icon.
Token tickers rendered through `[TICKER]` are linked to the strongest live pool's
GeckoTerminal chart; existing event templates do not need to be changed.

Optional tuning uses `PULSE_CACHE_SECONDS`, `PULSE_HISTORY_SECONDS`,
`PULSE_RECORD_INTERVAL_SECONDS`, `PULSE_PERSIST_INTERVAL_SECONDS`, and
`PULSE_MAX_EVENTS`. Live-source controls are `PULSE_MARKET_CACHE_SECONDS`,
`PULSE_MARKET_STALE_SECONDS`, `PULSE_TRADE_CONCURRENCY`, and
`PULSE_MAX_SNAPSHOT_AGE_SECONDS`. Feed controls are `PULSE_REFRESH_SECONDS`,
`PULSE_FEED_WINDOW_SECONDS`, and `PULSE_COVERAGE_GAP_SECONDS`. The history is
checkpointed atomically every five minutes by default while deduplicated buys are
persisted immediately in `output/pulse_buys.json`. Defaults require no environment
changes. The pulse event feed uses a one-hour rolling window by default.

`src/price_service.py` currently returns placeholder values. Replace it with DexScreener fetching later.
