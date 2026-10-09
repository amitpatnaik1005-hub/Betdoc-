"""Building the Sentinel's dispatchers from ``sentinel_channels``.

Each channel's secrets are one JSON object encrypted with the master vault key; its non-secret
configuration is plain JSON. ``CREDENTIAL_FIELDS`` and ``CONFIG_FIELDS`` are the whole contract the
API validates against. A row that is disabled, has no secrets, or cannot build (a webhook on the
wrong host, a malformed number) yields no dispatcher, and ``problems`` says why.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.adapters.notifications.base import DispatcherConfigurationError, NotificationDispatcher
from app.adapters.notifications.discord import DiscordDispatcher
from app.adapters.notifications.pagerduty import PagerDutyDispatcher
from app.adapters.notifications.telegram import TelegramDispatcher
from app.adapters.notifications.twilio import TwilioDispatcher
from app.core.security_vault import VaultCrypto, VaultDecryptionError
from app.models.sentinel import ChannelName, SentinelChannel

CREDENTIAL_FIELDS: dict[ChannelName, tuple[str, ...]] = {
    ChannelName.TELEGRAM: ("bot_token", "webhook_secret"),  # webhook_secret: Telegram's X-Telegram-Bot-Api-Secret-Token
    ChannelName.DISCORD: ("webhook_url",),
    ChannelName.TWILIO: ("account_sid", "auth_token"),
    ChannelName.PAGERDUTY: ("routing_key",),
}
REQUIRED_CREDENTIALS: dict[ChannelName, tuple[str, ...]] = {
    ChannelName.TELEGRAM: ("bot_token",),
    ChannelName.DISCORD: ("webhook_url",),
    ChannelName.TWILIO: ("account_sid", "auth_token"),
    ChannelName.PAGERDUTY: ("routing_key",),
}
CONFIG_FIELDS: dict[ChannelName, dict[str, type]] = {
    ChannelName.TELEGRAM: {"chat_ids": list, "admin_user_ids": list},  # alerts go to chat_ids; commands only from them (and these users)
    ChannelName.DISCORD: {"mention_on_fatal": bool},
    ChannelName.TWILIO: {"from_number": str, "to_numbers": list, "voice_on_fatal": bool},
    ChannelName.PAGERDUTY: {"source": str},
}


@dataclass(slots=True)
class ChannelSecrets:
    """Decrypted credentials. Only ever held in memory for as long as a dispatcher needs them."""

    values: dict[str, str]

    def get(self, name: str) -> str:
        return self.values.get(name, "")


def mask(value: str, keep: int = 4) -> str:
    value = value.strip()
    return "•" * 4 if len(value) <= keep * 2 else f"{value[:2]}…{value[-keep:]}"


def credentials_hint(channel: ChannelName, values: Mapping[str, str]) -> str:
    if channel is ChannelName.TELEGRAM:
        token = values.get("bot_token", "")
        bot_id = token.split(":", 1)[0] if ":" in token else ""
        return f"bot {bot_id or mask(token)}" + (" · webhook secret set" if values.get("webhook_secret") else "")
    if channel is ChannelName.DISCORD:
        return f"webhook …{values.get('webhook_url', '')[-6:]}"
    if channel is ChannelName.TWILIO:
        return f"account {mask(values.get('account_sid', ''))}"
    return f"key {mask(values.get('routing_key', ''))}"


def decrypt(row: SentinelChannel, vault: VaultCrypto | None) -> ChannelSecrets | None:
    if vault is None or not row.encrypted_credentials:
        return None
    try:
        raw = json.loads(vault.decrypt_key(row.encrypted_credentials))
    except (VaultDecryptionError, ValueError):
        return None
    return ChannelSecrets({k: str(v) for k, v in raw.items() if isinstance(v, str | int)}) if isinstance(raw, dict) else None


def _ints(values: Any) -> list[int]:
    out = []
    for value in values or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def build(row: SentinelChannel, vault: VaultCrypto | None, http: httpx.AsyncClient) -> NotificationDispatcher:
    """The dispatcher for one row; raises DispatcherConfigurationError when it cannot work."""
    channel = ChannelName(row.channel)
    secrets = decrypt(row, vault)
    if secrets is None:
        raise DispatcherConfigurationError("no credentials stored" if not row.encrypted_credentials else "credentials cannot be decrypted with this vault key")
    missing = [name for name in REQUIRED_CREDENTIALS[channel] if not secrets.get(name)]
    if missing:
        raise DispatcherConfigurationError(f"missing {', '.join(missing)}")
    config = row.config or {}
    try:
        if channel is ChannelName.TELEGRAM:
            chats = _ints(config.get("chat_ids"))
            if not chats:
                raise DispatcherConfigurationError("no chat ids configured")
            return TelegramDispatcher(http, bot_token=secrets.get("bot_token"), chat_ids=chats)
        if channel is ChannelName.DISCORD:
            return DiscordDispatcher(http, webhook_url=secrets.get("webhook_url"), mention_on_fatal=bool(config.get("mention_on_fatal", False)))
        if channel is ChannelName.TWILIO:
            return TwilioDispatcher(
                http,
                account_sid=secrets.get("account_sid"),
                auth_token=secrets.get("auth_token"),
                from_number=str(config.get("from_number", "")),
                to_numbers=[str(n) for n in config.get("to_numbers") or []],
                voice_on_fatal=bool(config.get("voice_on_fatal", True)),
            )
        return PagerDutyDispatcher(http, routing_key=secrets.get("routing_key"), source=str(config.get("source") or "betdoc"))
    except ValueError as exc:
        raise DispatcherConfigurationError(str(exc)) from exc


def build_all(rows: list[SentinelChannel], vault: VaultCrypto | None, http: httpx.AsyncClient) -> tuple[dict[ChannelName, NotificationDispatcher], dict[ChannelName, str]]:
    """(dispatchers for the enabled, working rows; the reason each other row has none)."""
    dispatchers: dict[ChannelName, NotificationDispatcher] = {}
    problems: dict[ChannelName, str] = {}
    known = {row.channel: row for row in rows}
    for channel in ChannelName:
        row = known.get(channel.value)
        if row is None:
            problems[channel] = "not configured"
        elif not row.enabled:
            problems[channel] = "disabled"
        else:
            try:
                dispatchers[channel] = build(row, vault, http)
            except DispatcherConfigurationError as exc:
                problems[channel] = str(exc)
    return dispatchers, problems
