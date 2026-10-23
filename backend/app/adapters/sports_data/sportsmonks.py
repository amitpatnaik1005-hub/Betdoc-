"""Sportmonks Football API v3 (Group 112): fixtures, state, scores and league positions. Developed for Amit Ashok Kumar Patnaik.

Docs: https://docs.sportmonks.com/football. The token is the ``api_token`` query parameter, so nothing here logs or raises
with a URL. A plan reads only its own leagues (the free plan: Denmark's Superliga and Scotland's Premiership); the leagues
to read come from ``SPORTSMONKS_LEAGUE_SPORT_KEYS``, each paired against the Odds API sport key it names.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.adapters.sports_data import get_json
from app.domain.live_context import FixtureStatus, ProviderFixture

PROVIDER = "sportmonks"
MAX_PAGES = 5  # 50 fixtures a page: a week of two leagues is one page
_STATE: dict[str, FixtureStatus] = {
    "NS": "SCHEDULED", "TBA": "SCHEDULED",
    "INPLAY_1ST_HALF": "IN_PLAY", "INPLAY_2ND_HALF": "IN_PLAY", "INPLAY_ET": "IN_PLAY", "INPLAY_ET_2ND_HALF": "IN_PLAY", "INPLAY_PENALTIES": "IN_PLAY",
    "HT": "PAUSED", "BREAK": "PAUSED", "EXTRA_TIME_BREAK": "PAUSED", "PEN_BREAK": "PAUSED",
    "FT": "FINISHED", "AET": "FINISHED", "FT_PEN": "FINISHED", "AWARDED": "FINISHED", "WO": "FINISHED",
    "POSTPONED": "POSTPONED", "DELAYED": "POSTPONED",
    "SUSPENDED": "SUSPENDED", "INTERRUPTED": "SUSPENDED", "ABANDONED": "SUSPENDED",
    "CANCELLED": "CANCELLED", "DELETED": "CANCELLED",
}


def _kickoff(raw: Mapping[str, Any]) -> datetime | None:
    stamp = raw.get("starting_at_timestamp")
    if isinstance(stamp, int) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, UTC)
    text = raw.get("starting_at")
    if isinstance(text, str) and text:
        try:
            return datetime.fromisoformat(text).replace(tzinfo=UTC)  # v3 answers in UTC unless asked for a zone
        except ValueError:
            return None
    return None


def parse_fixtures(payload: Any, leagues: Mapping[int, str]) -> list[ProviderFixture]:
    """``/fixtures/between`` (with participants, scores and state) -> fixtures of the configured leagues."""
    out: list[ProviderFixture] = []
    for raw in (payload or {}).get("data") or [] if isinstance(payload, Mapping) else []:
        if not isinstance(raw, Mapping):
            continue
        sport_key = leagues.get(raw.get("league_id")) if isinstance(raw.get("league_id"), int) else None
        kickoff = _kickoff(raw)
        sides: dict[str, Mapping[str, Any]] = {}
        for team in raw.get("participants") or []:
            location = ((team or {}).get("meta") or {}).get("location") if isinstance(team, Mapping) else None
            if location in ("home", "away") and team.get("name"):
                sides[location] = team
        if sport_key is None or kickoff is None or len(sides) != 2 or raw.get("id") is None:
            continue
        goals: dict[str, int] = {}
        for score in raw.get("scores") or []:
            if isinstance(score, Mapping) and score.get("description") == "CURRENT":
                detail = score.get("score") or {}
                side, value = detail.get("participant"), detail.get("goals")
                if side in ("home", "away") and isinstance(value, int) and value >= 0:
                    goals[side] = value
        state = str(((raw.get("state") or {}).get("developer_name")) or "") if isinstance(raw.get("state"), Mapping) else ""
        status = _STATE.get(state, "UNKNOWN")
        started = status in ("IN_PLAY", "PAUSED", "FINISHED")

        def position(side: str) -> int | None:
            value = (sides[side].get("meta") or {}).get("position")
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

        out.append(ProviderFixture(
            provider=PROVIDER, provider_id=str(raw["id"]), sport_key=sport_key, competition=str((raw.get("league") or {}).get("name") or raw.get("league_id")),
            kickoff=kickoff, home=(str(sides["home"]["name"]),), away=(str(sides["away"]["name"]),), status=status,
            home_score=goals.get("home") if started and len(goals) == 2 else None, away_score=goals.get("away") if started and len(goals) == 2 else None,
            home_position=position("home"), away_position=position("away"),
            # FT is a match decided in 90 minutes: its current score is the regular-time score (AET / FT_PEN are not)
            regulation=(goals["home"], goals["away"]) if state == "FT" and len(goals) == 2 else None,
        ))
    return out


class SportmonksClient:
    def __init__(self, http: httpx.AsyncClient, api_token: str, base_url: str, timeout: float) -> None:
        self._http, self._base, self._timeout, self._token = http, base_url.rstrip("/"), timeout, api_token
        self.rate_limit_remaining: int | None = None

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        payload, _ = await get_json(self._http, PROVIDER, f"{self._base}{path}", params={"api_token": self._token, **params}, timeout=self._timeout)
        limit = (payload or {}).get("rate_limit") if isinstance(payload, Mapping) else None
        remaining = (limit or {}).get("remaining") if isinstance(limit, Mapping) else None
        self.rate_limit_remaining = remaining if isinstance(remaining, int) else None
        return payload

    async def fixtures(self, leagues: Mapping[int, str], start: date, end: date) -> list[ProviderFixture]:
        """Every fixture of the configured leagues between the two dates (paged, at most MAX_PAGES calls)."""
        if not leagues:
            return []
        params = {"include": "participants;scores;state;league", "filters": f"fixtureLeagues:{','.join(str(i) for i in leagues)}", "per_page": 50}
        out: list[ProviderFixture] = []
        for page in range(1, MAX_PAGES + 1):
            payload = await self._get(f"/football/fixtures/between/{start.isoformat()}/{end.isoformat()}", {**params, "page": page})
            out.extend(parse_fixtures(payload, leagues))
            if not ((payload or {}).get("pagination") or {}).get("has_more"):
                break
        return out

    async def leagues(self) -> list[tuple[int, str]]:
        """The leagues the token's plan can read."""
        payload = await self._get("/football/leagues", {"per_page": 50})
        return [(int(lg["id"]), str(lg.get("name") or lg["id"])) for lg in (payload or {}).get("data") or [] if isinstance(lg, Mapping) and isinstance(lg.get("id"), int)]


__all__ = ["PROVIDER", "SportmonksClient", "parse_fixtures"]
