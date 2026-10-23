"""Football-Data.org v4 (Group 112): fixtures, status, scores and league tables. Developed for Amit Ashok Kumar Patnaik.

Docs: https://docs.football-data.org/general/v4/. The key travels in the ``X-Auth-Token`` header. The free tier allows ten
requests a minute and a ten-day range per ``/matches`` call; one call covers every configured competition, and a table is
one call per competition (cached by the caller).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.adapters.sports_data import get_json
from app.domain.live_context import FixtureStatus, ProviderFixture, TableRow

PROVIDER = "football-data.org"
_STATUS: dict[str, FixtureStatus] = {
    "SCHEDULED": "SCHEDULED", "TIMED": "SCHEDULED", "IN_PLAY": "IN_PLAY", "LIVE": "IN_PLAY", "PAUSED": "PAUSED", "FINISHED": "FINISHED",
    "AWARDED": "FINISHED", "POSTPONED": "POSTPONED", "SUSPENDED": "SUSPENDED", "CANCELLED": "CANCELLED",
}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _names(team: Any) -> tuple[str, ...]:
    if not isinstance(team, Mapping):
        return ()
    return tuple(dict.fromkeys(str(team[k]) for k in ("name", "shortName") if team.get(k)))


def _goals(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_matches(payload: Any, competitions: Mapping[str, str]) -> list[ProviderFixture]:
    """``/matches`` -> fixtures of the configured competitions (code -> Odds API sport key). A match without both teams
    named or a kickoff is left out (the draw's placeholders)."""
    out: list[ProviderFixture] = []
    for raw in (payload or {}).get("matches") or [] if isinstance(payload, Mapping) else []:
        if not isinstance(raw, Mapping):
            continue
        code = str((raw.get("competition") or {}).get("code") or "")
        sport_key = competitions.get(code)
        kickoff = _time(raw.get("utcDate"))
        home, away = _names(raw.get("homeTeam")), _names(raw.get("awayTeam"))
        if sport_key is None or kickoff is None or not home or not away or raw.get("id") is None:
            continue
        score = raw.get("score") if isinstance(raw.get("score"), Mapping) else {}
        full = score.get("fullTime") or {}
        status = _STATUS.get(str(raw.get("status") or ""), "UNKNOWN")
        # fullTime includes extra time; a match that went past 90 minutes gives its regular-time score separately
        regular = full if score.get("duration") in (None, "REGULAR") else score.get("regularTime") or {}
        regulation = (_goals(regular.get("home")), _goals(regular.get("away"))) if status == "FINISHED" and isinstance(regular, Mapping) else (None, None)
        out.append(ProviderFixture(
            provider=PROVIDER, provider_id=str(raw["id"]), sport_key=sport_key, competition=str((raw.get("competition") or {}).get("name") or code),
            kickoff=kickoff, home=home, away=away, status=status,
            home_score=_goals(full.get("home")) if status in ("IN_PLAY", "PAUSED", "FINISHED") else None,
            away_score=_goals(full.get("away")) if status in ("IN_PLAY", "PAUSED", "FINISHED") else None,
            updated_at=_time(raw.get("lastUpdated")),
            regulation=None if None in regulation else (regulation[0], regulation[1]),  # type: ignore[arg-type]
        ))
    return out


def parse_table(payload: Any) -> list[TableRow]:
    """``/competitions/{code}/standings`` -> the overall table (the first TOTAL stage: a league's season, the Champions
    League's league phase). Group tables of a tournament are not one table and are left out."""
    stages = (payload or {}).get("standings") or [] if isinstance(payload, Mapping) else []
    total = [s for s in stages if isinstance(s, Mapping) and s.get("type") == "TOTAL"]
    if len(total) != 1:
        return []
    rows: list[TableRow] = []
    for raw in total[0].get("table") or []:
        names = _names(raw.get("team")) if isinstance(raw, Mapping) else ()
        position = raw.get("position") if isinstance(raw, Mapping) else None
        if names and isinstance(position, int):
            rows.append(TableRow(team=names, position=position, points=_goals(raw.get("points")), played=_goals(raw.get("playedGames"))))
    return rows


class FootballDataClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, base_url: str, timeout: float) -> None:
        self._http, self._base, self._timeout = http, base_url.rstrip("/"), timeout
        self._headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
        self.requests_available: int | None = None  # X-Requests-Available-Minute: the latest answer that carried it

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload, headers = await get_json(self._http, PROVIDER, f"{self._base}{path}", params=params, headers=self._headers, timeout=self._timeout)
        try:
            self.requests_available = int(headers["X-Requests-Available-Minute"])
        except (KeyError, ValueError):
            pass  # an answer without the counter leaves the last reading
        return payload

    async def fixtures(self, competitions: Mapping[str, str], date_from: date, date_to: date) -> list[ProviderFixture]:
        """Every match of the configured competitions between the two dates, one call."""
        if not competitions:
            return []
        payload = await self._get("/matches", {"competitions": ",".join(competitions), "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()})
        return parse_matches(payload, competitions)

    async def table(self, code: str) -> list[TableRow]:
        return parse_table(await self._get(f"/competitions/{code}/standings"))

    async def competitions(self) -> list[str]:
        """The competition codes the key's plan can read (the free tier lists its twelve)."""
        payload = await self._get("/competitions")
        return sorted(str(c["code"]) for c in (payload or {}).get("competitions") or [] if isinstance(c, Mapping) and c.get("code"))


def window(today: date, lookback_days: int, lookahead_days: int) -> tuple[date, date]:
    """The /matches range: yesterday's matches for their final score through the week ahead (inside the free tier's ten days)."""
    return today - timedelta(days=lookback_days), today + timedelta(days=lookahead_days)


__all__ = ["PROVIDER", "FootballDataClient", "parse_matches", "parse_table", "window"]
