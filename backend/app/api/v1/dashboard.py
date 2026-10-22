from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.domain.dashboard import build_dashboard_summary, fetch_activity_feed
from app.schemas.dashboard import ActivityEvent, DashboardSummary

router = APIRouter(tags=["dashboard"])

@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary(
    current_user: CurrentUser,
    db: DbSession,
    tz_offset_hours: Annotated[float, Query(ge=-12.0, le=14.0)] = 0.0,
) -> DashboardSummary:
    try:
        return await build_dashboard_summary(current_user.id, db, tz_offset_hours)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/activity-feed", response_model=list[ActivityEvent])
async def get_activity_feed(
    current_user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ActivityEvent]:
    return await fetch_activity_feed(current_user.id, db, limit)

