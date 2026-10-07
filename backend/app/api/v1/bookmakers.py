"""Bookmaker API gateway (v1)."""

import math
from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.bookmakers import manager
from app.domain.bookmakers.omniroute import OmniRouteClient, default_omniroute_client
from app.schemas.bookmakers import (
    BookmakerConfigCreate,
    BookmakerConfigRead,
    BookmakerConfigUpdate,
    OddsComparisonRequest,
    OddsComparisonResponse,
    OmniRouteExecuteRequest,
    OmniRouteExecuteResponse,
)

HTTP_422: Final[int] = 422

router = APIRouter(prefix="/bookmakers", tags=["bookmakers"])


def get_omniroute_client() -> OmniRouteClient:
    return default_omniroute_client


DbSession = Annotated[AsyncSession, Depends(get_db)]
OmniRoute = Annotated[OmniRouteClient, Depends(get_omniroute_client)]
BookmakerPathName = Annotated[str, Path(min_length=1, max_length=100)]


def _replace_non_finite(value: Any) -> Any:
    """Recursively convert NaN and +/-Infinity floats into strings."""
    if isinstance(value, float):
        return str(value) if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Mapping):
        return {
            (_replace_non_finite(key) if isinstance(key, float) else key): _replace_non_finite(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_replace_non_finite(item) for item in value]
    return value


async def nan_safe_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a 422 that can be serialized even when the rejected input held NaN or Infinity."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    encoded = jsonable_encoder(exc.errors())
    return JSONResponse(status_code=HTTP_422, content={"detail": _replace_non_finite(encoded)})


def register_bookmaker_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, nan_safe_validation_exception_handler)


_DOMAIN_ERROR_STATUS: Final[tuple[tuple[type[manager.BookmakerError], int], ...]] = (
    (manager.BookmakerConfigNotFoundError, status.HTTP_404_NOT_FOUND),
    (manager.BookmakerConfigConflictError, status.HTTP_409_CONFLICT),
    (manager.BookmakerInactiveError, status.HTTP_409_CONFLICT),
)


def _to_http_exception(exc: manager.BookmakerError) -> HTTPException:
    for error_type, status_code in _DOMAIN_ERROR_STATUS:
        if isinstance(exc, error_type):
            return HTTPException(status_code=status_code, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/config", response_model=list[BookmakerConfigRead])
async def list_bookmaker_configs(
    db: DbSession, active_only: Annotated[bool, Query()] = False
) -> list[BookmakerConfigRead]:
    configs = await manager.get_all_configs(db, active_only=active_only)
    return [BookmakerConfigRead.model_validate(config) for config in configs]


@router.post(
    "/config", response_model=BookmakerConfigRead, status_code=status.HTTP_201_CREATED
)
async def create_bookmaker_config(
    payload: BookmakerConfigCreate, db: DbSession
) -> BookmakerConfigRead:
    try:
        config = await manager.create_config(db, payload)
    except manager.BookmakerError as exc:
        raise _to_http_exception(exc) from exc
    return BookmakerConfigRead.model_validate(config)


@router.put("/{name}/config", response_model=BookmakerConfigRead)
async def update_bookmaker_config(
    name: BookmakerPathName, payload: BookmakerConfigUpdate, db: DbSession
) -> BookmakerConfigRead:
    try:
        config = await manager.update_config(db, name, payload)
    except manager.BookmakerError as exc:
        raise _to_http_exception(exc) from exc
    return BookmakerConfigRead.model_validate(config)


@router.post("/compare-odds", response_model=OddsComparisonResponse)
async def compare_odds(
    payload: OddsComparisonRequest, db: DbSession, omniroute_client: OmniRoute
) -> OddsComparisonResponse:
    try:
        outcome = await manager.compare_and_route(
            db,
            payload.odds,
            sport=payload.sport,
            league=payload.league,
            match=payload.match,
            market=payload.market,
            selection=payload.selection,
            execute=payload.execute,
            stake=payload.stake,
            omniroute_client=omniroute_client,
        )
    except manager.BookmakerError as exc:
        raise _to_http_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc
    return OddsComparisonResponse.model_validate(outcome)


@router.post("/omniroute/execute", response_model=OmniRouteExecuteResponse)
async def execute_omniroute_bet(
    payload: OmniRouteExecuteRequest, db: DbSession, omniroute_client: OmniRoute
) -> OmniRouteExecuteResponse:
    try:
        result = await manager.execute_route(
            db,
            payload.bookmaker_name,
            payload.model_dump(exclude={"bookmaker_name"}, exclude_none=True),
            omniroute_client=omniroute_client,
        )
    except manager.BookmakerError as exc:
        raise _to_http_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc
    return OmniRouteExecuteResponse.model_validate(result)
