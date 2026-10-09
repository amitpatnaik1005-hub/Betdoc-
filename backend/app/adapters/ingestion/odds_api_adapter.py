"""The Odds API (v4): bookmaker h2h prices for every configured sport key.

Docs: https://the-odds-api.com/liveapi/guides/v4/. The key travels as the ``apiKey`` query
parameter, so nothing here ever logs or raises with a URL. Each odds request costs
``regions x markets`` credits; ``x-requests-remaining`` / ``x-requests-used`` are tracked so the
fleet can fail over before the account runs dry, and fetching stops under ``ODDS_QUOTA_FLOOR``.
``GET /sports`` costs nothing and still reports the quota: it is the probe that notices a reset.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar

import httpx

from app.adapters.ingestion.base import BaseDataIngestor, IngestionBatch, IngestionError, QuotaExhaustedError, SourcePayload
from app.core.config import Settings

_SPORT_KEY_RE = re.compile(r"^[a-z0-9_]+$")
BULK_MARKETS = ("h2h", "spreads", "totals")  # what /sports/{sport}/odds serves; btts is per event only


def odds_api_markets(settings: Settings) -> str:
    """``ODDS_API_MARKETS`` restricted to what the bulk endpoint serves, h2h always first. Each market
    multiplies the credits a call costs, so the default stays "h2h"."""
    wanted = {m.strip().lower() for m in settings.ODDS_API_MARKETS.split(",") if m.strip()}
    return ",".join(m for m in BULK_MARKETS if m == "h2h" or m in wanted)


class OddsApiIngestor(BaseDataIngestor):
    source_id = "odds_api"
    display_name = "The Odds API"
    description = "Licensed bookmaker prices (h2h), de-vigged into a consensus probability."
    requires_api_key = True
    docs_url = "https://the-odds-api.com/liveapi/guides/v4/"
    requests_per_minute = 30.0
    burst = 5
    quota_remaining_header = "x-requests-remaining"
    quota_used_header = "x-requests-used"

    # Last quota reading per process, so the floor is checked BEFORE a credit is spent.
    _last_quota: ClassVar[float | None] = None

    @classmethod
    def interval_seconds(cls, settings: Settings) -> float:
        return float(max(10, settings.ODDS_POLLING_INTERVAL_SEC))

    @classmethod
    def coverage(cls, settings: Settings) -> dict[str, str]:
        """Canonical sport key -> provider-native key (identical for The Odds API)."""
        return {s: s for s in settings.odds_sport_keys if _SPORT_KEY_RE.match(s)}

    def _on_response(self, response: httpx.Response) -> None:
        super()._on_response(response)
        if self._quota_remaining is not None:
            type(self)._last_quota = self._quota_remaining

    async def probe(self) -> IngestionBatch:
        await self._get_json(f"{self._settings.ODDS_API_BASE_URL.rstrip('/')}/sports", params={"apiKey": self._api_key or ""})
        return self._batch([], {"probe": True})

    async def fetch(self, scope: Sequence[str] | None = None) -> IngestionBatch:
        floor = self._settings.ODDS_QUOTA_FLOOR
        covered = self.coverage(self._settings)
        sports = [covered[s] for s in (scope if scope is not None else covered) if s in covered]
        if not sports:
            raise IngestionError("The Odds API: no valid sport key in scope (ODDS_SPORT_KEYS)")

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
                    "markets": odds_api_markets(self._settings),
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )
            if not isinstance(data, list):
                raise IngestionError(f"The Odds API: expected a list for {sport}, got {type(data).__name__}")
            payloads.append(SourcePayload(key=sport, data=data))
        return self._batch(payloads, {"sports": sports, "regions": self._settings.ODDS_API_REGIONS})
