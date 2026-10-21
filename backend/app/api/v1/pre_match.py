"""/pre-match: the stored pre-match board (Group 101, point b.13a). Developed for Amit Ashok Kumar Patnaik.

Reads never call a provider: they serve what the ingestion fleet last bought, with each fixture's tier, age and whether
its tier says it is stale. ``force-refresh`` buys the fixture's sport board now, at most once per
``PRE_MATCH_FORCE_REFRESH_COOLDOWN_SECONDS`` per sport and never past the fleet's breaker, quota reserve or cache shield.
Mounted for signed-in users (``router.py``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.domain.pre_match.cadence import freshness
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.schemas.pre_match import PreMatchFixtureDetail, PreMatchFixtureSummary, PreMatchRefreshResponse
from app.services import pre_match_odds_service as service
from app.services.omni_fleet import FleetDeps

logger = logging.getLogger("betdoc.pre_match")
router = APIRouter(tags=["Pre-Match Persistence"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
MatchId = Annotated[str, Path(min_length=1, max_length=128)]
_NOT_STORED = "Fixture not found in the pre-match store"


def fleet_deps(request: Request) -> FleetDeps:
    deps: FleetDeps | None = getattr(request.app.state, "fleet_deps", None)
    if deps is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Fleet is not initialised")
    return deps


def _summary_fields(row: PreMatchOddsSnapshotModel, settings: Settings, now: datetime) -> dict[str, object]:
    fresh = freshness(row.commence_time, row.last_polled_at, now, settings)
    return {
        "id": row.id, "match_id": row.match_id, "sport_key": row.sport_key, "home_team": row.home_team, "away_team": row.away_team,
        "commence_time": row.commence_time, "consensus_fair_odds": row.consensus_fair_odds or {}, "bookmaker_odds": row.bookmaker_odds or {},
        "last_polled_at": row.last_polled_at, "tier": fresh.tier, "age_seconds": round(fresh.age_seconds, 1),
        "max_age_seconds": fresh.max_age_seconds, "is_stale": fresh.is_stale,
    }


def _detail(row: PreMatchOddsSnapshotModel, settings: Settings, now: datetime) -> PreMatchFixtureDetail:
    return PreMatchFixtureDetail(**_summary_fields(row, settings, now), markets_json=row.markets_json or {})


@router.get("/fixtures", response_model=list[PreMatchFixtureSummary])
async def list_cached_fixtures(
    db: DbSession,
    settings: AppSettings,
    sport: Annotated[str | None, Query(pattern=r"^[a-z0-9_]+$", max_length=64, description="Odds API sport key (soccer_epl, cricket_ipl, ...) or all")] = None,
    window: Annotated[service.Window, Query(description="Kickoff within 3h, 12h, 24h, 72h, or all (PRE_MATCH_HORIZON_HOURS)")] = "all",
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> list[PreMatchFixtureSummary]:
    now = datetime.now(UTC)
    rows = await service.list_fixtures(db, settings, sport=None if sport in (None, "all") else sport, window=window, now=now, limit=limit)
    return [PreMatchFixtureSummary(**_summary_fields(r, settings, now)) for r in rows]


@router.get("/fixtures/{match_id}", response_model=PreMatchFixtureDetail)
async def get_fixture_detail(match_id: MatchId, db: DbSession, settings: AppSettings) -> PreMatchFixtureDetail:
    row = await service.get_fixture(db, match_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_STORED)
    return _detail(row, settings, datetime.now(UTC))


@router.post("/fixtures/{match_id}/force-refresh", response_model=PreMatchRefreshResponse)
async def force_refresh_fixture(match_id: MatchId, deps: Annotated[FleetDeps, Depends(fleet_deps)], settings: AppSettings) -> PreMatchRefreshResponse:
    now = datetime.now(UTC)
    try:
        outcome = await service.force_refresh(deps, match_id, now)
    except service.RefreshRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except service.RefreshFailed as exc:
        logger.warning("pre-match: force refresh of %s failed: %s", match_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The Odds API call failed: {exc}") from exc
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_STORED)
    row = outcome.snapshot
    return PreMatchRefreshResponse(match_id=row.match_id, status=outcome.status, synced_at=row.last_polled_at, source=outcome.source,
                                   detail=outcome.detail, fixture=_detail(row, settings, datetime.now(UTC)))
