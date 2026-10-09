"""The Sentinel's outbound dispatchers (Group 68): Telegram, Discord, Twilio (SMS and voice), PagerDuty.

Each one takes an ``OutboundMessage`` and makes its service's API call(s), retrying what is
transient and never letting a credential into an error. ``registry`` builds them from the
``sentinel_channels`` rows, decrypting the secrets with the master vault key.
"""

from app.adapters.notifications.base import DeliveryResult, DispatcherConfigurationError, NotificationDispatcher, OutboundMessage
from app.adapters.notifications.discord import DiscordDispatcher
from app.adapters.notifications.pagerduty import PagerDutyDispatcher
from app.adapters.notifications.telegram import TelegramDispatcher
from app.adapters.notifications.twilio import TwilioDispatcher

__all__ = [
    "DeliveryResult",
    "DiscordDispatcher",
    "DispatcherConfigurationError",
    "NotificationDispatcher",
    "OutboundMessage",
    "PagerDutyDispatcher",
    "TelegramDispatcher",
    "TwilioDispatcher",
]
