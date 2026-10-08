"""The Odds API (v4): bookmaker h2h prices for every configured sport key.

Docs: https://the-odds-api.com/liveapi/guides/v4/. The key travels as the ``apiKey`` query
parameter, so nothing here ever logs or raises with a URL. Each request costs
``regions x markets`` credits; the ``x-requests-remaining`` header is tracked and fetching stops
before the account drops under ``ODDS_QUOTA_FLOOR``.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx

from app.adapters.ingestion.base import BaseDataIngestor, IngestionBatch, IngestionError, QuotaExhaustedError, SourcePayload
from app.core.config import Settings

_SPORT_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class OddsApiIngestor(BaseDataIngestor):
    source_id = "odds_api"
    display_name = "The Odds API"
    description = "Licensed bookmaker prices (h2h), de-vigged into a consensus probability."
    requires_api_key = True
    docs_url = "https://the-odds-api.com/liveapi/guides/v4/"

    # Last quota reading per process, so the floor is checked BEFORE a credit is spent.
    _last_quota: ClassVar[float | None] = None

    @classmethod
    def interval_seconds(cls, settings: Settings) -> float:
        return float(max(10, settings.ODDS_POLLING_INTERVAL_SEC))

    def _on_response(self, response: httpx.Response) -> None:
        raw = response.headers.get("x-requests-remaining")
        if raw is None:
            return
        try:
            remaining = float(raw)
        except ValueError:
            return
        self._quota_remaining = remaining
        type(self)._last_quota = remaining

    async def fetch(self) -> IngestionBatch:
        floor = self._settings.ODDS_QUOTA_FLOOR
        sports = [s for s in self._settings.odds_sport_keys if _SPORT_KEY_RE.match(s)]
        if not sports:
            raise IngestionError("The Odds API: ODDS_SPORT_KEYS has no valid sport key")

        base = self._settings.ODDS_API_BASE_URL.rstrip("/")
        payloads: list[SourcePayload] = []
        for sport in sports:
            last = type(self)._last_quota
            if last is not None and last < floor:
                raise QuotaExhaustedError(f"The Odds API: {last:g} credits left, below the floor of {floor}")
            data: Any = await self._get_json(
                f"{base}/sports/{sport}/odds",
                params={
                    "apiKey": self._api_key or "",
                    "regions": self._settings.ODDS_API_REGIONS,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )
            if not isinstance(data, list):
                raise IngestionError(f"The Odds API: expected a list for {sport}, got {type(data).__name__}")
            payloads.append(SourcePayload(key=sport, data=data))
        return self._batch(payloads, {"sports": sports, "regions": self._settings.ODDS_API_REGIONS})
