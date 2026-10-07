"""THE VAULT - CFO Advisory router. Mount with prefix="/the-vault/cfo"."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.cfo.errors import AlertNotFoundError, CfoDomainError
from app.domain.cfo.manager import MAX_ALERT_LIMIT, CfoManager
from app.schemas.cfo import (
    AdvisoryRequest,
    AdvisoryResponse,
    AlertRead,
    StressTestRequest,
    StressTestResult,
    TaxCalculateRequest,
    TaxRecordRead,
)

logger = logging.getLogger("betdoc.the_vault.todarmal")

router = APIRouter(tags=["THE VAULT: CFO Advisory"])

_manager = CfoManager()


def get_cfo_manager() -> CfoManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[CfoManager, Depends(get_cfo_manager)]


def _bad_request(exc: CfoDomainError) -> HTTPException:
    logger.warning("[TODAR MAL]: request rejected: %s", exc.message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)


@router.post("/advisory", response_model=AdvisoryResponse)
async def create_advisory(payload: AdvisoryRequest, db: DbSession, manager: Manager) -> AdvisoryResponse:
    try:
        advisory = await manager.generate_advisory_report(
            db,
            user_id=None,
            current_bankroll=payload.current_bankroll,
            active_exposure=payload.active_exposure,
            variance_threshold_pct=payload.variance_threshold_pct,
        )
    except CfoDomainError as exc:
        raise _bad_request(exc) from exc
    return AdvisoryResponse.model_validate(advisory)


@router.post("/stress-test", response_model=StressTestResult)
async def create_stress_test(payload: StressTestRequest, db: DbSession, manager: Manager) -> StressTestResult:
    try:
        result = await manager.run_stress_test(
            db,
            user_id=None,
            scenario=payload.scenario,
            portfolio_value=payload.portfolio_value,
            shock_pct=payload.shock_pct,
            survival_threshold_pct=payload.survival_threshold_pct,
        )
    except CfoDomainError as exc:
        raise _bad_request(exc) from exc
    return StressTestResult.model_validate(result)


@router.post("/taxes", response_model=TaxRecordRead)
async def calculate_taxes(payload: TaxCalculateRequest, db: DbSession, manager: Manager) -> TaxRecordRead:
    try:
        record = await manager.calculate_tax(
            db,
            user_id=None,
            year=payload.year,
            total_profit=payload.total_profit,
            tax_allowance=payload.tax_allowance,
            tax_rate_pct=payload.tax_rate_pct,
        )
    except CfoDomainError as exc:
        raise _bad_request(exc) from exc
    return TaxRecordRead.model_validate(record)


@router.get("/alerts", response_model=list[AlertRead])
async def list_alerts(
    db: DbSession,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=MAX_ALERT_LIMIT)] = 50,
) -> list[AlertRead]:
    try:
        alerts = await manager.get_unread_alerts(db, user_id=None, limit=limit)
    except CfoDomainError as exc:
        raise _bad_request(exc) from exc
    return [AlertRead.model_validate(alert) for alert in alerts]


@router.patch("/alerts/{alert_id}/read", response_model=AlertRead)
async def mark_alert_read(alert_id: UUID, db: DbSession, manager: Manager) -> AlertRead:
    try:
        alert = await manager.mark_alert_read(db, alert_id=alert_id, user_id=None)
    except AlertNotFoundError as exc:
        logger.warning("[TODAR MAL]: alert %s not found.", alert_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except CfoDomainError as exc:
        raise _bad_request(exc) from exc
    return AlertRead.model_validate(alert)
