"""R&D Competitive Intel router (FA-7) and the Group 85 live market consensus (Commanders KARNA & PANINI).
Mount with prefix="/rnd/competitive-intel". Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Analytics Fleet.

    GET  ""                               the spy-bot dashboard (FA-7)
    POST /scan                            run the spy bots (FA-7)
    GET  /reports/{site_name}             a feature comparison report (FA-7)
    GET  /live-consensus                  Parimatch and 1xBet against the sharp zero-vig price per live selection (?sport=&public_trap_only=)
    POST /live-consensus/sync             rebuild the live board's consensus, benchmarks and public-pick grades now
    GET  /bookmaker-benchmarks            measured margins and price shares per book and sport (?sport=)
    GET  /public-traps                    public picks graded by BetDoc (?sport=)
    POST /public-traps                    record a named source's public pick (admin; nothing is scraped)
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_db
from app.core.config import Settings, get_settings
from app.domain.competitive_intel.errors import CompetitiveIntelDomainError, SiteNotFoundError
from app.domain.competitive_intel.manager import CompetitiveIntelManager
from app.schemas.competitive_intel import (
    BookmakerBenchmarkRead,
    BotRead,
    DashboardResponse,
    FeatureGapRead,
    LiveConsensusSyncResponse,
    MarkdownReportResponse,
    PublicConsensusSnapshotRead,
    PublicPickIn,
    PublicTrapSignalRead,
    ScanResponse,
)
from app.services.competitive_intel_live_service import CompetitiveIntelLiveService, LiveIntelRefusal

logger = logging.getLogger("betdoc.spy")

router = APIRouter(tags=["R&D Competitive Intel"])

_manager = CompetitiveIntelManager()
_live_service = CompetitiveIntelLiveService()


def get_competitive_intel_manager() -> CompetitiveIntelManager:
    return _manager


def get_competitive_intel_live_service() -> CompetitiveIntelLiveService:
    return _live_service


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[CompetitiveIntelManager, Depends(get_competitive_intel_manager)]
LiveService = Annotated[CompetitiveIntelLiveService, Depends(get_competitive_intel_live_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]


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


# ---------------------------------------------------------------- Group 85: live market consensus (KARNA & PANINI)
def _redis(request: Request):  # noqa: ANN202 - the app's Redis client, or None
    return getattr(request.app.state, "redis", None)


def _refused(exc: LiveIntelRefusal) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.message})


@router.get("/live-consensus", response_model=list[PublicConsensusSnapshotRead])
async def get_live_consensus(request: Request, db: DbSession, service: LiveService, settings: AppSettings,
                             sport: Annotated[str | None, Query(max_length=40, description="Cricket, Football, Tennis ... or All")] = None,
                             public_trap_only: Annotated[bool, Query(description="Only the selections flagged as public traps")] = False) -> list[PublicConsensusSnapshotRead]:
    rows = await service.get_live_consensus_board(db, _redis(request), settings, sport=sport, public_trap_only=public_trap_only)
    return [PublicConsensusSnapshotRead.model_validate(r) for r in rows]


@router.post("/live-consensus/sync", response_model=LiveConsensusSyncResponse)
async def sync_live_consensus(request: Request, db: DbSession, service: LiveService, settings: AppSettings) -> LiveConsensusSyncResponse:
    try:
        summary = await service.sync_live_consensus(db, _redis(request), settings)
    except LiveIntelRefusal as exc:
        raise _refused(exc) from exc
    return LiveConsensusSyncResponse(status="synced", **summary)


@router.get("/bookmaker-benchmarks", response_model=list[BookmakerBenchmarkRead])
async def get_bookmaker_benchmarks(request: Request, db: DbSession, service: LiveService, settings: AppSettings,
                                   sport: Annotated[str | None, Query(max_length=40)] = None) -> list[BookmakerBenchmarkRead]:
    rows = await service.get_bookmaker_benchmarks(db, _redis(request), settings, sport=sport)
    return [BookmakerBenchmarkRead.model_validate(r) for r in rows]


@router.get("/public-traps", response_model=list[PublicTrapSignalRead])
async def get_public_traps(request: Request, db: DbSession, service: LiveService, settings: AppSettings,
                           sport: Annotated[str | None, Query(max_length=40)] = None) -> list[PublicTrapSignalRead]:
    rows = await service.get_public_traps_feed(db, _redis(request), settings, sport=sport)
    return [PublicTrapSignalRead.model_validate(r) for r in rows]


@router.post("/public-traps", response_model=PublicTrapSignalRead, status_code=status.HTTP_201_CREATED)
async def record_public_pick(body: PublicPickIn, request: Request, db: DbSession, service: LiveService, settings: AppSettings, admin: CurrentAdmin) -> PublicTrapSignalRead:
    try:
        row = await service.record_public_pick(db, _redis(request), settings, body, admin.username)
    except LiveIntelRefusal as exc:
        raise _refused(exc) from exc
    return PublicTrapSignalRead.model_validate(row)
