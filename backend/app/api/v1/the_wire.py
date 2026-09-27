from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser
from app.domain.the_wire import (
    MockNewsProvider,
    MockScoreProvider,
    MockWeatherProvider,
    WireAggregatorService,
)
from app.schemas.the_wire import WireDashboard

MAX_MATCH_IDS: Final[int] = 50
MAX_MATCH_ID_LENGTH: Final[int] = 64

router = APIRouter(tags=["the_wire"])

wire_service = WireAggregatorService(
    news_provider=MockNewsProvider(),
    score_provider=MockScoreProvider(),
    weather_provider=MockWeatherProvider(),
)


@router.get("/dashboard", response_model=WireDashboard)
async def get_wire_dashboard(
    current_user: CurrentUser,  # noqa: ARG001 - enforces authentication
    match_ids: Annotated[
        str, Query(description="Comma-separated match IDs", max_length=4096)
    ] = "",
) -> WireDashboard:
    ids = list(dict.fromkeys(m.strip() for m in match_ids.split(",") if m.strip()))
    if len(ids) > MAX_MATCH_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"At most {MAX_MATCH_IDS} match IDs are allowed per request",
        )
    if any(len(m) > MAX_MATCH_ID_LENGTH for m in ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Match IDs must be at most {MAX_MATCH_ID_LENGTH} characters",
        )
    return await wire_service.fetch_dashboard(ids)
