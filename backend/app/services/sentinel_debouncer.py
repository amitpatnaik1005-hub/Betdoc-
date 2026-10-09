"""The Sentinel's spam debouncer: at most one CRITICAL message per channel per window.

A cascading failure raises CRITICAL alerts in bursts (every bookmaker source, then every bot, then
every margin check). Paging someone forty times in a minute buries the first, most useful message,
so per channel:

* the first CRITICAL goes out at once and opens a window of ``SENTINEL_DEBOUNCE_SECONDS`` (30s);
* every CRITICAL offered while the window is open, or while earlier ones are still held, is held;
* when the window closes, everything held leaves together as one digest (``Digest``), which is that
  window's message and opens the next one.

So for each channel, any two CRITICAL-class messages (a single alert or a digest) are at least one
window apart, and every CRITICAL offered goes out exactly once: alone, or inside exactly one digest.
FATAL is never held (a dead man's switch must not wait 30 seconds) and does not use up the window;
INFO and WARNING are not debounced (the routing matrix keeps them off the noisy channels).

Pure and clock-injected: the dispatcher holds one per process and calls ``due`` on every loop; the
held alerts stay unacknowledged on the stream until their digest is delivered, so a dispatcher that
dies mid-window loses nothing (the next leader reclaims and re-offers them).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.models.sentinel import Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert


class Decision(StrEnum):
    SEND = "SEND"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class Digest:
    """Everything one channel held through one window."""

    id: uuid.UUID
    channel: str
    alerts: tuple[SentinelAlert, ...]
    opened_at: datetime  # when the window that held them opened (the last message's time)
    released_at: datetime

    def as_alert(self) -> SentinelAlert:
        kinds = Counter(a.kind for a in self.alerts)
        lines = [f"{a.occurred_at:%H:%M:%S} {a.kind}: {a.title}" for a in self.alerts[:25]]
        if len(self.alerts) > 25:
            lines.append(f"... and {len(self.alerts) - 25} more")
        summary = ", ".join(f"{n}x {k}" for k, n in kinds.most_common())
        return SentinelAlert(
            id=self.id,
            kind=AlertKind.DIGEST,
            severity=Severity.CRITICAL,
            title=f"{len(self.alerts)} critical alert{'s' if len(self.alerts) != 1 else ''} held by the debouncer ({summary})"[:200],
            body="\n".join(lines),
            source="sentinel.debouncer",
            detail={"alert_ids": [str(a.id) for a in self.alerts], "kinds": dict(kinds), "opened_at": self.opened_at.isoformat(), "released_at": self.released_at.isoformat()},
            occurred_at=self.released_at,
        )


@dataclass(slots=True)
class _Channel:
    last_sent: datetime | None = None
    held: list[SentinelAlert] = field(default_factory=list)
    sent: int = 0
    digests: int = 0
    batched: int = 0


class SpamDebouncer:
    def __init__(self, window_seconds: float) -> None:
        if window_seconds <= 0:
            raise ValueError("the debounce window must be positive")
        self.window = timedelta(seconds=window_seconds)
        self._channels: dict[str, _Channel] = {}

    def _state(self, channel: str) -> _Channel:
        return self._channels.setdefault(channel, _Channel())

    def offer(self, channel: str, alert: SentinelAlert, now: datetime) -> Decision:
        """SEND now, or HOLD for this channel's next digest. Only CRITICAL is ever held."""
        if alert.severity is not Severity.CRITICAL:
            return Decision.SEND
        state = self._state(channel)
        if not state.held and (state.last_sent is None or now - state.last_sent >= self.window):
            state.last_sent = now
            state.sent += 1
            return Decision.SEND
        state.held.append(alert)
        state.batched += 1
        return Decision.HOLD

    def due(self, now: datetime) -> list[Digest]:
        """The digests whose window has closed, one per channel; each opens that channel's next window."""
        out: list[Digest] = []
        for channel, state in self._channels.items():
            if state.held and state.last_sent is not None and now - state.last_sent >= self.window:
                out.append(Digest(uuid.uuid4(), channel, tuple(state.held), state.last_sent, now))
                state.held = []
                state.last_sent = now
                state.digests += 1
        return out

    def next_due(self) -> datetime | None:
        """When the earliest held digest becomes due (None: nothing is held)."""
        times = [s.last_sent + self.window for s in self._channels.values() if s.held and s.last_sent is not None]
        return min(times) if times else None

    def holding(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._state(channel).held)
        return sum(len(s.held) for s in self._channels.values())

    def held_ids(self) -> set[uuid.UUID]:
        return {a.id for s in self._channels.values() for a in s.held}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per channel, for the Sentinel tab."""
        return {
            channel: {
                "holding": len(s.held),
                "last_sent": s.last_sent.isoformat() if s.last_sent else None,
                "window_closes": (s.last_sent + self.window).isoformat() if s.last_sent else None,
                "sent": s.sent,
                "digests": s.digests,
                "batched": s.batched,
            }
            for channel, s in self._channels.items()
        }
