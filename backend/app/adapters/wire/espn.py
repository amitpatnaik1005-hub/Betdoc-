"""ESPN's public site API (no key): a league's scoreboard for one day and one match's summary (Group 78).

The API is ESPN's own site feed, unofficial and undocumented: shapes are parsed defensively
(``app/domain/the_wire/espn_parse.py``) and ``WIRE_ESPN_ENABLED`` switches the source off. It answers single
dates only (``dates=YYYYMMDD``); ranges are refused.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from app.core.config import Settings


def client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.WIRE_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": settings.WIRE_USER_AGENT}, follow_redirects=True)


async def scoreboard(http: httpx.AsyncClient, settings: Settings, league: str, day: date) -> dict[str, Any]:
    response = await http.get(f"{settings.WIRE_ESPN_BASE_URL}/{league}/scoreboard", params={"dates": day.strftime("%Y%m%d")})
    response.raise_for_status()
    return response.json()


async def summary(http: httpx.AsyncClient, settings: Settings, league: str, event_id: str) -> dict[str, Any]:
    response = await http.get(f"{settings.WIRE_ESPN_BASE_URL}/{league}/summary", params={"event": event_id})
    response.raise_for_status()
    return response.json()
