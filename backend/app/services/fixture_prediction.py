"""One fixture's prediction from what the live board and the Wire hold (Groups 108-109). Developed for Amit Ashok Kumar Patnaik.

The de-vigged Match Odds consensus on the live board (``the_wire.news.consensus``), through the lineup model's rated
absences (``the_wire.lineups``); for three-way football, the Dixon-Coles scorelines fitted to it
(``score_predictor.football_from_market``, the exact matrix) and, when rated absences move it, to the adjusted
probabilities too. Other sports' simulators need their teams' stats, which no feed carries: their prediction is the
probabilities alone, and it says so. Shared by the neural chain's Score Predictor step and the Pocket Twin's /score.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.oracle.match_model import MarketTargets
from app.services.the_wire import lineups, news
from app.services.the_wire.fixtures import Fixture

BASIS = "the de-vigged Match Odds consensus on the live board, through the lineup model's rated absences"
EXACT = "Dixon-Coles scorelines fitted to the probabilities (exact matrix)"
PROBABILITIES_ONLY = "probabilities only: this sport's simulator needs its stats (run it in Score Prediction)"


def _fit(probabilities: dict[str, float], settings: Settings) -> dict[str, Any]:
    from app.domain.prediction import score_predictor as sp  # noqa: PLC0415 - numerical engine, loaded on use

    p = sp.football_from_market(MarketTargets(probabilities["HOME"], probabilities["DRAW"], probabilities["AWAY"]),
                                heatmap_goals=settings.PREDICTION_HEATMAP_GOALS, top=settings.PREDICTION_TOP_SCORELINES)
    return {"expected_goals": [round(x, 4) for x in p.expected_goals],
            "top_scorelines": [{"score": s.label, "probability": round(s.probability, 4)} for s in p.top_scorelines],
            "over_2_5": round(float(p.over.get(2.5, 0.0)), 4) if 2.5 in p.over else None, "btts_yes": round(p.btts_yes, 4)}


async def predict(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, fixture: Fixture, now: datetime) -> dict[str, Any]:
    """``consensus`` is None when the live board has no Match Odds consensus; ``market_fit`` only for three-way football."""
    probs = (await news.consensus(redis, settings, [fixture.fixture_id], now)).get(fixture.fixture_id)
    async with sessions() as session:
        rows = await lineups.rows_for(session, fixture.fixture_id)
    view = lineups.view(rows, fixture, settings, probs)
    moved = view["adjusted"] is not None and any(abs(v) > 0 for v in view["delta"].values())
    out: dict[str, Any] = {"consensus": probs, "absences": len(view["absences"]), "unrated": view["unrated"],
                           "delta_pts": {k: round(100.0 * v, 2) for k, v in view["delta"].items()}, "adjusted": view["adjusted"],
                           "moved": moved, "basis": BASIS, "market_fit": None, "lineup_fit": None, "model": None}
    if not probs:
        return out
    if fixture.family == "soccer" and all(k in probs for k in ("HOME", "DRAW", "AWAY")):
        out["market_fit"] = await asyncio.to_thread(_fit, probs, settings)
        if moved:
            out["lineup_fit"] = await asyncio.to_thread(_fit, view["adjusted"], settings)
        out["model"] = EXACT
    else:
        out["model"] = PROBABILITIES_ONLY
    return out
