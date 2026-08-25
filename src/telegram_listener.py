"""Watching for the owner's replies, so a reply can trigger a callback.

This is the only inbound control path into the service, and it acts on what it
receives by ringing someone, so the sender check is the whole security story:

- the update must come from the configured chat, **and**
- the sender's user id must be on the allowlist.

Chat id alone is not enough. If the summaries ever move to a group, anyone in that
group would otherwise be able to make the service place calls.

Only one process may long-poll a bot token - a second one causes Telegram to hand
out updates alternately and both behave erratically. If your supervisor is an
agent that also speaks Telegram, turn its Telegram platform OFF: this container
owns the bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

# Words that mean "no, don't do that" rather than "say this to them".
CANCEL_WORDS = {"stop", "cancel", "no", "ignore", "nvm", "never mind", "leave it"}

POLL_TIMEOUT_S = 50


class TelegramListener:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        allowed_user_ids: set[str],
        offset_path: Path,
        on_reply: Callable[[int | None, str], Awaitable[None]],
    ):
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._allowed = {str(u) for u in allowed_user_ids if str(u).strip()}
        self._offset_path = offset_path
        self._on_reply = on_reply
        self._offset: int | None = self._load_offset()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def _load_offset(self) -> int | None:
        try:
            return int(json.loads(self._offset_path.read_text())["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_offset(self) -> None:
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.write_text(json.dumps({"offset": self._offset}))
        except OSError as exc:
            log.error("could not persist telegram offset: %s", exc)

    def start(self) -> None:
        if not self.enabled:
            log.warning("telegram listener disabled - no bot token or chat id")
            return
        self._task = asyncio.create_task(self._run())
        log.info("telegram listener started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = 1
        while True:
            try:
                await self._poll_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the listener must never die
                log.warning(
                    "telegram poll failed (%s), retrying in %ss",
                    type(exc).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll_once(self) -> None:
        params: dict[str, object] = {
            "timeout": POLL_TIMEOUT_S,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset

        async with httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{self._token}/getUpdates", params=params
            )
        if resp.status_code != 200:
            raise RuntimeError(f"getUpdates HTTP {resp.status_code}")

        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError("getUpdates returned not-ok")

        for update in body.get("result", []):
            # Advance past every update, including ones we ignore, or a message we
            # will never act on would be redelivered forever.
            self._offset = update["update_id"] + 1
            await self._handle(update)

        self._save_offset()

    async def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text:
            return

        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))

        if chat_id != self._chat_id:
            log.warning("ignoring telegram message from unexpected chat")
            return
        if self._allowed and user_id not in self._allowed:
            log.warning("ignoring telegram message from unauthorised user")
            return

        reply_to = (message.get("reply_to_message") or {}).get("message_id")

        if text.strip().lower() in CANCEL_WORDS:
            log.info("telegram reply was a cancel word - taking no action")
            return

        await self._on_reply(reply_to, text)
