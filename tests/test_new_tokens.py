from __future__ import annotations

import unittest
import json
import tempfile
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from src.new_tokens_service import (
    DEFAULT_NEW_TOKEN_RESULT_LIMIT,
    NewToken,
    NewTokenFilters,
    NewTokensService,
    NewTokensSnapshot,
    VerifiedAsset,
    _ton_address_key,
    _ton_friendly_address,
    _yaml_scalar,
)
from src.telegram_bot import TelegramDashboardBot


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def new_pool_payload() -> dict:
    return {
        "included": [
            {
                "type": "token",
                "id": "ton_cat-address",
                "attributes": {"address": "cat-address", "name": "Cat & Dog", "symbol": "cat<dog"},
            },
            {
                "type": "token",
                "id": "ton_usdt-address",
                "attributes": {"address": "usdt-address", "name": "Official USDT TON", "symbol": "USDT-TON"},
            },
            {
                "type": "dex",
                "id": "stonfi-v2",
                "attributes": {"name": "STON.fi V2"},
            },
        ],
        "data": [
            _pool("ton_cat-address", "cat-pool", "2026-07-31T11:30:00Z", "1250", "24000", "150000"),
            _pool("ton_usdt-address", "fake-pool", "2026-07-31T11:40:00Z", "9000", "50000", "900000"),
        ],
    }


def _pool(
    base_id: str,
    pool_id: str,
    created_at: str,
    liquidity: str,
    volume: str,
    fdv: str,
) -> dict:
    return {
        "type": "pool",
        "id": f"ton_{pool_id}",
        "attributes": {
            "pool_created_at": created_at,
            "base_token_price_usd": "0.001",
            "market_cap_usd": None,
            "fdv_usd": fdv,
            "reserve_in_usd": liquidity,
            "volume_usd": {"h24": volume},
        },
        "relationships": {
            "base_token": {"data": {"type": "token", "id": base_id}},
            "dex": {"data": {"type": "dex", "id": "stonfi-v2"}},
        },
    }


def _dex_pair(pair_address: str, liquidity: float, volume_24h: float) -> dict:
    return {
        "chainId": "ton",
        "dexId": "dedust",
        "pairAddress": pair_address,
        "url": f"https://dexscreener.com/ton/{pair_address}",
        "baseToken": {
            "address": "cat-address",
            "name": "Cat & Dog",
            "symbol": "CAT<DOG",
        },
        "quoteToken": {"address": "ton-address", "symbol": "TON"},
        "priceUsd": "0.001",
        "marketCap": None,
        "fdv": "150000",
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume_24h},
        "pairCreatedAt": 1_785_604_800_000,
    }


def token(
    *,
    address: str = "cat-address",
    created_at: datetime = NOW - timedelta(minutes=30),
    liquidity: float = 1_250,
    market_cap: float | None = None,
    fdv: float | None = 150_000,
    holders: int | None = 321,
    verification: str | None = "whitelist",
) -> NewToken:
    return NewToken(
        token_address=address,
        name="Cat & Dog",
        symbol="CAT<DOG",
        pool_address="cat-pool",
        dex_name="STON.fi V2",
        created_at=created_at,
        price_usd=0.001,
        market_cap_usd=market_cap,
        fdv_usd=fdv,
        liquidity_usd=liquidity,
        volume_24h_usd=24_000,
        holders=holders,
        verification=verification,
    )


class NewTokenServiceTests(unittest.TestCase):
    def make_service(self) -> NewTokensService:
        service = NewTokensService.__new__(NewTokensService)
        service.minimum_liquidity_usd = 500
        service.result_limit = DEFAULT_NEW_TOKEN_RESULT_LIMIT
        service.blocked_symbols = frozenset()
        service.blocked_addresses = frozenset()
        return service

    def test_parses_pool_fields_and_filters_stablecoin_lookalikes(self) -> None:
        service = self.make_service()
        parsed = service.parse_payload(new_pool_payload())
        eligible = [item for item in parsed if not service.is_blocked(item)]

        self.assertEqual(len(eligible), 1)
        item = eligible[0]
        self.assertEqual(item.symbol, "CAT<DOG")
        self.assertEqual(item.dex_name, "STON.fi V2")
        self.assertEqual(item.fdv_usd, 150_000)
        self.assertIsNone(item.market_cap_usd)
        self.assertEqual(item.valuation_label, "FDV")
        self.assertEqual(item.chart_url, "https://www.geckoterminal.com/ton/pools/cat-pool")

    def test_dex_market_uses_strongest_chart_and_aggregates_24h_pool_volume(self) -> None:
        asset = VerifiedAsset(
            token_address="cat-address",
            name="Cat & Dog",
            symbol="CAT<DOG",
            verified_at=NOW,
            source_path="jettons/CAT.yaml",
        )

        result = NewTokensService._token_from_dex_payload(
            [
                _dex_pair("small-pool", 1_000, 2_000),
                _dex_pair("large-pool", 10_000, 24_000),
            ],
            asset,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("large-pool", result.pool_address)
        self.assertEqual(26_000, result.volume_24h_usd)
        self.assertEqual("DeDust", result.dex_name)
        self.assertEqual(
            "https://dexscreener.com/ton/large-pool",
            result.chart_url,
        )

    def test_selection_applies_age_liquidity_and_mcap_fdv_fallback(self) -> None:
        service = self.make_service()
        snapshot = NewTokensSnapshot(
            updated_at=NOW,
            tokens=(
                token(),
                token(address="low", liquidity=100),
                token(address="old", created_at=NOW - timedelta(hours=2)),
                token(address="small", fdv=50_000),
            ),
        )
        filters = NewTokenFilters(max_age=timedelta(hours=1), minimum_valuation_usd=100_000)

        selected = service.select(snapshot, filters, now=NOW)

        self.assertEqual([item.token_address for item in selected], ["cat-address"])

    def test_default_selection_returns_only_top_five_tokens(self) -> None:
        service = self.make_service()
        snapshot = NewTokensSnapshot(
            updated_at=NOW,
            tokens=tuple(
                token(
                    address=f"token-{index}",
                    created_at=NOW - timedelta(minutes=index),
                )
                for index in range(6)
            ),
        )

        selected = service.select(snapshot, NewTokenFilters(), now=NOW)

        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0].token_address, "token-0")
        self.assertEqual(selected[-1].token_address, "token-4")

    def test_selection_never_exceeds_seven_days_even_for_an_internal_filter(self) -> None:
        service = self.make_service()
        snapshot = NewTokensSnapshot(
            updated_at=NOW,
            tokens=(
                token(address="recent", created_at=NOW - timedelta(days=6, hours=23)),
                token(address="too-old", created_at=NOW - timedelta(days=7, minutes=1)),
            ),
        )

        selected = service.select(
            snapshot,
            NewTokenFilters(max_age=timedelta(days=7)),
            now=NOW,
        )

        self.assertEqual([item.token_address for item in selected], ["recent"])

    def test_selection_includes_only_tonapi_whitelisted_tokens(self) -> None:
        service = self.make_service()
        snapshot = NewTokensSnapshot(
            updated_at=NOW,
            tokens=(
                token(address="verified", verification="whitelist"),
                token(address="gray", verification="graylist"),
                token(address="black", verification="blacklist"),
                token(address="none", verification="none"),
                token(address="unknown", verification=None),
            ),
        )

        selected = service.select(snapshot, NewTokenFilters(), now=NOW)

        self.assertEqual([item.token_address for item in selected], ["verified"])

    def test_friendly_and_raw_ton_addresses_share_one_verification_key(self) -> None:
        account_hash = bytes.fromhex("ab" * 32)
        friendly = base64.urlsafe_b64encode(
            bytes((0x11, 0x00)) + account_hash + b"\x00\x00"
        ).decode().rstrip("=")

        self.assertEqual(_ton_address_key(friendly), f"0:{'ab' * 32}")
        self.assertEqual(_ton_address_key(f"0:{'AB' * 32}"), f"0:{'ab' * 32}")

    def test_raw_address_is_converted_to_canonical_friendly_form(self) -> None:
        raw = "0:267b058c55c70ca5c2ad80b52372f9301954a7cd821aaf6c37f43f96ffe269d5"

        friendly = _ton_friendly_address(raw)

        self.assertEqual(friendly, "EQAmewWMVccMpcKtgLUjcvkwGVSnzYIar2w39D-W_-Jp1U_V")
        self.assertEqual(_ton_address_key(friendly), raw)


class NewTokenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_verification_week_is_cached_as_a_valid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            service = NewTokensService(
                AsyncMock(return_value=object()),
                cache_path,
                refresh_seconds=3600,
            )
            service._fetch_newly_verified_assets = AsyncMock(return_value=())

            snapshot = await service.refresh(force=True)

            self.assertEqual(snapshot.tokens, ())
            self.assertTrue(cache_path.exists())
            reloaded = NewTokensService(AsyncMock(), cache_path)
            self.assertIsNotNone(reloaded.current())
            self.assertEqual(reloaded.current().tokens, ())

    async def test_refresh_enriches_and_caches_discovered_list_additions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = NewTokensService(
                AsyncMock(return_value=object()),
                Path(directory) / "cache.json",
            )
            asset = VerifiedAsset(
                token_address="cat-address",
                name="Cat & Dog",
                symbol="CAT<DOG",
                verified_at=NOW - timedelta(hours=3),
                source_path="jettons/CAT.yaml",
            )
            service._fetch_newly_verified_assets = AsyncMock(return_value=(asset,))
            service._fetch_asset_market = AsyncMock(return_value=token(holders=None))
            service.enrich_holders = AsyncMock(return_value=(token(holders=321),))

            snapshot = await service.refresh(force=True)

            self.assertEqual([item.token_address for item in snapshot.tokens], ["cat-address"])
            self.assertTrue((Path(directory) / "cache.json").exists())
            service.enrich_holders.assert_awaited_once()
            self.assertTrue(service.enrich_holders.await_args.kwargs["force"])

    async def test_market_enrichment_updates_volume_and_preserves_holder_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            original = token(holders=321)
            service = NewTokensService(AsyncMock(return_value=object()), cache_path)
            service.snapshot = NewTokensSnapshot(updated_at=NOW, tokens=(original,))
            live_market = replace(
                original,
                volume_24h_usd=31_500,
                liquidity_usd=9_000,
                holders=None,
                chart_url_override="https://dexscreener.com/ton/cat-pool",
            )
            service._fetch_asset_market = AsyncMock(return_value=live_market)

            enriched = await service.enrich_market((original,), force=True)

            self.assertEqual(31_500, enriched[0].volume_24h_usd)
            self.assertEqual(321, enriched[0].holders)
            self.assertEqual(
                "https://dexscreener.com/ton/cat-pool",
                enriched[0].chart_url,
            )
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(31_500, persisted["tokens"][0]["volume_24h_usd"])
            self.assertEqual(321, persisted["tokens"][0]["holders"])

    async def test_zero_bulk_holder_count_uses_authoritative_total_and_persists_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            raw = "0:267b058c55c70ca5c2ad80b52372f9301954a7cd821aaf6c37f43f96ffe269d5"
            original = token(address=raw, holders=None)
            service = NewTokensService(AsyncMock(return_value=object()), cache_path)
            service.snapshot = NewTokensSnapshot(updated_at=NOW, tokens=(original,))
            service._fetch_bulk_jetton_metadata = AsyncMock(
                return_value={
                    _ton_address_key(raw): {
                        "holders_count": 0,
                        "verification": "whitelist",
                        "mintable": False,
                    }
                }
            )
            service._fetch_json = AsyncMock(return_value={"total": 985})

            enriched = await service.enrich_holders((original,), force=True)

            self.assertEqual(enriched[0].holders, 985)
            self.assertFalse(enriched[0].mintable)
            requested_url = service._fetch_json.await_args.args[1]
            self.assertIn("EQAmewWMVccMpcKtgLUjcvkwGVSnzYIar2w39D-W_-Jp1U_V", requested_url)
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["tokens"][0]["holders"], 985)

    async def test_holder_enrichment_keeps_last_valid_count_on_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = token(holders=230)
            service = NewTokensService(
                AsyncMock(return_value=object()),
                Path(directory) / "cache.json",
            )
            service.snapshot = NewTokensSnapshot(updated_at=NOW, tokens=(original,))
            service._fetch_bulk_jetton_metadata = AsyncMock(
                return_value={
                    _ton_address_key(original.token_address): {
                        "holders_count": 0,
                        "verification": "whitelist",
                    }
                }
            )
            service._fetch_json = AsyncMock(side_effect=RuntimeError("rate limited"))

            enriched = await service.enrich_holders((original,), force=True)

            self.assertEqual(enriched[0].holders, 230)

    async def test_discovery_rejects_partial_metadata_instead_of_replacing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = NewTokensService(
                AsyncMock(return_value=object()),
                Path(directory) / "cache.json",
            )
            service._fetch_json = AsyncMock(
                side_effect=[
                    [{"sha": "base-sha"}],
                    {
                        "files": [
                            {"status": "added", "filename": "jettons/A.yaml"},
                            {"status": "added", "filename": "jettons/B.yaml"},
                        ],
                        "commits": [],
                    },
                ]
            )
            asset = VerifiedAsset(
                token_address="a-address",
                name="A",
                symbol="A",
                verified_at=NOW,
                source_path="jettons/A.yaml",
            )
            service._fetch_verified_asset = AsyncMock(
                side_effect=[asset, RuntimeError("rate limited")]
            )

            with self.assertRaisesRegex(RuntimeError, "metadata was unavailable"):
                await service._fetch_newly_verified_assets(object(), NOW)


class NewTokenVerificationHistoryTests(unittest.TestCase):
    def test_yaml_scalar_reads_quoted_and_unquoted_metadata(self) -> None:
        payload = 'name: "Cat: Token"\nsymbol: CAT # comment\naddress: \'EQabc\'\n'

        self.assertEqual(_yaml_scalar(payload, "name"), "Cat: Token")
        self.assertEqual(_yaml_scalar(payload, "symbol"), "CAT")
        self.assertEqual(_yaml_scalar(payload, "address"), "EQabc")

    def test_verification_date_uses_earliest_path_commit(self) -> None:
        dates = [
            {"commit": {"committer": {"date": "2026-07-30T12:00:00Z"}}},
            {"commit": {"committer": {"date": "2026-07-29T09:00:00Z"}}},
        ]

        result = NewTokensService._verification_date(
            "jettons/CAT.yaml",
            dates,
            [],
            fallback=NOW - timedelta(days=7),
        )

        self.assertEqual(result, datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc))

    def test_verification_date_prefers_the_review_merge_over_old_author_date(self) -> None:
        result = NewTokensService._verification_date(
            "jettons/WAR.yaml",
            [
                {
                    "sha": "source-commit",
                    "commit": {"committer": {"date": "2026-07-20T09:00:00Z"}},
                }
            ],
            [
                {
                    "sha": "merge-commit",
                    "parents": [{"sha": "main-parent"}, {"sha": "source-commit"}],
                    "commit": {"committer": {"date": "2026-07-30T12:00:00Z"}},
                }
            ],
            fallback=NOW - timedelta(days=7),
        )

        self.assertEqual(result, datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc))

    def test_verification_date_can_fall_back_to_matching_compare_commit(self) -> None:
        result = NewTokensService._verification_date(
            "jettons/MERLIN.yaml",
            [],
            [
                {
                    "commit": {
                        "message": "Add MERLIN jetton",
                        "committer": {"date": "2026-07-28T15:00:00Z"},
                    }
                }
            ],
            fallback=NOW - timedelta(days=7),
        )

        self.assertEqual(result, datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc))

    def test_best_pool_uses_highest_liquidity_matching_base_token(self) -> None:
        payload = new_pool_payload()
        payload["data"].append(
            _pool("ton_cat-address", "large-pool", "2026-07-20T00:00:00Z", "9000", "1", "1")
        )

        result = NewTokensService._best_pool(payload, "cat-address")

        self.assertIsNotNone(result)
        self.assertEqual(result[2], "large-pool")

    def test_best_pool_accepts_target_token_as_quote_token(self) -> None:
        payload = new_pool_payload()
        payload["included"].append(
            {
                "type": "token",
                "id": "ton_quote-address",
                "attributes": {"address": "quote-address", "name": "Quote", "symbol": "QUOTE"},
            }
        )
        payload["data"][0]["relationships"]["base_token"]["data"]["id"] = "ton_quote-address"
        payload["data"][0]["relationships"]["quote_token"] = {
            "data": {"type": "token", "id": "ton_cat-address"}
        }

        result = NewTokensService._best_pool(payload, "cat-address")

        self.assertIsNotNone(result)
        self.assertFalse(result[3])

    def test_github_token_is_not_sent_to_market_or_tonapi_providers(self) -> None:
        with patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}):
            self.assertNotIn(
                "Authorization",
                NewTokensService._headers("https://api.geckoterminal.com/api/v2/example"),
            )
            self.assertNotIn(
                "Authorization",
                NewTokensService._headers("https://tonapi.io/v2/example"),
            )
            self.assertIn(
                "Authorization",
                NewTokensService._headers("https://api.github.com/repos/example"),
            )


class NewTokenFormatTests(unittest.TestCase):
    def make_bot(self) -> TelegramDashboardBot:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.new_message = bot.DEFAULT_NEW_MESSAGE
        bot.new_row = bot.DEFAULT_NEW_ROW
        bot.new_tokens_service = SimpleNamespace(minimum_liquidity_usd=0)
        return bot

    def test_formats_owner_templates_links_values_and_escapes_provider_text(self) -> None:
        bot = self.make_bot()
        snapshot = NewTokensSnapshot(updated_at=NOW, tokens=(token(market_cap=140_000),))

        text = bot.format_new_tokens_message(
            snapshot,
            snapshot.tokens,
            now=NOW,
        )

        self.assertIn(
            '$CAT&lt;DOG — <a href="https://www.geckoterminal.com/ton/pools/cat-pool">'
            'Cat &amp; Dog</a></b>',
            text,
        )
        self.assertNotIn("Verified:", text)
        self.assertIn("├ 📊 MCAP: $140K", text)
        self.assertNotIn("24h Volume", text)
        self.assertIn("└ 👥 Holders: 321", text)
        self.assertNotIn("STON.fi V2", text)
        self.assertNotIn("Liquidity", text)
        self.assertNotIn("<code>cat-address</code>", text)
        self.assertNotIn("Filters:", text)
        self.assertNotIn("verified within 1h", text)
        self.assertIn("New TON Tokens", text)
        self.assertIn("31/07/2026 12:00 UTC", text)

    def test_all_new_templates_validate_required_placeholders(self) -> None:
        bot = self.make_bot()
        for key in (
            "new_message",
            "new_row",
            "new_usage_message",
            "new_empty_message",
            "new_unavailable_message",
        ):
            with self.subTest(key=key):
                default = bot.PUBLIC_MESSAGE_DEFAULTS[key]
                self.assertIsNone(bot.validate_message_template(key, default))

    def test_new_row_can_be_saved_without_an_optional_chart_url(self) -> None:
        bot = self.make_bot()
        row_without_chart = (
            "[RANK]. <b>$[TICKER] — [NAME]</b>\n"
            "[VALUATION_LABEL]: [MARKET_CAP] · Holders: [HOLDERS]"
        )

        self.assertIsNone(bot.validate_message_template("new_row", row_without_chart))
        self.assertIn(
            "[CHART_URL]",
            bot.PUBLIC_MESSAGE_OPTIONAL_PLACEHOLDERS["new_row"],
        )
        self.assertIn(
            "[VOLUME_24H]",
            bot.PUBLIC_MESSAGE_OPTIONAL_PLACEHOLDERS["new_row"],
        )

    def test_multi_day_verification_age_is_readable(self) -> None:
        bot = self.make_bot()

        self.assertEqual(bot.format_new_token_age(timedelta(days=3, hours=4)), "3d 4h")
        self.assertEqual(bot.format_new_token_age(timedelta(days=7), compact_limit=True), "7d")


class NewTokenCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_command_has_no_arguments_and_works_in_groups(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.owner_id = 386839171
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_new_tokens = AsyncMock()

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": -100999, "type": "supergroup"},
                "text": "/new@memepricesbot",
            }
        )

        bot.ensure_subscribed.assert_awaited_once_with(12345, -100999, "supergroup")
        bot.send_new_tokens.assert_awaited_once_with(-100999)

    async def test_new_command_rejects_extra_filter_arguments(self) -> None:
        bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
        bot.pending_text_edits = {}
        bot.ensure_subscribed = AsyncMock(return_value=True)
        bot.send_new_tokens = AsyncMock()
        bot.send_message = AsyncMock()
        bot.new_usage_message = bot.DEFAULT_NEW_USAGE_MESSAGE

        await bot.handle_message(
            {
                "from": {"id": 12345},
                "chat": {"id": -100999, "type": "supergroup"},
                "text": "/new@memepricesbot 24h",
            }
        )

        bot.send_new_tokens.assert_not_awaited()
        self.assertIn("Use /new without additional text", bot.send_message.await_args.args[1])


class NewTokenMessageMigrationTests(unittest.TestCase):
    def test_adds_new_command_to_custom_messages_and_preserves_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {
                "help_message": "Custom\n<code>/trending</code> — trends\n<code>/help</code>",
                "private_help_message": "Private\n<code>/trending</code> — trends\n<code>/guide</code>",
                "guide_commands_message": (
                    "Guide\n<code>/trending</code>\n<blockquote>Trends.</blockquote>\nFooter"
                ),
                "unrelated": "preserve me",
            }

            bot.migrate_new_command_messages()
            saved = json.loads(bot.settings_path.read_text(encoding="utf-8"))

            self.assertIn("<code>/new</code>", saved["help_message"])
            self.assertIn("<code>/new</code>", saved["private_help_message"])
            self.assertIn("<code>/new</code>", saved["guide_commands_message"])
            self.assertNotIn("[filter]", saved["help_message"])
            self.assertIn("verified in the last 7 days", saved["help_message"])
            self.assertIn("newly verified during the last 7 days", saved["guide_commands_message"])
            self.assertEqual(saved["unrelated"], "preserve me")

            bot.migrate_new_command_messages()
            self.assertEqual(bot.settings["help_message"].count("<code>/new"), 1)

    def test_replaces_old_launch_wording_in_existing_custom_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {
                "help_message": "<code>/new [filter]</code> — newly launched TON tokens",
                "private_help_message": "<code>/new [filter]</code> — newly launched TON tokens",
                "guide_commands_message": (
                    "<code>/new [filter]</code>\n"
                    "<blockquote>Discover newly launched TON tokens by age and minimum valuation.</blockquote>"
                ),
            }

            bot.migrate_new_command_messages()

            self.assertNotIn("newly launched", bot.settings["help_message"])
            self.assertNotIn("[filter]", bot.settings["help_message"])
            self.assertIn("verified in the last 7 days", bot.settings["private_help_message"])
            self.assertIn("newly verified during the last 7 days", bot.settings["guide_commands_message"])

    def test_removes_filter_placeholder_from_custom_empty_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = TelegramDashboardBot.__new__(TelegramDashboardBot)
            bot.settings_path = Path(directory) / "settings.json"
            bot.settings = {
                "new_empty_message": (
                    "🙂 <b>No newly verified tokens found</b>\n\n"
                    "No reviewed TON asset-list additions matched: <b>[FILTERS]</b>"
                ),
                "unrelated": "preserve me",
            }

            bot.migrate_new_command_messages()

            self.assertNotIn("[FILTERS]", bot.settings["new_empty_message"])
            self.assertIn("last 7 days", bot.settings["new_empty_message"])
            self.assertEqual("preserve me", bot.settings["unrelated"])


if __name__ == "__main__":
    unittest.main()
