from __future__ import annotations

import random
from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/v1/models", tags=["telemetry"])

class PredictiveModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str
    status: str
    roi_percentage: float
    strike_rate: float
    sharpe_ratio: float
    max_drawdown: float
    open_positions: int
    updated_at: str

MOCK_MODELS_DATA: Final[list[PredictiveModel]] = [
    PredictiveModel(
        id="mdl-poisson-mo-v4",
        name="Poisson Match Odds",
        description="Bivariate Poisson on attack/defence latents, NUTS-sampled with team-form decay priors.",
        status="online",
        roi_percentage=12.84,
        strike_rate=58.31,
        sharpe_ratio=2.41,
        max_drawdown=8.62,
        open_positions=42,
        updated_at="2026-09-14T13:58:12Z",
    ),
    PredictiveModel(
        id="mdl-bradley-terry-dyn",
        name="Bradley-Terry Dynamic",
        description="Time-varying pairwise strength coefficients with Kalman-smoothed posterior updates.",
        status="online",
        roi_percentage=7.62,
        strike_rate=54.07,
        sharpe_ratio=1.78,
        max_drawdown=12.41,
        open_positions=27,
        updated_at="2026-09-14T13:41:55Z",
    ),
    PredictiveModel(
        id="mdl-gaussian-spread",
        name="Gaussian Spread Engine",
        description="Hierarchical normal margin model with heteroskedastic variance by competition tier.",
        status="online",
        roi_percentage=4.35,
        strike_rate=51.84,
        sharpe_ratio=1.12,
        max_drawdown=17.93,
        open_positions=18,
        updated_at="2026-09-14T12:57:04Z",
    ),
    PredictiveModel(
        id="mdl-plackett-luce-props",
        name="Plackett-Luce Props",
        description="Rank-ordered choice model for player props; long-tail selections, low strike by design.",
        status="training",
        roi_percentage=9.07,
        strike_rate=31.46,
        sharpe_ratio=1.44,
        max_drawdown=21.38,
        open_positions=11,
        updated_at="2026-09-14T09:12:30Z",
    ),
    PredictiveModel(
        id="mdl-markov-tennis",
        name="Markov Tennis Chain",
        description="Point-level absorbing Markov chain with serve/return hierarchical shrinkage priors.",
        status="online",
        roi_percentage=-2.18,
        strike_rate=47.22,
        sharpe_ratio=-0.34,
        max_drawdown=26.75,
        open_positions=6,
        updated_at="2026-09-14T11:26:47Z",
    ),
    PredictiveModel(
        id="mdl-dirichlet-corners",
        name="Dirichlet Corner Markets",
        description="Dirichlet-multinomial set-piece allocation; suspended pending prior recalibration.",
        status="offline",
        roi_percentage=-6.41,
        strike_rate=43.91,
        sharpe_ratio=-0.82,
        max_drawdown=33.17,
        open_positions=0,
        updated_at="2026-09-11T19:04:19Z",
    ),
]

@router.get("", response_model=list[PredictiveModel])
async def get_models() -> list[PredictiveModel]:
    # Randomly jitter some values so it's "live telemetry"
    jittered = []
    for m in MOCK_MODELS_DATA:
        if m.status == "online":
            # Add some jitter to ROI and strike rate to simulate live updates
            roi = round(m.roi_percentage + random.uniform(-0.5, 0.5), 2)
            strike = round(m.strike_rate + random.uniform(-0.5, 0.5), 2)
            sharpe = round(m.sharpe_ratio + random.uniform(-0.1, 0.1), 2)
            updated = m.model_copy(update={
                "roi_percentage": roi,
                "strike_rate": strike,
                "sharpe_ratio": sharpe,
            })
            jittered.append(updated)
        else:
            jittered.append(m)
    return jittered