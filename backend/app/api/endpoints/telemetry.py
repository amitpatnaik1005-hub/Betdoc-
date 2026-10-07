"""Dashboard telemetry endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_orchestrator
from app.domain.integration.orchestrator import OrchestratorService
from app.domain.integration.schemas import CascadeResult, PredictionResult, SubsystemStatus, TelemetryResponse

__all__ = ["CascadeResult", "PredictionResult", "SubsystemStatus", "TelemetryResponse", "router"]

router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


@router.get("/dashboard", response_model=TelemetryResponse, summary="Aggregated subsystem telemetry")
async def get_dashboard_telemetry(
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> TelemetryResponse:
    """Concurrent, fault-isolated snapshot of Vault, Oracle, Arena and Lab.

    A failing subsystem yields ``status="error"`` with ``last_error`` populated; the rest still report.
    """
    return await orchestrator.gather_dashboard_telemetry()

@router.post("/oracle/predictions/{event_id}", response_model=PredictionResult)
async def create_prediction(
    event_id: str,
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)]
) -> PredictionResult:
    return await orchestrator.execute_oracle_prediction_cycle(event_id)

@router.post("/risk/stop-loss", response_model=CascadeResult)
async def trigger_stop_loss(
    limit_breached: float,
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)]
) -> CascadeResult:
    return await orchestrator.trigger_stop_loss_cascade(limit_breached)
