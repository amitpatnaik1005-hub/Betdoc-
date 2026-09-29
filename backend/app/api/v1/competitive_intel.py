"""R&D Competitive Intel router. Mount with prefix="/rnd/competitive-intel"."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.competitive_intel.errors import CompetitiveIntelDomainError, SiteNotFoundError
from app.domain.competitive_intel.manager import CompetitiveIntelManager
from app.schemas.competitive_intel import (
    BotRead,
    DashboardResponse,
    FeatureGapRead,
    MarkdownReportResponse,
    ScanResponse,
)

logger = logging.getLogger("betdoc.spy")

router = APIRouter(tags=["R&D Competitive Intel"])

_manager = CompetitiveIntelManager()


def get_competitive_intel_manager() -> CompetitiveIntelManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[CompetitiveIntelManager, Depends(get_competitive_intel_manager)]


def _to_dashboard(payload: dict[str, list]) -> DashboardResponse:
    return DashboardResponse(
        bots=[BotRead.model_validate(bot) for bot in payload["bots"]],
        gaps=[FeatureGapRead.model_validate(gap) for gap in payload["gaps"]],
    )


@router.get("", response_model=DashboardResponse)
async def get_competitive_intel_dashboard(db: DbSession, manager: Manager) -> DashboardResponse:
    try:
        dashboard = await manager.get_dashboard(db)
        if not dashboard["bots"]:
            logger.info("SPY-BOT: no scouts deployed yet; running an initial scan.")
            await manager.trigger_scan(db)
            dashboard = await manager.get_dashboard(db)
    except CompetitiveIntelDomainError as exc:
        logger.warning("SPY-BOT: dashboard failed: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return _to_dashboard(dashboard)


@router.post("/scan", response_model=ScanResponse)
async def trigger_competitive_scan(db: DbSession, manager: Manager) -> ScanResponse:
    try:
        await manager.trigger_scan(db)
    except CompetitiveIntelDomainError as exc:
        logger.warning("SPY-BOT: scan failed: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    logger.info("SPY-BOT: scan requested via API and completed.")
    return ScanResponse(status="scan_complete")


@router.get("/reports/{site_name}", response_model=MarkdownReportResponse)
async def get_comparison_report(site_name: str, manager: Manager) -> MarkdownReportResponse:
    try:
        markdown = manager.generate_markdown_report(site_name)
    except SiteNotFoundError as exc:
        logger.warning("SPY-BOT: report requested for untracked site %r.", site_name)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except CompetitiveIntelDomainError as exc:
        logger.warning("SPY-BOT: report request rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return MarkdownReportResponse(markdown=markdown)
