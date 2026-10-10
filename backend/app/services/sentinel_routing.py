"""The Sentinel's routing matrix: which channels hear which alerts.

Rows are the four severities plus ``HYPE`` (INFO alerts that go to a phone, not to the incident
channels: the daily market forecast, and the twin's vetted slips and pullout calls). Columns are the four dispatchers plus ``BROWSER``: the
Sentinel tab's HTML5 siren, which the frontend sounds for the rows that list it (FATAL by default).
One row in ``sentinel_routing``; a missing or malformed row falls back to ``DEFAULT_MATRIX``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sentinel import ChannelName, SentinelRouting, Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert

BROWSER = "BROWSER"
HYPE = "HYPE"
ROWS: tuple[str, ...] = (Severity.FATAL, Severity.CRITICAL, Severity.WARNING, Severity.INFO, HYPE)
COLUMNS: tuple[str, ...] = (*(c.value for c in ChannelName), BROWSER)

DEFAULT_MATRIX: dict[str, list[str]] = {
    Severity.FATAL: [ChannelName.TELEGRAM, ChannelName.DISCORD, ChannelName.TWILIO, ChannelName.PAGERDUTY, BROWSER],
    Severity.CRITICAL: [ChannelName.TELEGRAM, ChannelName.DISCORD, ChannelName.PAGERDUTY],
    Severity.WARNING: [ChannelName.DISCORD],
    Severity.INFO: [],
    HYPE: [ChannelName.TELEGRAM, ChannelName.TWILIO],
}


def normalise(raw: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Every row present, only known columns, in a stable order. Unknown rows are dropped."""
    out: dict[str, list[str]] = {}
    for row in ROWS:
        chosen = raw.get(row) if isinstance(raw, Mapping) and row in raw else DEFAULT_MATRIX[row]
        wanted = {str(c).upper() for c in chosen} if isinstance(chosen, Sequence) and not isinstance(chosen, str) else set()
        out[str(row)] = [c for c in COLUMNS if c in wanted]
    return out


def validate(raw: Mapping[str, Any]) -> list[str]:
    """Problems with a matrix an operator submitted (empty: it is fine)."""
    problems = [f"unknown row {row!r}" for row in raw if row not in ROWS]
    for row, chosen in raw.items():
        if not isinstance(chosen, Sequence) or isinstance(chosen, str):
            problems.append(f"row {row!r} must be a list of channels")
            continue
        problems += [f"unknown channel {c!r} in row {row!r}" for c in chosen if str(c).upper() not in COLUMNS]
    return problems


# INFO alerts the user acts on from their phone: the forecast, and the twin's slips and pullouts (Group 72)
PHONE_KINDS = frozenset({AlertKind.MARKET_HYPE, AlertKind.TWIN_SLIP_VETTED, AlertKind.TWIN_PULLOUT})


def row_of(alert: SentinelAlert) -> str:
    return HYPE if alert.kind in PHONE_KINDS else str(alert.severity)


def channels_for(alert: SentinelAlert, matrix: Mapping[str, Sequence[str]]) -> list[ChannelName]:
    """The dispatchers this alert goes to (the browser column is the frontend's business)."""
    return [ChannelName(c) for c in matrix.get(row_of(alert), ()) if c != BROWSER]


async def load_matrix(session: AsyncSession) -> dict[str, list[str]]:
    row = await session.get(SentinelRouting, 1)
    return normalise(row.matrix if row is not None else None)
