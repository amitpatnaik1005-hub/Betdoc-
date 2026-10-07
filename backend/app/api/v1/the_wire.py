from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser
from app.core.config import settings
from app.domain.the_wire import WireAggregatorService
from app.domain.the_wire.live_providers import OddsApiScoreProvider, RssNewsProvider, VenueWeatherUnavailable
from app.schemas.the_wire import WireDashboard

MAX_MATCH_IDS: Final[int] = 50
MAX_MATCH_ID_LENGTH: Final[int] = 64

router = APIRouter(tags=["the_wire"])

wire_service = WireAggregatorService(
    news_provider=RssNewsProvider(settings.WIRE_NEWS_FEEDS),
    score_provider=OddsApiScoreProvider(
        settings.ODDS_API_KEY.get_secret_value() if settings.ODDS_API_KEY else None,
        settings.ODDS_API_BASE_URL,
        settings.odds_sport_keys,
    ),
    weather_provider=VenueWeatherUnavailable(),
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
