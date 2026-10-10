"""The post-execution feedback loop under ``/api/v1/twin/settlement`` (Group 73).

    POST /twin/settlement/sweep                 settle the user's pending bets and attribute what settled (CLV, model feedback, root cause)
    GET  /twin/settlement/summary               the user's closing-line value and root causes
    GET  /twin/settlement/feedback              the user's model feedback rows (one per settled leg and predictor)
    GET  /twin/settlement/model-accuracy        every predictor over FEEDBACK_WINDOW_DAYS: Brier, log loss, RPS, the weight it earns, the weight published
    GET  /twin/settlement/calibration           one predictor's reliability curve and expected calibration error
    POST /twin/settlement/recalibrate-weights   publish the inverse-Brier weights pillar 1 reads (admin)
    POST /twin/settlement/override              settle a bet by hand (admin); its attribution re-runs

Model accuracy is about the models, so it is computed over every settled prediction; bets, CLV and root
causes are each user's own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.feedback import ModelPredictionFeedback, SettlementRootCauseAudit
from app.models.user_bets_ledger import PlacedStatus, UserPlacedBet
from app.schemas.feedback import OverrideSettlementRequest
from app.services import user_pnl_tracker as tracker
from app.services.twin import feedback_tracker as feedback
from app.services.twin.vetting import developer_credit

router = APIRouter(prefix="/twin/settlement", tags=["Post-Execution Feedback Loop"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


async def _credit(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as session:
        return await developer_credit(session)


@router.post("/sweep")
async def sweep(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    report = await feedback.sweep(sessions, _redis(request), settings, datetime.now(UTC), user_id=user.id)
    return {**report.as_dict(), "developer_credit": await _credit(sessions)}


@router.get("/summary")
async def summary(user: CurrentUser, sessions: Sessions, recent: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, Any]:
    async with sessions() as session:
        settled = list((await session.execute(
            select(UserPlacedBet).where(UserPlacedBet.user_id == user.id, UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.feedback_at.is_not(None))
        )).scalars())
        tags = dict((await session.execute(
            select(UserPlacedBet.root_cause_tag, func.count(UserPlacedBet.id)).where(UserPlacedBet.user_id == user.id, UserPlacedBet.root_cause_tag.is_not(None))
            .group_by(UserPlacedBet.root_cause_tag)
        )).all())
        audits = (await session.execute(
            select(SettlementRootCauseAudit, UserPlacedBet).join(UserPlacedBet, UserPlacedBet.id == SettlementRootCauseAudit.bet_id)
            .where(UserPlacedBet.user_id == user.id).order_by(SettlementRootCauseAudit.created_at.desc()).limit(recent)
        )).all()
        credit = await developer_credit(session)
    with_clv = [b for b in settled if b.clv_pct is not None]
    sharp = [b.clv_sharp_pct for b in settled if b.clv_sharp_pct is not None]
    return {
        "attributed_bets": len(settled),
        "clv": {
            "bets": len(with_clv),
            "avg_clv_pct": None if not with_clv else round(sum(b.clv_pct for b in with_clv) / len(with_clv), 3),  # type: ignore[misc]
            "beat_close_rate": None if not with_clv else round(sum(1 for b in with_clv if b.clv_pct > 0) / len(with_clv), 4),  # type: ignore[operator]
            "avg_clv_sharp_pct": None if not sharp else round(sum(sharp) / len(sharp), 3),  # type: ignore[arg-type]
        },
        "root_causes": {str(k): int(v) for k, v in tags.items()},
        "recent": [
            {"bet_id": str(bet.id), "status": bet.status, "pnl_inr": None if bet.pnl_inr is None else str(bet.pnl_inr), "booking_code": bet.booking_code,
             "root_cause_tag": audit.root_cause_tag, "explanation": audit.explanation, "model_error_delta": audit.model_error_delta, "evidence": audit.evidence,
             "clv_pct": bet.clv_pct, "settled_at": None if bet.settled_at is None else bet.settled_at.isoformat()}
            for audit, bet in audits
        ],
        "developer_credit": credit,
    }


@router.get("/feedback")
async def feedback_rows(user: CurrentUser, sessions: Sessions, model_name: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    async with sessions() as session:
        query = (
            select(ModelPredictionFeedback).join(UserPlacedBet, UserPlacedBet.id == ModelPredictionFeedback.bet_id)
            .where(UserPlacedBet.user_id == user.id).order_by(ModelPredictionFeedback.created_at.desc(), ModelPredictionFeedback.model_name).limit(limit)
        )
        if model_name:
            query = query.where(ModelPredictionFeedback.model_name == model_name)
        rows = list((await session.execute(query)).scalars())
    return [
        {"id": str(r.id), "bet_id": str(r.bet_id), "leg_id": str(r.leg_id), "fixture_id": r.fixture_id, "sport_key": r.sport_key, "market": r.market,
         "selection": r.selection, "model_name": r.model_name, "predicted_prob": round(r.predicted_prob, 6), "actual_outcome": r.actual_outcome,
         "brier_score": round(r.brier_score, 6), "log_loss": round(r.log_loss, 6), "rps": None if r.rps is None else round(r.rps, 6),
         "closing_odds": None if r.closing_odds is None else str(r.closing_odds), "clv_pct": r.clv_pct, "details": r.details, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/model-accuracy")
async def model_accuracy(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    now = datetime.now(UTC)
    async with sessions() as session:
        stats = await feedback.model_stats(session, settings, now)
        credit = await developer_credit(session)
    recommended = feedback.recommended_weights(stats, settings)
    published, meta = await feedback.published_weights(_redis(request), settings)
    return {
        "window_days": settings.FEEDBACK_WINDOW_DAYS, "min_samples": settings.FEEDBACK_MIN_SAMPLES, "developer_credit": credit,
        "published_at": None if meta is None else meta.get("computed_at"),
        "models": [
            {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in s.items()},
             "eligible": not s["reference"] and s["predictions"] >= settings.FEEDBACK_MIN_SAMPLES,
             "recommended_weight": None if s["model_name"] not in recommended else round(recommended[s["model_name"]], 6),
             "published_weight": published.get(s["model_name"])}
            for s in stats
        ],
    }


@router.get("/calibration")
async def calibration(user: CurrentUser, sessions: Sessions, settings: AppSettings, model_name: Annotated[str, Query(min_length=1, max_length=32)]) -> dict[str, Any]:  # noqa: ARG001
    since = datetime.now(UTC) - timedelta(days=settings.FEEDBACK_WINDOW_DAYS)
    async with sessions() as session:
        pairs = (await session.execute(
            select(ModelPredictionFeedback.predicted_prob, ModelPredictionFeedback.actual_outcome)
            .where(ModelPredictionFeedback.model_name == model_name, ModelPredictionFeedback.created_at >= since)
        )).all()
    return {"model_name": model_name, "predictions": len(pairs), **feedback.calibration_report(((p, y) for p, y in pairs), settings)}


@router.post("/recalibrate-weights")
async def recalibrate(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The weights live in Redis, which is unavailable"})
    result = await feedback.recalibrate(sessions, redis, settings, datetime.now(UTC))
    return {**result, "developer_credit": await _credit(sessions)}


@router.post("/override")
async def override(body: OverrideSettlementRequest, request: Request, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        bet = await session.get(UserPlacedBet, body.bet_id, with_for_update=True)
        if bet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such bet")
        try:
            await feedback.override(session, bet, body.status, body.return_inr, body.notes, datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "INVALID_OVERRIDE", "message": str(exc)}) from exc
        await session.commit()
        credit = await developer_credit(session)
    await tracker.bump(_redis(request), [bet.user_id])
    return {"bet_id": str(bet.id), "status": bet.status, "return_inr": str(bet.return_inr), "pnl_inr": str(bet.pnl_inr), "settlement_source": bet.settlement_source,
            "feedback_due": bet.feedback_at is None, "developer_credit": credit}
