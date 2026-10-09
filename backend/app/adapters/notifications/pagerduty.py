"""PagerDuty: incidents through the Events API v2.

Every alert is a ``trigger`` (https://developer.pagerduty.com/docs/events-api-v2/trigger-events/) whose
``dedup_key`` is the alert's incident key, so a dependency that flaps raises one incident, not ten,
and the alert that ``resolves`` it (the dependency back up, Garuda beating again) sends ``resolve``
with the same key. FATAL maps to ``critical``, CRITICAL to ``error``, WARNING to ``warning``, INFO to
``info``. The integration's routing key is the credential.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

import httpx

from app.adapters.notifications.base import DispatcherConfigurationError, NotificationDispatcher, OutboundMessage, check
from app.models.sentinel import ChannelName, Severity

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
PD_SEVERITY = {Severity.FATAL: "critical", Severity.CRITICAL: "error", Severity.WARNING: "warning", Severity.INFO: "info"}


class PagerDutyDispatcher(NotificationDispatcher):
    channel: ClassVar[ChannelName] = ChannelName.PAGERDUTY

    def __init__(self, http: httpx.AsyncClient, *, routing_key: str, source: str = "betdoc", **kwargs: object) -> None:
        super().__init__(http, **kwargs)  # type: ignore[arg-type]
        if not routing_key or len(routing_key) != 32:
            raise DispatcherConfigurationError("a PagerDuty integration (routing) key has 32 characters")
        self.routing_key = routing_key
        self.source = source or "betdoc"

    def secrets(self) -> Iterable[str]:
        return (self.routing_key,)

    def dedup_key(self, message: OutboundMessage) -> str:
        return (message.dedupe_key or f"{message.kind}:{message.alert_id}")[:255]

    def payload(self, message: OutboundMessage) -> dict[str, object]:
        if message.resolves:
            return {"routing_key": self.routing_key, "event_action": "resolve", "dedup_key": self.dedup_key(message)}
        return {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": self.dedup_key(message),
            "payload": {
                "summary": message.headline()[:1024],
                "source": self.source,
                "severity": PD_SEVERITY[message.severity],
                "timestamp": message.occurred_at.isoformat(),
                "component": message.source,
                "class": message.kind,
                "custom_details": {"body": message.body[:4000], **({"batched": message.batched} if message.batched else {}), **message.detail},
            },
        }

    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        if "event" in done:
            return
        check(await self.http.post(EVENTS_URL, json=self.payload(message)))
        done.add("event")
