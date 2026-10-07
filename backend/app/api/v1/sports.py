"""Multi-Sport Support router (prefix /sports) and a NaN-safe validation error handler."""

import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.sports.errors import (
    InvalidSportConfigError,
    SportInactiveError,
    SportNotFoundError,
    SportsDomainError,
)
from app.domain.sports.manager import SportsManager
from app.schemas.sports import (
    DlsRequest,
    DlsResponse,
    ProjectScoreRequest,
    ProjectScoreResponse,
    SportConfigRead,
    SportConfigUpdate,
    SpreadRequest,
    SpreadResponse,
    TennisGameRequest,
    TennisGameResponse,
)

logger = logging.getLogger("betdoc.sports")

router = APIRouter(prefix="/sports", tags=["Multi-Sport Support"])

_manager = SportsManager()


def get_sports_manager() -> SportsManager:
    return _manager


def _sanitize_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_non_finite(item) for item in value]
    return value


async def nan_safe_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 handler that survives NaN/Infinity echoed back in Pydantic's error `input` field.

    Register on the application: app.add_exception_handler(RequestValidationError, nan_safe_validation_exception_handler)
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _sanitize_non_finite(jsonable_encoder(errors))},
    )


def _http_error(exc: SportsDomainError) -> HTTPException:
    if isinstance(exc, SportNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, SportInactiveError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, InvalidSportConfigError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_400_BAD_REQUEST
    logger.warning("SPORTS: request rejected (%d): %s", code, exc.message)
    return HTTPException(status_code=code, detail=exc.message)


@router.get("/{sport_name}/config", response_model=SportConfigRead)
async def read_sport_config(
    sport_name: str,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> SportConfigRead:
    try:
        row = await manager.get_config(db, sport_name)
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return SportConfigRead.model_validate(row)


@router.put("/{sport_name}/config", response_model=SportConfigRead)
async def update_sport_config(
    sport_name: str,
    payload: SportConfigUpdate,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> SportConfigRead:
    try:
        row = await manager.update_config(db, sport_name, payload.config, payload.is_active)
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return SportConfigRead.model_validate(row)


@router.post("/cricket/calculate-dls", response_model=DlsResponse)
async def calculate_dls(
    payload: DlsRequest,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> DlsResponse:
    try:
        result = await manager.calculate_dls(db, payload.resources_left_pct, payload.original_target)
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return DlsResponse(**result)


@router.post("/cricket/project-score", response_model=ProjectScoreResponse)
async def project_score(
    payload: ProjectScoreRequest,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> ProjectScoreResponse:
    try:
        result = await manager.project_first_innings(
            db,
            current_score=payload.current_score,
            overs_bowled=payload.overs_bowled,
            wickets_lost=payload.wickets_lost,
            pitch_degradation_factor=payload.pitch_degradation_factor,
            total_overs=payload.total_overs,
        )
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return ProjectScoreResponse(**result)


@router.post("/basketball/calculate-spread", response_model=SpreadResponse)
async def calculate_spread(
    payload: SpreadRequest,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> SpreadResponse:
    try:
        result = await manager.calculate_basketball_spread(
            db,
            home_rating=payload.home_rating,
            away_rating=payload.away_rating,
            home_pace=payload.home_pace,
            away_pace=payload.away_pace,
            league_avg_pace=payload.league_avg_pace,
        )
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return SpreadResponse(**result)


@router.post("/tennis/calculate-game-prob", response_model=TennisGameResponse)
async def calculate_game_prob(
    payload: TennisGameRequest,
    db: AsyncSession = Depends(get_db),
    manager: SportsManager = Depends(get_sports_manager),
) -> TennisGameResponse:
    try:
        result = await manager.calculate_tennis_game(
            db,
            base_serve_prob=payload.base_serve_prob,
            player_surface_elo=payload.player_surface_elo,
            opponent_surface_elo=payload.opponent_surface_elo,
        )
    except SportsDomainError as exc:
        raise _http_error(exc) from exc
    return TennisGameResponse(**result)
