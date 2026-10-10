"""The model recalibration engine (Group 74): the one publisher of pillar 1's model weights.

``recalibrate`` (weekly on Celery beat, on demand, or triggered by a run of losses the root-cause classifier
blamed on the models):

1. takes the run lock (``SET NX EX``): one run at a time, across workers and the API;
2. reads every settled prediction of the window (Group 73's ``model_prediction_feedback``), the weights in
   force (``<TWIN_PREFIX>:model_weights``), the administrators' pins and each model's previous state;
3. scores, classifies and weighs every model (``app.domain.oracle.calibration``);
4. records the run and one audit per model, then publishes in one ``MULTI``: the weights hash replaced
   whole (pillar 1 sees the old weights or the new, never a mix) and the provenance beside it. A run with
   no settled predictions and no pins publishes nothing: the weights in force stay;
5. pages the Sentinel: a summary (INFO), and a drift warning for every model newly on probation or benched.

``override`` sets one model's weight by hand (optionally pinned, so later runs keep it) and ``reset``
clears every weight and pin (pillar 1 back to equal weights); both are recorded as runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.oracle import calibration as cal
from app.models.feedback import REFERENCE_PREDICTORS, ModelPredictionFeedback, RootCauseTag, SettlementRootCauseAudit
from app.models.model_calibration import ModelLifecycleStatus, ModelRecalibrationRun, ModelWeightAudit, RecalibrationTrigger
from app.models.sentinel import Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin.intel import model_weights, weight_pins_key, weights_key, weights_meta_key
from app.services.twin.vetting import developer_credit

logger = logging.getLogger("betdoc.calibration")

DRIFT = frozenset({ModelLifecycleStatus.PROBATION, ModelLifecycleStatus.BENCHED})


class RecalibrationBusy(Exception):
    """Another run holds the lock."""


def lock_key(settings: Settings) -> str:
    return f"{settings.TWIN_PREFIX}:recalibration:lock"


def loss_trigger_key(settings: Settings) -> str:
    return f"{settings.TWIN_PREFIX}:recalibration:loss_trigger"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def read_pins(redis: Redis | None, settings: Settings) -> dict[str, float]:
    if redis is None:
        return {}
    try:
        raw = await redis.hgetall(weight_pins_key(settings))
    except (RedisError, OSError):
        return {}
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            weight = float(value)
        except ValueError:
            continue
        if weight >= 0:
            out[name] = weight
    return out


async def read_meta(redis: Redis | None, settings: Settings) -> dict[str, Any] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(weights_meta_key(settings))
        return json.loads(raw) if raw else None
    except (RedisError, OSError, ValueError):
        return None


async def latest_audits(session: AsyncSession) -> dict[str, ModelWeightAudit]:
    """model -> its most recent audit (its state as the last run left it)."""
    newest = select(ModelWeightAudit.model_name, func.max(ModelWeightAudit.created_at).label("at")).group_by(ModelWeightAudit.model_name).subquery()
    rows = (await session.execute(
        select(ModelWeightAudit).join(newest, (ModelWeightAudit.model_name == newest.c.model_name) & (ModelWeightAudit.created_at == newest.c.at))
    )).scalars()
    return {r.model_name: r for r in rows}


async def observations(session: AsyncSession, settings: Settings, now: datetime) -> list[cal.Observation]:
    since = now - timedelta(days=settings.TWIN_RECALIBRATION_WINDOW_DAYS)
    rows = (await session.execute(
        select(ModelPredictionFeedback.model_name, ModelPredictionFeedback.leg_id, ModelPredictionFeedback.predicted_prob, ModelPredictionFeedback.actual_outcome,
               ModelPredictionFeedback.brier_score, ModelPredictionFeedback.clv_pct, ModelPredictionFeedback.created_at)
        .where(ModelPredictionFeedback.created_at >= since)
    )).all()
    return [cal.Observation(model, str(leg), float(p), float(y), float(b), None if clv is None else float(clv), (now - _aware(at)).total_seconds() / 86_400.0)
            for model, leg, p, y, b, clv, at in rows]


class _Lock:
    def __init__(self, redis: Redis | None, settings: Settings) -> None:
        self.redis, self.settings, self.token = redis, settings, uuid.uuid4().hex

    async def __aenter__(self) -> _Lock:
        if self.redis is None:
            return self
        try:
            taken = await self.redis.set(lock_key(self.settings), self.token, nx=True, ex=self.settings.TWIN_RECALIBRATION_LOCK_SECONDS)
        except (RedisError, OSError) as exc:
            raise RecalibrationBusy("the run lock cannot be taken: Redis is unavailable") from exc
        if not taken:
            raise RecalibrationBusy("another recalibration is running")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.redis is None:
            return
        try:
            if await self.redis.get(lock_key(self.settings)) == self.token:
                await self.redis.delete(lock_key(self.settings))
        except (RedisError, OSError):
            pass


async def _publish(redis: Redis, settings: Settings, weights: dict[str, float], meta: dict[str, Any], *, pins: dict[str, float] | None = None, clear_pins: bool = False) -> None:
    pipe = redis.pipeline(transaction=True)  # MULTI ... EXEC
    pipe.delete(weights_key(settings))
    if weights:
        pipe.hset(weights_key(settings), mapping={k: repr(float(v)) for k, v in weights.items()})
    if clear_pins:
        pipe.delete(weight_pins_key(settings))
    elif pins is not None:
        pipe.delete(weight_pins_key(settings))
        if pins:
            pipe.hset(weight_pins_key(settings), mapping={k: repr(float(v)) for k, v in pins.items()})
    pipe.set(weights_meta_key(settings), json.dumps(meta, default=str))
    await pipe.execute()


def _audit(run_id: uuid.UUID, v: cal.ModelVerdict, now: datetime) -> ModelWeightAudit:
    m = v.murphy
    return ModelWeightAudit(
        id=uuid.uuid4(), run_id=run_id, model_name=v.model, sample_count=v.samples, sample_count_30d=v.samples_short, paired_count=v.paired,
        brier_score_30d=cal._r(v.brier_short), brier_score_90d=cal._r(v.brier_long), brier_decayed=cal._r(v.brier_decayed),
        brier_skill_score=cal._r(v.bss), avg_clv_pct=cal._r(v.clv_pct, 4), reliability=None if m is None else cal._r(m.reliability),
        resolution=None if m is None else cal._r(m.resolution), uncertainty=None if m is None else cal._r(m.uncertainty),
        previous_weight=v.previous_weight, new_weight=v.weight, previous_status=None if v.previous_status is None else v.previous_status.value,
        status=v.status.value, status_reason=v.reason[:2000], metrics_snapshot=v.snapshot(), created_at=now,
    )


def _alerts(run: ModelRecalibrationRun, verdicts: Sequence[cal.ModelVerdict]) -> list[SentinelAlert]:
    states = ", ".join(f"{v.model} {v.status.value.lower().replace('_', ' ')} {v.weight:g}" for v in verdicts) or "no model scored"
    out = [SentinelAlert(
        kind=AlertKind.MODEL_RECALIBRATED, severity=Severity.INFO, source="model_calibrator",
        title=f"Model recalibration ({run.trigger_type.lower().replace('_', ' ')}): {run.models_evaluated} scored, {run.models_promoted} up, {run.models_demoted} down",
        body=f"{states}\nDeveloper: {run.developer_credit}"[:4000], dedupe_key=f"calibration:run:{run.id}",
        detail={"run_id": str(run.id), "weights": run.published_weights, "published": run.published},
    )]
    drifted = [v for v in verdicts if v.status in DRIFT and v.previous_status is not v.status]
    if drifted:
        out.append(SentinelAlert(
            kind=AlertKind.MODEL_DRIFT, severity=Severity.WARNING, source="model_calibrator",
            title=f"Model drift: {', '.join(f'{v.model} {v.status.value.lower()}' for v in drifted)}"[:200],
            body="\n".join(f"{v.model}: {v.reason}" for v in drifted)[:4000], dedupe_key=f"calibration:drift:{run.id}",
            detail={"run_id": str(run.id), "models": {v.model: {"status": v.status.value, "weight": v.weight} for v in drifted}},
        ))
    return out


async def recalibrate(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, trigger: RecalibrationTrigger, *,
                      triggered_by: uuid.UUID | None = None, note: str | None = None) -> ModelRecalibrationRun:
    policy = cal.CalibrationPolicy.from_settings(settings, REFERENCE_PREDICTORS)
    async with _Lock(redis, settings):
        previous = await model_weights(redis, settings) if redis is not None else {}
        pins = await read_pins(redis, settings)
        async with sessions() as session:
            obs = await observations(session, settings, now)
            states = {name: a.status for name, a in (await latest_audits(session)).items()}
            result = cal.evaluate(obs, policy, previous_weights=previous, previous_status=states, pins=pins)
            credit = await developer_credit(session)
            publishable = bool(result.verdicts) or bool(pins)
            run = ModelRecalibrationRun(
                id=uuid.uuid4(), trigger_type=trigger.value, triggered_by=triggered_by, models_evaluated=len(result.verdicts), models_promoted=result.promoted,
                models_demoted=result.demoted, benchmark_model=policy.benchmark, benchmark_brier=cal._r(result.benchmark_brier), published=False,
                published_weights=result.weights, parameters={**policy.as_dict(), "window_days": settings.TWIN_RECALIBRATION_WINDOW_DAYS, "references": result.references},
                note=note if publishable else (note or "no settled predictions in the window: the weights in force stay"), developer_credit=credit, created_at=now,
            )
            session.add(run)
            for v in result.verdicts:
                session.add(_audit(run.id, v, now))
            await session.flush()
            if publishable and redis is not None:
                meta = {"run_id": str(run.id), "trigger": trigger.value, "recalibrated_at": now.isoformat(), "benchmark": policy.benchmark,
                        "weights": result.weights, "status": {v.model: v.status.value for v in result.verdicts}, "pinned": sorted(pins), "developer_credit": credit}
                try:
                    await _publish(redis, settings, result.weights, meta)
                    run.published = True
                except (RedisError, OSError) as exc:
                    run.note = f"publishing failed ({type(exc).__name__}): the weights in force stay"
            await session.commit()
    for alert in _alerts(run, result.verdicts):
        await emit_alert(redis, settings, alert)
    logger.info("recalibration %s (%s): published=%s weights=%s", run.id, trigger.value, run.published, run.published_weights)
    return run


async def override(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, now: datetime, *, model: str, weight: float, reason: str,
                   pin: bool, admin_id: uuid.UUID | None) -> ModelRecalibrationRun:
    async with _Lock(redis, settings):
        weights = await model_weights(redis, settings)
        pins = await read_pins(redis, settings)
        meta = await read_meta(redis, settings) or {}
        previous = weights.get(model)
        weights[model] = float(weight)
        if pin:
            pins[model] = float(weight)
        else:
            pins.pop(model, None)
        async with sessions() as session:
            last = (await latest_audits(session)).get(model)
            credit = await developer_credit(session)
            status = ModelLifecycleStatus.BENCHED if weight == 0 else ModelLifecycleStatus(last.status) if last is not None else ModelLifecycleStatus.ACTIVE
            run = ModelRecalibrationRun(
                id=uuid.uuid4(), trigger_type=RecalibrationTrigger.MANUAL_OVERRIDE.value, triggered_by=admin_id, models_evaluated=1, models_promoted=0, models_demoted=0,
                benchmark_model=settings.TWIN_RECALIBRATION_BENCHMARK_MODEL, benchmark_brier=None, published=False, published_weights=weights,
                parameters={"model": model, "weight": weight, "pin": pin}, note=reason, developer_credit=credit, created_at=now,
            )
            session.add(run)
            session.add(ModelWeightAudit(
                id=uuid.uuid4(), run_id=run.id, model_name=model, sample_count=0 if last is None else last.sample_count, sample_count_30d=0 if last is None else last.sample_count_30d,
                paired_count=0 if last is None else last.paired_count, previous_weight=previous, new_weight=float(weight), previous_status=None if last is None else last.status,
                status=status.value, status_reason=f"set by an administrator{' and pinned' if pin else ''}: {reason}"[:2000], metrics_snapshot={"override": True, "pin": pin}, created_at=now,
            ))
            await session.flush()
            meta.update({"run_id": str(run.id), "trigger": RecalibrationTrigger.MANUAL_OVERRIDE.value, "recalibrated_at": now.isoformat(), "weights": weights,
                         "pinned": sorted(pins), "developer_credit": credit})
            meta.setdefault("status", {})[model] = status.value
            await _publish(redis, settings, weights, meta, pins=pins)
            run.published = True
            await session.commit()
    logger.warning("model weight override: %s -> %s (pin=%s) by %s: %s", model, weight, pin, admin_id, reason)
    return run


async def reset(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, now: datetime, *, reason: str, admin_id: uuid.UUID | None) -> ModelRecalibrationRun:
    async with _Lock(redis, settings):
        async with sessions() as session:
            credit = await developer_credit(session)
            run = ModelRecalibrationRun(
                id=uuid.uuid4(), trigger_type=RecalibrationTrigger.EMERGENCY_RESET.value, triggered_by=admin_id, models_evaluated=0, models_promoted=0, models_demoted=0,
                benchmark_model=settings.TWIN_RECALIBRATION_BENCHMARK_MODEL, benchmark_brier=None, published=False, published_weights={},
                parameters={}, note=reason, developer_credit=credit, created_at=now,
            )
            session.add(run)
            await session.flush()
            pipe = redis.pipeline(transaction=True)
            pipe.delete(weights_key(settings), weight_pins_key(settings), weights_meta_key(settings))
            await pipe.execute()
            run.published = True
            await session.commit()
    logger.warning("model weights reset to equal by %s: %s", admin_id, reason)
    return run


async def maybe_loss_trigger(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> ModelRecalibrationRun | None:
    """A recalibration when ``LOSS_TRIGGER_COUNT`` losses inside ``LOSS_TRIGGER_HOURS`` were blamed on the models,
    at most once per ``LOSS_COOLDOWN_HOURS``."""
    if redis is None:
        return None
    since = now - timedelta(hours=settings.TWIN_RECALIBRATION_LOSS_TRIGGER_HOURS)
    async with sessions() as session:
        blamed = await session.scalar(select(func.count()).select_from(SettlementRootCauseAudit).where(
            SettlementRootCauseAudit.root_cause_tag == RootCauseTag.MODEL_UNDERESTIMATION.value, SettlementRootCauseAudit.created_at >= since,
        ))
    if (blamed or 0) < settings.TWIN_RECALIBRATION_LOSS_TRIGGER_COUNT:
        return None
    try:
        armed = await redis.set(loss_trigger_key(settings), now.isoformat(), nx=True, ex=int(settings.TWIN_RECALIBRATION_LOSS_COOLDOWN_HOURS * 3600))
    except (RedisError, OSError):
        return None
    if not armed:
        return None
    try:
        return await recalibrate(sessions, redis, settings, now, RecalibrationTrigger.LOSS_THRESHOLD_TRIGGER,
                                 note=f"{blamed} losses blamed on the models in {settings.TWIN_RECALIBRATION_LOSS_TRIGGER_HOURS:g}h")
    except RecalibrationBusy:
        return None


def audit_view(a: ModelWeightAudit) -> dict[str, Any]:
    return {
        "model_name": a.model_name, "sample_count": a.sample_count, "sample_count_30d": a.sample_count_30d, "paired_count": a.paired_count,
        "brier_score_30d": a.brier_score_30d, "brier_score_90d": a.brier_score_90d, "brier_decayed": a.brier_decayed, "brier_skill_score": a.brier_skill_score,
        "avg_clv_pct": a.avg_clv_pct, "reliability": a.reliability, "resolution": a.resolution, "uncertainty": a.uncertainty,
        "previous_weight": a.previous_weight, "new_weight": a.new_weight, "previous_status": a.previous_status, "status": a.status, "status_reason": a.status_reason,
        "metrics": a.metrics_snapshot, "run_id": str(a.run_id), "created_at": a.created_at.isoformat(),
    }


def run_view(run: ModelRecalibrationRun, audits: Sequence[ModelWeightAudit] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run_id": str(run.id), "trigger_type": run.trigger_type, "models_evaluated": run.models_evaluated, "models_promoted": run.models_promoted,
        "models_demoted": run.models_demoted, "benchmark_model": run.benchmark_model, "benchmark_brier": run.benchmark_brier, "published": run.published,
        "weights": run.published_weights, "note": run.note, "created_at": run.created_at.isoformat(), "developer_credit": run.developer_credit,
    }
    if audits is not None:
        out["audits"] = [audit_view(a) for a in audits]
        out["parameters"] = run.parameters
    return out
