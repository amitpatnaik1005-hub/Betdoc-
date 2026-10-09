"""Discord: alerts as embeds through a channel webhook.

The webhook URL *is* the credential (anyone holding it can post), so it is stored encrypted, checked
to be a real Discord webhook (https, a Discord host, ``/api/webhooks/``) before anything is sent to
it, and scrubbed from errors. Mentions are off (``allowed_mentions.parse = []``) unless the channel
is configured to ping ``@here`` on FATAL. 429 answers carry ``retry_after``, which the base honours.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar
from urllib.parse import urlsplit

import httpx

from app.adapters.notifications.base import DispatcherConfigurationError, NotificationDispatcher, OutboundMessage, check
from app.models.sentinel import ChannelName, Severity

DISCORD_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})
COLOURS = {Severity.FATAL: 0x7F1D1D, Severity.CRITICAL: 0xDC2626, Severity.WARNING: 0xF59E0B, Severity.INFO: 0x2563EB}
RESOLVED_COLOUR, HYPE_COLOUR = 0x16A34A, 0xA855F7
DESCRIPTION_LIMIT = 4000


def validate_webhook_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in DISCORD_HOSTS or not parts.path.startswith("/api/webhooks/"):
        raise DispatcherConfigurationError("not a Discord webhook URL (https://discord.com/api/webhooks/...)")
    return url.strip()


class DiscordDispatcher(NotificationDispatcher):
    channel: ClassVar[ChannelName] = ChannelName.DISCORD

    def __init__(self, http: httpx.AsyncClient, *, webhook_url: str, mention_on_fatal: bool = False, **kwargs: object) -> None:
        super().__init__(http, **kwargs)  # type: ignore[arg-type]
        self.webhook_url = validate_webhook_url(webhook_url)
        self.mention_on_fatal = mention_on_fatal

    def secrets(self) -> Iterable[str]:
        return (self.webhook_url, urlsplit(self.webhook_url).path)

    def payload(self, message: OutboundMessage) -> dict[str, object]:
        colour = RESOLVED_COLOUR if message.resolves else HYPE_COLOUR if message.kind == "MARKET_HYPE" else COLOURS[message.severity]
        fields = [{"name": "Severity", "value": str(message.severity), "inline": True}, {"name": "Source", "value": message.source[:64], "inline": True}]
        if message.batched:
            fields.append({"name": "Batched", "value": str(message.batched), "inline": True})
        body: dict[str, object] = {
            "username": "BetDoc Sentinel",
            "embeds": [
                {
                    "title": message.headline()[:256],
                    "description": message.body[:DESCRIPTION_LIMIT],
                    "color": colour,
                    "timestamp": message.occurred_at.isoformat(),
                    "footer": {"text": message.kind},
                    "fields": fields,
                }
            ],
            "allowed_mentions": {"parse": []},
        }
        if message.severity is Severity.FATAL and self.mention_on_fatal and not message.resolves:
            body["content"] = "@here"
            body["allowed_mentions"] = {"parse": ["everyone"]}
        return body

    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        if "webhook" in done:
            return
        check(await self.http.post(self.webhook_url, params={"wait": "true"}, json=self.payload(message)))
        done.add("webhook")
