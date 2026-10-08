"""Polymarket public Gamma API: prediction-market moneylines for configured leagues (no key).

Docs: https://docs.polymarket.com/developers/gamma-markets-api/overview. Two calls per league:

* ``GET /sports`` (cached an hour per process) maps a league code (``epl``, ``nfl``) to its tag
  id and to whether event titles list the home side first (``ordering``).
* ``GET /markets?tag_id=..&sports_market_types=moneyline`` with an end-date window. Game markets
  end at kick-off, so the window is "started up to IN_PLAY_HOURS ago" to "LOOKAHEAD_DAYS ahead".
  This endpoint is used rather than ``/events`` because an NFL event embeds ~300 prop markets
  (18 MB for 20 games); filtered markets are ~100 KB per league.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from app.adapters.ingestion.base import BaseDataIngestor, IngestionBatch, IngestionError, SourcePayload
from app.core.config import Settings

SPORTS_CACHE_SECONDS = 3600.0
IN_PLAY_HOURS = 4
LOOKAHEAD_DAYS = 7
PAGE_SIZE = 100
MAX_PAGES = 3


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PolymarketIngestor(BaseDataIngestor):
    source_id = "polymarket"
    display_name = "Polymarket"
    description = "Public prediction-market moneylines (Gamma API, no key needed)."
    requires_api_key = False
    docs_url = "https://docs.polymarket.com/developers/gamma-markets-api/overview"

    _sports_cache: ClassVar[tuple[float, dict[str, dict[str, Any]]] | None] = None

    @classmethod
    def interval_seconds(cls, settings: Settings) -> float:
        return float(settings.POLYMARKET_POLL_INTERVAL_SEC)

    async def _sports(self, base: str) -> dict[str, dict[str, Any]]:
        cached = type(self)._sports_cache
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        data = await self._get_json(f"{base}/sports")
        if not isinstance(data, list):
            raise IngestionError("Polymarket: /sports did not return a list")
        index = {str(row["sport"]).lower(): row for row in data if isinstance(row, dict) and row.get("sport")}
        type(self)._sports_cache = (time.monotonic() + SPORTS_CACHE_SECONDS, index)
        return index

    async def fetch(self) -> IngestionBatch:
        base = self._settings.POLYMARKET_GAMMA_BASE_URL.rstrip("/")
        leagues = self._settings.polymarket_leagues
        if not leagues:
            raise IngestionError("Polymarket: POLYMARKET_LEAGUES is empty")
        sports = await self._sports(base)

        now = datetime.now(UTC)
        window = {
            "end_date_min": _iso(now - timedelta(hours=IN_PLAY_HOURS)),
            "end_date_max": _iso(now + timedelta(days=LOOKAHEAD_DAYS)),
        }
        payloads: list[SourcePayload] = []
        unknown: list[str] = []
        for league in leagues:
            meta = sports.get(league)
            tag_id = meta.get("primaryTagId") if meta else None
            if tag_id is None:
                unknown.append(league)
                continue
            markets: list[Any] = []
            for page in range(MAX_PAGES):
                rows = await self._get_json(
                    f"{base}/markets",
                    params={
                        "tag_id": str(tag_id),
                        "sports_market_types": "moneyline",
                        "active": "true",
                        "closed": "false",
                        "limit": str(PAGE_SIZE),
                        "offset": str(page * PAGE_SIZE),
                        **window,
                    },
                )
                if not isinstance(rows, list):
                    raise IngestionError(f"Polymarket: /markets did not return a list for {league}")
                markets.extend(rows)
                if len(rows) < PAGE_SIZE:
                    break
            payloads.append(
                SourcePayload(
                    key=league,
                    data={"league": league, "ordering": meta.get("ordering", "home") if meta else "home", "markets": markets},
                )
            )
        if not payloads:
            raise IngestionError(f"Polymarket: no known league in POLYMARKET_LEAGUES ({', '.join(unknown)})")
        return self._batch(payloads, {"leagues": [p.key for p in payloads], "unknown_leagues": unknown})
