"""Telegram: alerts to the admin chats through the Bot API, and replies to the command webhook.

``sendMessage`` (https://core.telegram.org/bots/api#sendmessage) once per configured chat, HTML parse
mode with everything user-supplied escaped. The bot token lives in the URL path, so it is scrubbed
from every error. Replies to ``/halt``, ``/resume`` and ``/status`` do not use this class: the webhook
answers Telegram's request with the ``sendMessage`` call in its own response body.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar

import httpx

from app.adapters.notifications.base import FinalDeliveryError, NotificationDispatcher, OutboundMessage, check
from app.models.sentinel import ChannelName

API_BASE = "https://api.telegram.org"
MESSAGE_LIMIT = 4096


class TelegramDispatcher(NotificationDispatcher):
    channel: ClassVar[ChannelName] = ChannelName.TELEGRAM

    def __init__(self, http: httpx.AsyncClient, *, bot_token: str, chat_ids: Sequence[int], **kwargs: object) -> None:
        super().__init__(http, **kwargs)  # type: ignore[arg-type]
        if not bot_token:
            raise ValueError("a Telegram dispatcher needs a bot token")
        self.bot_token = bot_token
        self.chat_ids = tuple(chat_ids)

    def secrets(self) -> Iterable[str]:
        return (self.bot_token,)

    def payload(self, message: OutboundMessage, chat_id: int) -> dict[str, object]:
        return {
            "chat_id": chat_id,
            "text": message.html(MESSAGE_LIMIT),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": message.severity.rank < 2 and message.kind != "MARKET_HYPE",  # quiet for INFO/WARNING
        }

    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        if not self.chat_ids:
            raise FinalDeliveryError("no chat ids configured")
        for chat_id in self.chat_ids:
            if str(chat_id) in done:
                continue
            response = check(await self.http.post(f"{API_BASE}/bot{self.bot_token}/sendMessage", json=self.payload(message, chat_id)))
            body = response.json()
            if not body.get("ok", False):
                raise FinalDeliveryError(f"Telegram refused: {str(body.get('description', ''))[:120]}")
            done.add(str(chat_id))
