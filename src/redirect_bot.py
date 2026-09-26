from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv


class TelegramRedirectBot:
    def __init__(self) -> None:
        load_dotenv(os.getenv("REDIRECT_ENV_FILE", ".env.redirect"), override=False)
        self.bot_token = os.getenv("REDIRECT_BOT_TOKEN", "").strip()
        self.source_username = self._normalize_username(
            os.getenv("SOURCE_BOT_USERNAME", "memepricesbot")
        )
        self.target_username = self._normalize_username(
            os.getenv("TARGET_BOT_USERNAME", "memesbot")
        )
        self.admin_ids = self._parse_ids(os.getenv("ADMIN_IDS", "386839171,6422556848"))
        self.cooldown_seconds = max(5, int(os.getenv("REDIRECT_COOLDOWN_SECONDS", "30")))
        self.state_path = Path(
            os.getenv("REDIRECT_STATE_PATH", "output/redirect_state.json")
        )
        self.offset = self._load_offset()
        self.bot_id = 0
        self.cooldowns: dict[tuple[int, int], float] = {}
        self.session: aiohttp.ClientSession | None = None

        if not self.bot_token:
            raise RuntimeError("REDIRECT_BOT_TOKEN is missing")
        if not self.source_username or not self.target_username:
            raise RuntimeError("Source and target bot usernames are required")
        if self.source_username == self.target_username:
            raise RuntimeError("Source and target bot usernames must be different")

    @staticmethod
    def _normalize_username(value: str) -> str:
        return value.strip().lstrip("@").casefold()

    @staticmethod
    def _parse_ids(value: str) -> set[int]:
        ids: set[int] = set()
        for item in value.split(","):
            try:
                user_id = int(item.strip())
            except ValueError:
                continue
            if user_id > 0:
                ids.add(user_id)
        return ids

    @property
    def target_url(self) -> str:
        return f"https://t.me/{self.target_username}"

    def notice_text(self) -> str:
        return (
            "📢 <b>Meme Price Bot has moved</b>\n\n"
            f'The bot is now available at <a href="{self.target_url}">'
            f"@{self.target_username}</a>.\n\n"
            "Use the new bot for live prices, token reports, swaps, trending, "
            "alerts, and all other features."
        )

    def notice_markup(self) -> str:
        return json.dumps(
            {
                "inline_keyboard": [
                    [
                        {
                            "text": f"Open @{self.target_username}",
                            "url": self.target_url,
                        }
                    ]
                ]
            },
            separators=(",", ":"),
        )

    def _load_offset(self) -> int:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return max(0, int(payload.get("offset") or 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _save_offset(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"offset": self.offset}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def api(
        self,
        method: str,
        payload: dict[str, str] | None = None,
        *,
        timeout: int = 35,
    ) -> dict[str, Any]:
        session = await self.get_session()
        request_timeout = aiohttp.ClientTimeout(
            total=timeout,
            sock_connect=5,
            sock_read=max(5, timeout - 2),
        )
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        async with session.post(
            url,
            data=payload or {},
            timeout=request_timeout,
        ) as response:
            result = await response.json(content_type=None)
        if not result.get("ok"):
            description = str(result.get("description") or "Telegram API request failed")
            raise RuntimeError(f"{method}: {description}")
        return result

    async def validate_identity(self) -> None:
        result = await self.api("getMe")
        bot = result.get("result") or {}
        username = self._normalize_username(str(bot.get("username") or ""))
        if username != self.source_username:
            raise RuntimeError(
                f"Redirect token belongs to @{username or 'unknown'}, expected "
                f"@{self.source_username}"
            )
        self.bot_id = int(bot.get("id") or 0)
        if self.bot_id <= 0:
            raise RuntimeError("Redirect bot identity is invalid")

    async def configure_bot(self) -> None:
        commands = [
            {"command": "start", "description": f"Open @{self.target_username}"},
            {"command": "help", "description": f"Open @{self.target_username}"},
        ]
        description = (
            f"Meme Price Bot has moved to @{self.target_username}.\n\n"
            "Open the new bot for live prices, token reports, swaps, trending, "
            "alerts, and all other features."
        )
        await self.api("deleteWebhook", {"drop_pending_updates": "false"}, timeout=15)
        await self.api("setMyName", {"name": "Meme Price • Moved"}, timeout=15)
        await self.api("setMyDescription", {"description": description}, timeout=15)
        await self.api(
            "setMyShortDescription",
            {"short_description": f"Moved to @{self.target_username}."},
            timeout=15,
        )
        await self.api(
            "setMyCommands",
            {"commands": json.dumps(commands, separators=(",", ":"))},
            timeout=15,
        )
        await self.api(
            "setMyCommands",
            {
                "commands": json.dumps(commands, separators=(",", ":")),
                "scope": json.dumps(
                    {"type": "all_private_chats"}, separators=(",", ":")
                ),
            },
            timeout=15,
        )
        await self.api(
            "deleteMyCommands",
            {
                "scope": json.dumps(
                    {"type": "all_group_chats"}, separators=(",", ":")
                )
            },
            timeout=15,
        )
        for admin_id in self.admin_ids:
            await self.api(
                "deleteMyCommands",
                {
                    "scope": json.dumps(
                        {"type": "chat", "chat_id": admin_id},
                        separators=(",", ":"),
                    )
                },
                timeout=15,
            )
        await self.api(
            "setChatMenuButton",
            {"menu_button": json.dumps({"type": "commands"})},
            timeout=15,
        )

    def message_needs_redirect(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        chat_type = str(chat.get("type") or "")
        if chat_type == "private":
            return True
        if chat_type not in {"group", "supergroup"}:
            return False

        reply_user = ((message.get("reply_to_message") or {}).get("from") or {}).get("id")
        if self.bot_id > 0 and int(reply_user or 0) == self.bot_id:
            return True

        text = str(message.get("text") or message.get("caption") or "")
        if not text:
            return False
        return bool(
            re.search(
                rf"@{re.escape(self.source_username)}\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _cooldown_allows(self, chat_id: int, user_id: int) -> bool:
        now = time.monotonic()
        key = (chat_id, user_id)
        if self.cooldowns.get(key, 0.0) > now:
            return False
        self.cooldowns[key] = now + self.cooldown_seconds
        if len(self.cooldowns) > 2_000:
            self.cooldowns = {
                item: expiry for item, expiry in self.cooldowns.items() if expiry > now
            }
        return True

    async def send_notice(
        self,
        chat_id: int,
        user_id: int,
        *,
        reply_to_message_id: int | None = None,
    ) -> bool:
        if not self._cooldown_allows(chat_id, user_id):
            return False
        payload = {
            "chat_id": str(chat_id),
            "text": self.notice_text(),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": self.notice_markup(),
        }
        if reply_to_message_id and chat_id < 0:
            payload["reply_parameters"] = json.dumps(
                {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                },
                separators=(",", ":"),
            )
        try:
            await self.api("sendMessage", payload)
            print(f"Redirect sent chat={chat_id} user={user_id}", flush=True)
            return True
        except Exception as exc:
            print(
                f"Redirect send skipped chat={chat_id} user={user_id}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    async def handle_message(self, message: dict[str, Any]) -> None:
        if not self.message_needs_redirect(message):
            return
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        user_id = int((message.get("from") or {}).get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        if chat_id:
            await self.send_notice(
                chat_id,
                user_id,
                reply_to_message_id=message_id or None,
            )

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        if callback_id:
            try:
                await self.api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": f"This bot moved to @{self.target_username}.",
                        "show_alert": "true",
                    },
                    timeout=15,
                )
            except Exception as exc:
                print(f"Callback acknowledgement failed: {type(exc).__name__}: {exc}", flush=True)

        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        user_id = int((callback.get("from") or {}).get("id") or 0)
        if chat_id:
            await self.send_notice(chat_id, user_id)

    async def handle_inline_query(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        if not query_id:
            return
        result = {
            "type": "article",
            "id": "meme-price-moved-v1",
            "title": f"Meme Price moved to @{self.target_username}",
            "description": "Open the new bot for live prices, alerts, and tools.",
            "input_message_content": {
                "message_text": self.notice_text(),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            "reply_markup": json.loads(self.notice_markup()),
        }
        await self.api(
            "answerInlineQuery",
            {
                "inline_query_id": query_id,
                "results": json.dumps([result], separators=(",", ":")),
                "cache_time": "1",
                "is_personal": "true",
            },
            timeout=15,
        )

    async def handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("message"), dict):
            await self.handle_message(update["message"])
        elif isinstance(update.get("callback_query"), dict):
            await self.handle_callback(update["callback_query"])
        elif isinstance(update.get("inline_query"), dict):
            await self.handle_inline_query(update["inline_query"])

    async def poll_forever(self) -> None:
        allowed_updates = json.dumps(["message", "callback_query", "inline_query"])
        backoff = 1
        while True:
            try:
                result = await self.api(
                    "getUpdates",
                    {
                        "offset": str(self.offset),
                        "timeout": "25",
                        "allowed_updates": allowed_updates,
                    },
                    timeout=35,
                )
                for update in result.get("result") or []:
                    update_id = int(update.get("update_id") or 0)
                    try:
                        await self.handle_update(update)
                    except Exception as exc:
                        print(
                            f"Redirect update failed id={update_id}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    self.offset = max(self.offset, update_id + 1)
                    self._save_offset()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Redirect polling error: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def run(self) -> None:
        try:
            await self.validate_identity()
            await self.configure_bot()
            print(
                f"Redirect active @{self.source_username} -> @{self.target_username} "
                f"offset={self.offset}",
                flush=True,
            )
            await self.poll_forever()
        finally:
            if self.session is not None and not self.session.closed:
                await self.session.close()


def main() -> None:
    asyncio.run(TelegramRedirectBot().run())


if __name__ == "__main__":
    main()
