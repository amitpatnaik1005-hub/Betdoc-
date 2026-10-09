"""PRATAP's CoreOrchestrator: the Engine Room control plane.

Every status transition is an explicit compare-and-swap
(UPDATE ... WHERE status = :expected) and every counter is incremented with a
server-side SQL expression. Background workers open their own sessions.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4

import psutil
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.math.models import DixonColesModel, EloProbabilityModel, MonteCarloModel, PoissonModel
from app.domain.the_core.errors import (
    CoreDomainError,
    EngineConcurrencyError,
    InvalidSmallcaseStateError,
    SmallcaseNotFoundError,
)
from app.models.the_core import (
    ComponentKind,
    BacktestJobModel,
    CoreEngineMetricsModel,
    EngineTaskStatus,
    SmallcaseRegistryModel,
    SmallcaseStatus,
    TestBenchRunModel,
)
from app.schemas.math import MatchContext, PredictionResult
from app.schemas.the_core import EngineTelemetryRead, PipelineStageRead, SmallcaseDetailRead, SmallcaseRead

logger = logging.getLogger(__name__)

OUTCOMES: tuple[str, ...] = ("home", "draw", "away")
DEFAULT_ODDS: dict[str, float] = {"home": 2.10, "draw": 3.40, "away": 3.60}
MAX_KELLY_FRACTION = 0.25
MAX_BACKTEST_STAKE_FRACTION = 0.05
FLAT_STAKE_FRACTION = 0.02
BACKTEST_KELLY_MULTIPLIER = 0.5
STARTING_BANKROLL = 1_000.0
MATCHES_PER_DAY = 14
MIN_BACKTEST_MATCHES = 250
MAX_BACKTEST_MATCHES = 25_000
BACKTEST_YIELD_EVERY = 250
BACKTEST_PROGRESS_EVERY = 1_000
CROSS_VAL_FOLDS = 5
MAX_EXPECTED_GOALS = 15.0
INSANE_ODDS_THRESHOLD = 1_000.0
STATUS_SAMPLE_TTL = timedelta(seconds=5)
NATIVE_STEP_TIMEOUT_S = 10.0
MAX_ERROR_DETAIL_CHARS = 8_000
NATIVE_METHOD_CANDIDATES: tuple[str, ...] = ("predict", "calculate", "compute", "run", "evaluate")

DEFAULT_MODEL_REGISTRY: dict[str, type[Any]] = {
    "PoissonModel": PoissonModel,
    "DixonColesModel": DixonColesModel,
    "EloProbabilityModel": EloProbabilityModel,
    "MonteCarloModel": MonteCarloModel,
}

BOOTSTRAP_SMALLCASES: tuple[dict[str, Any], ...] = (
    {
        "name": "PRATAP Poisson-Kelly Classic",
        "description": "Poisson goal model feeding a capped Kelly stake optimiser.",
        "pipeline_config": ["PoissonModel", "KellyStake"],
        "status": SmallcaseStatus.ACTIVE,
    },
    {
        "name": "PANINI Dixon-Coles Value Hunter",
        "description": "Dixon-Coles low-score correction, value-edge filter, then Kelly staking.",
        "pipeline_config": ["DixonColesModel", "ValueEdgeFilter", "KellyStake"],
        "status": SmallcaseStatus.ACTIVE,
    },
    {
        "name": "Elo Monte Carlo Fusion",
        "description": "Elo ratings and Monte Carlo simulation blended by consensus before staking.",
        "pipeline_config": ["EloProbabilityModel", "MonteCarloModel", "ConsensusBlender", "KellyStake"],
        "status": SmallcaseStatus.STANDBY,
    },
)


class LiveBroadcaster(Protocol):
    async def broadcast(self, message: dict[str, Any]) -> None: ...


# --------------------------------------------------------------------------- helpers


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _uniform() -> dict[str, float]:
    return {outcome: 1.0 / len(OUTCOMES) for outcome in OUTCOMES}


def _normalize(raw: Mapping[str, Any]) -> dict[str, float] | None:
    cleaned = {o: max(_safe_float(raw.get(o), 0.0) or 0.0, 0.0) for o in OUTCOMES}
    total = sum(cleaned.values())
    if total <= 0.0 or not math.isfinite(total):
        return None
    return {o: value / total for o, value in cleaned.items()}


def _round_probs(probs: Mapping[str, float]) -> dict[str, float]:
    return {o: round(float(probs[o]), 6) for o in OUTCOMES}


def _implied_probabilities(odds: Mapping[str, float]) -> dict[str, float] | None:
    return _normalize({o: (1.0 / odds[o]) if odds.get(o, 0.0) > 0.0 else 0.0 for o in OUTCOMES})


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _poisson_outcome_probabilities(
    home_lambda: float, away_lambda: float, max_goals: int = 10
) -> dict[str, float] | None:
    lh = min(max(home_lambda, 0.0), MAX_EXPECTED_GOALS)
    la = min(max(away_lambda, 0.0), MAX_EXPECTED_GOALS)
    home_pmf = [_poisson_pmf(k, lh) for k in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(k, la) for k in range(max_goals + 1)]
    home = draw = away = 0.0
    for i, p_home in enumerate(home_pmf):
        for j, p_away in enumerate(away_pmf):
            joint = p_home * p_away
            if i > j:
                home += joint
            elif i == j:
                draw += joint
            else:
                away += joint
    return _normalize({"home": home, "draw": draw, "away": away})


_PROBABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "home": ("home_win_prob", "home_win_probability", "home_win", "home_prob", "home_probability", "home", "p_home"),
    "draw": ("draw_prob", "draw_probability", "draw", "p_draw"),
    "away": ("away_win_prob", "away_win_probability", "away_win", "away_prob", "away_probability", "away", "p_away"),
}


def _extract_probabilities(payload: Any, _depth: int = 0) -> dict[str, float] | None:
    if not isinstance(payload, Mapping) or _depth > 4:
        return None
    for nested_key in ("probabilities", "probs", "outcome_probabilities"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            found = _extract_probabilities(nested, _depth + 1)
            if found is not None:
                return found
    raw: dict[str, float] = {}
    for outcome, keys in _PROBABILITY_ALIASES.items():
        for key in keys:
            if key in payload and _is_number(payload[key]):
                raw[outcome] = float(payload[key])
                break
    if len(raw) != len(OUTCOMES):
        return None
    return _normalize(raw)


def _extract_expected_goals(ctx: Mapping[str, Any]) -> tuple[float, float] | None:
    def first(source: Mapping[str, Any], keys: Sequence[str]) -> float | None:
        for key in keys:
            if key in source:
                value = _safe_float(source[key], None)
                if value is not None and value >= 0.0:
                    return value
        return None

    nested = ctx.get("expected_goals")
    if isinstance(nested, Mapping):
        home, away = first(nested, ("home",)), first(nested, ("away",))
        if home is not None and away is not None:
            return home, away
    home = first(ctx, ("home_expected_goals", "home_xg", "expected_home_goals", "lambda_home", "home_lambda"))
    away = first(ctx, ("away_expected_goals", "away_xg", "expected_away_goals", "lambda_away", "away_lambda"))
    if home is None or away is None:
        return None
    return home, away


def _extract_odds(ctx: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    nested = ctx.get("odds")
    odds: dict[str, float] = {}
    anomalies: list[str] = []
    for outcome in OUTCOMES:
        raw: Any = None
        found = False
        if isinstance(nested, Mapping):
            for key in (outcome, f"{outcome}_odds", f"{outcome}_win_odds"):
                if key in nested:
                    raw, found = nested[key], True
                    break
        if not found:
            for key in (f"{outcome}_odds", f"odds_{outcome}", f"{outcome}_win_odds"):
                if key in ctx:
                    raw, found = ctx[key], True
                    break
        if not found:
            odds[outcome] = DEFAULT_ODDS[outcome]
            continue
        value = _safe_float(raw, None)
        if value is None:
            anomalies.append(f"odds.{outcome}: non-numeric or non-finite value {raw!r}; defaulted")
            odds[outcome] = DEFAULT_ODDS[outcome]
            continue
        if value <= 1.0:
            anomalies.append(f"odds.{outcome}: {value} offers no positive return")
        elif value >= INSANE_ODDS_THRESHOLD:
            anomalies.append(f"odds.{outcome}: {value} is an insane price")
        odds[outcome] = value
    return odds, anomalies


def _kelly_fraction(probability: float, decimal_odds: float) -> float:
    b = decimal_odds - 1.0
    if b <= 0.0 or probability <= 0.0 or not math.isfinite(b):
        return 0.0
    fraction = (b * probability - (1.0 - probability)) / b
    if not math.isfinite(fraction):
        return 0.0
    return min(max(fraction, 0.0), MAX_KELLY_FRACTION)


def _classify_stage(model_name: str) -> str:
    lowered = model_name.lower()
    if "kelly" in lowered or "stake" in lowered:
        return "staking"
    if any(token in lowered for token in ("filter", "edge", "value")):
        return "value_filter"
    if any(token in lowered for token in ("blend", "consensus", "ensemble")):
        return "blender"
    return "probability"


_PROCESS = psutil.Process()
_PROCESS.cpu_percent(interval=None)  # prime: the first reading is always 0.0
_CPU_COUNT = psutil.cpu_count() or 1

def _perturb(base: Mapping[str, float], rng: random.Random, scale: float) -> dict[str, float]:
    noisy = _normalize({o: base[o] * max(0.0, 1.0 + rng.gauss(0.0, scale)) for o in OUTCOMES})
    return noisy if noisy is not None else dict(base)


def _weighted_choice(probs: Mapping[str, float], rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for outcome in OUTCOMES:
        cumulative += probs[outcome]
        if roll <= cumulative:
            return outcome
    return OUTCOMES[-1]


def _format_exception(exc: BaseException) -> str:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return rendered[-MAX_ERROR_DETAIL_CHARS:]


def _to_jsonable(value: Any, _depth: int = 0) -> Any:
    if _depth > 32:
        return repr(value)
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _to_jsonable(value.value, _depth + 1)
    if isinstance(value, BaseModel):
        return _to_jsonable(value.model_dump(mode="json"), _depth + 1)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(item, _depth + 1) for item in value]
    for converter in ("tolist", "item"):
        method = getattr(value, converter, None)
        if callable(method):
            try:
                return _to_jsonable(method(), _depth + 1)
            except Exception:  # noqa: BLE001 - best-effort conversion only
                break
    return repr(value)


@dataclass(slots=True)
class _PipelineState:
    odds: dict[str, float]
    anomalies: list[str] = field(default_factory=list)
    current_probs: dict[str, float] | None = None
    history: list[dict[str, float]] = field(default_factory=list)
    edges: dict[str, float] | None = None
    value_outcomes: list[str] | None = None
    kelly: dict[str, Any] | None = None

    def best_probabilities(self) -> dict[str, float]:
        return self.current_probs or _implied_probabilities(self.odds) or _uniform()


# --------------------------------------------------------------------------- orchestrator


class CoreOrchestrator:
    """PRATAP, Master of the Core, assisted by PANINI (the Math Engine)."""

    def __init__(
        self,
        *,
        model_registry: Mapping[str, type[Any]] | None = None,
        context_schema: type[BaseModel] = MatchContext,
        native_step_timeout_s: float = NATIVE_STEP_TIMEOUT_S,
    ) -> None:
        self._model_registry: dict[str, type[Any]] = dict(
            DEFAULT_MODEL_REGISTRY if model_registry is None else model_registry
        )
        self._context_schema = context_schema
        self._native_step_timeout_s = native_step_timeout_s

    @property
    def model_registry(self) -> dict[str, type[Any]]:
        return dict(self._model_registry)

    def stage_execution_mode(self, model_name: str) -> str:
        return "NATIVE" if model_name in self._model_registry else "SIMULATED"

    # ------------------------------------------------------------------ telemetry

    async def record_engine_metrics(self, db: AsyncSession) -> CoreEngineMetricsModel:
        in_flight = (EngineTaskStatus.QUEUED, EngineTaskStatus.RUNNING)
        queued_runs = (
            await db.execute(
                select(func.count()).select_from(TestBenchRunModel).where(TestBenchRunModel.status.in_(in_flight))
            )
        ).scalar_one()
        queued_jobs = (
            await db.execute(
                select(func.count()).select_from(BacktestJobModel).where(BacktestJobModel.status.in_(in_flight))
            )
        ).scalar_one()
        queue_depth = int(queued_runs) + int(queued_jobs)

        pipelines = (
            await db.execute(
                select(SmallcaseRegistryModel.pipeline_config).where(
                    SmallcaseRegistryModel.status == SmallcaseStatus.ACTIVE, SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE
                )
            )
        ).scalars().all()
        active_models_count = sum(len(p) for p in pipelines if isinstance(p, list))

        # Measured, not modelled: this API process's CPU share (normalised across cores) and resident memory.
        cpu = round(min(max(_PROCESS.cpu_percent(interval=None) / _CPU_COUNT, 0.0), 100.0), 2)
        memory = int(_PROCESS.memory_info().rss // (1024 * 1024))

        metric_id = uuid4()
        await db.execute(
            insert(CoreEngineMetricsModel).values(
                id=metric_id,
                cpu_usage_pct=cpu,
                memory_usage_mb=memory,
                queue_depth=queue_depth,
                active_models_count=active_models_count,
                recorded_at=_utcnow(),
            )
        )
        await db.commit()
        metric = await db.get(CoreEngineMetricsModel, metric_id, populate_existing=True)
        if metric is None:
            raise CoreDomainError("Telemetry sample vanished immediately after insert.")
        return metric

    @staticmethod
    def _derive_engine_state(cpu_usage_pct: float, queue_depth: int) -> str:
        if cpu_usage_pct >= 90.0 or queue_depth >= 50:
            return "SATURATED"
        if cpu_usage_pct >= 75.0 or queue_depth >= 20:
            return "DEGRADED"
        return "ONLINE"

    async def get_engine_status(self, db: AsyncSession) -> EngineTelemetryRead:
        latest = (
            await db.execute(
                select(CoreEngineMetricsModel).order_by(CoreEngineMetricsModel.recorded_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if latest is None or _utcnow() - _as_aware(latest.recorded_at) > STATUS_SAMPLE_TTL:
            latest = await self.record_engine_metrics(db)

        total_smallcases = (
            await db.execute(select(func.count()).select_from(SmallcaseRegistryModel).where(SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE))
        ).scalar_one()
        active_smallcases = (
            await db.execute(
                select(func.count())
                .select_from(SmallcaseRegistryModel)
                .where(SmallcaseRegistryModel.status == SmallcaseStatus.ACTIVE, SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE)
            )
        ).scalar_one()

        return EngineTelemetryRead(
            id=latest.id,
            cpu_usage_pct=latest.cpu_usage_pct,
            memory_usage_mb=latest.memory_usage_mb,
            queue_depth=latest.queue_depth,
            active_models_count=latest.active_models_count,
            recorded_at=_as_aware(latest.recorded_at),
            engine_state=self._derive_engine_state(latest.cpu_usage_pct, latest.queue_depth),
            master_bot="PRATAP",
            math_engine="PANINI",
            total_smallcases=int(total_smallcases),
            active_smallcases=int(active_smallcases),
        )

    # ------------------------------------------------------------------ registry

    async def list_smallcases(
        self, db: AsyncSession, status: SmallcaseStatus | None = None
    ) -> list[SmallcaseRegistryModel]:
        # Model-registry components (Group 65) share the table; The Core only ever sees its pipelines
        statement = select(SmallcaseRegistryModel).where(SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE).order_by(SmallcaseRegistryModel.name)
        if status is not None:
            statement = statement.where(SmallcaseRegistryModel.status == SmallcaseStatus(status))
        result = await db.execute(statement.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def _get_smallcase_or_raise(self, db: AsyncSession, smallcase_id: UUID) -> SmallcaseRegistryModel:
        result = await db.execute(
            select(SmallcaseRegistryModel)
            .where(SmallcaseRegistryModel.id == smallcase_id, SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE)
            .execution_options(populate_existing=True)
        )
        smallcase = result.scalar_one_or_none()
        if smallcase is None:
            raise SmallcaseNotFoundError(smallcase_id)
        return smallcase

    async def get_smallcase_details(self, db: AsyncSession, smallcase_id: UUID) -> SmallcaseDetailRead:
        smallcase = await self._get_smallcase_or_raise(db, smallcase_id)
        pipeline = [str(name) for name in (smallcase.pipeline_config or [])]
        base = SmallcaseRead.model_validate(smallcase).model_dump()
        return SmallcaseDetailRead(
            **base,
            pipeline_config=pipeline,
            pipeline_stages=[
                PipelineStageRead(
                    index=index,
                    model_name=name,
                    stage_kind=_classify_stage(name),
                    execution_mode=self.stage_execution_mode(name),
                )
                for index, name in enumerate(pipeline)
            ],
        )

    async def bootstrap_smallcases(self, db: AsyncSession) -> list[SmallcaseRegistryModel]:
        names = [spec["name"] for spec in BOOTSTRAP_SMALLCASES]
        existing = set(
            (
                await db.execute(select(SmallcaseRegistryModel.name).where(SmallcaseRegistryModel.name.in_(names)))
            ).scalars().all()
        )
        now = _utcnow()
        rows = [
            {
                "id": uuid4(),
                "name": spec["name"],
                "description": spec["description"],
                "pipeline_config": list(spec["pipeline_config"]),
                "status": spec["status"],
                "current_accuracy": 0.0,
                "cross_val_score": 0.0,
                "total_backtests_run": 0,
                "last_tested_at": None,
                "created_at": now,
                "updated_at": now,
            }
            for spec in BOOTSTRAP_SMALLCASES
            if spec["name"] not in existing
        ]
        if rows:
            try:
                await db.execute(insert(SmallcaseRegistryModel), rows)
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.info("Concurrent bootstrap detected; another worker seeded the registry first.")
        result = await db.execute(
            select(SmallcaseRegistryModel)
            .where(SmallcaseRegistryModel.name.in_(names))
            .order_by(SmallcaseRegistryModel.name)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def toggle_smallcase(
        self,
        db: AsyncSession,
        smallcase_id: UUID,
        *,
        expected_status: SmallcaseStatus,
        target_status: SmallcaseStatus,
    ) -> SmallcaseRegistryModel:
        expected = SmallcaseStatus(expected_status)
        target = SmallcaseStatus(target_status)
        if expected == target:
            raise InvalidSmallcaseStateError(
                f"Toggle is a no-op: expected and target are both {expected}.",
                context={"smallcase_id": smallcase_id},
            )
        result = await db.execute(
            update(SmallcaseRegistryModel)
            .where(SmallcaseRegistryModel.id == smallcase_id, SmallcaseRegistryModel.status == expected, SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE)
            .values(status=target, updated_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            current = (
                await db.execute(
                    select(SmallcaseRegistryModel.status).where(SmallcaseRegistryModel.id == smallcase_id)
                )
            ).scalar_one_or_none()
            if current is None:
                raise SmallcaseNotFoundError(smallcase_id)
            raise EngineConcurrencyError(
                f"CAS rejected: Smallcase {smallcase_id} is {current}, expected {expected}.",
                context={"smallcase_id": smallcase_id, "current_status": str(current), "expected_status": str(expected)},
            )
        await db.commit()
        return await self._get_smallcase_or_raise(db, smallcase_id)

    # ------------------------------------------------------------------ job creation

    async def create_test_bench_runs(
        self,
        db: AsyncSession,
        smallcase_ids: Sequence[UUID],
        match_context: Mapping[str, Any],
        *,
        is_stress_test: bool = False,
    ) -> list[TestBenchRunModel]:
        unique_ids = list(dict.fromkeys(smallcase_ids))
        if not unique_ids:
            raise CoreDomainError("At least one smallcase_id is required.")
        rows = (
            await db.execute(
                select(
                    SmallcaseRegistryModel.id,
                    SmallcaseRegistryModel.status,
                    SmallcaseRegistryModel.pipeline_config,
                ).where(SmallcaseRegistryModel.id.in_(unique_ids))
            )
        ).all()
        found = {row.id: row for row in rows}
        for smallcase_id in unique_ids:
            row = found.get(smallcase_id)
            if row is None:
                raise SmallcaseNotFoundError(smallcase_id)
            if row.status == SmallcaseStatus.DISABLED:
                raise InvalidSmallcaseStateError(
                    f"Smallcase {smallcase_id} is DISABLED and cannot enter the test bench.",
                    context={"smallcase_id": smallcase_id},
                )
            if not isinstance(row.pipeline_config, list) or not row.pipeline_config:
                raise InvalidSmallcaseStateError(
                    f"Smallcase {smallcase_id} has an empty pipeline.", context={"smallcase_id": smallcase_id}
                )

        payload = _to_jsonable(dict(match_context))
        now = _utcnow()
        run_ids = [uuid4() for _ in unique_ids]
        await db.execute(
            insert(TestBenchRunModel),
            [
                {
                    "id": run_id,
                    "smallcase_id": smallcase_id,
                    "match_context": payload,
                    "pipeline_execution_steps": [],
                    "predicted_outcome": None,
                    "status": EngineTaskStatus.QUEUED,
                    "is_stress_test": is_stress_test,
                    "execution_time_ms": None,
                    "error_detail": None,
                    "created_at": now,
                    "completed_at": None,
                }
                for run_id, smallcase_id in zip(run_ids, unique_ids, strict=True)
            ],
        )
        await db.commit()
        result = await db.execute(
            select(TestBenchRunModel)
            .where(TestBenchRunModel.id.in_(run_ids))
            .execution_options(populate_existing=True)
        )
        by_id = {run.id: run for run in result.scalars().all()}
        return [by_id[run_id] for run_id in run_ids]

    async def create_test_bench_run(
        self,
        db: AsyncSession,
        smallcase_id: UUID,
        match_context: Mapping[str, Any],
        *,
        is_stress_test: bool = False,
    ) -> TestBenchRunModel:
        runs = await self.create_test_bench_runs(db, [smallcase_id], match_context, is_stress_test=is_stress_test)
        return runs[0]

    async def create_backtest_job(
        self, db: AsyncSession, smallcase_id: UUID, start_date: date, end_date: date
    ) -> BacktestJobModel:
        if end_date < start_date:
            raise CoreDomainError("end_date must be on or after start_date.")
        smallcase = await self._get_smallcase_or_raise(db, smallcase_id)
        if smallcase.status == SmallcaseStatus.DISABLED:
            raise InvalidSmallcaseStateError(
                f"Smallcase {smallcase_id} is DISABLED and cannot be backtested.",
                context={"smallcase_id": smallcase_id},
            )
        job_id = uuid4()
        await db.execute(
            insert(BacktestJobModel).values(
                id=job_id,
                smallcase_id=smallcase_id,
                start_date=start_date,
                end_date=end_date,
                total_matches_simulated=0,
                status=EngineTaskStatus.QUEUED,
                created_at=_utcnow(),
            )
        )
        await db.commit()
        job = await db.get(BacktestJobModel, job_id, populate_existing=True)
        if job is None:
            raise CoreDomainError("Backtest job vanished immediately after insert.")
        return job

    # ------------------------------------------------------------------ worker plumbing

    async def _emit(self, ws_manager: LiveBroadcaster | None, event: str, payload: Mapping[str, Any]) -> None:
        if ws_manager is None:
            return
        envelope = {
            "event": event,
            "source": "PRATAP",
            "timestamp": _utcnow().isoformat(),
            "payload": _to_jsonable(payload),
        }
        try:
            await ws_manager.broadcast(envelope)
        except Exception:  # noqa: BLE001 - live stream must never break a worker
            logger.warning("Engine Room broadcast failed for event %s", event, exc_info=True)

    @staticmethod
    async def _compare_and_swap_status(
        session: AsyncSession,
        model: type[TestBenchRunModel] | type[BacktestJobModel],
        task_id: UUID,
        *,
        expected: EngineTaskStatus,
        target: EngineTaskStatus,
        **values: Any,
    ) -> bool:
        result = await session.execute(
            update(model)
            .where(model.id == task_id, model.status == expected)
            .values(status=target, **values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def _mark_task_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model: type[TestBenchRunModel] | type[BacktestJobModel],
        task_id: UUID,
        expected_status: EngineTaskStatus,
        detail: str,
    ) -> bool:
        try:
            async with session_factory() as session:
                swapped = await self._compare_and_swap_status(
                    session,
                    model,
                    task_id,
                    expected=expected_status,
                    target=EngineTaskStatus.FAILED,
                    error_detail=detail,
                    completed_at=_utcnow(),
                )
                await session.commit()
                return swapped
        except Exception:  # noqa: BLE001 - failure path must not raise
            logger.exception("Could not persist FAILED status for %s %s", model.__name__, task_id)
            return False

    async def _handle_worker_failure(
        self,
        *,
        exc: BaseException,
        model: type[TestBenchRunModel] | type[BacktestJobModel],
        task_id: UUID,
        current_status: EngineTaskStatus,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: LiveBroadcaster | None,
        event: str,
        id_field: str,
        smallcase_id: UUID | None,
    ) -> bool:
        """Zombie shield. Returns True when the caller must re-raise."""
        must_reraise = isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception)
        if current_status == EngineTaskStatus.COMPLETED:
            logger.warning("%s %s raised after completion: %r", model.__name__, task_id, exc)
            return must_reraise
        detail = _format_exception(exc)
        persisted = await asyncio.shield(
            self._mark_task_failed(session_factory, model, task_id, current_status, detail)
        )
        if must_reraise:
            logger.warning("%s %s interrupted (%s); marked FAILED=%s", model.__name__, task_id, type(exc).__name__, persisted)
        else:
            logger.error(
                "%s %s failed; marked FAILED=%s",
                model.__name__,
                task_id,
                persisted,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        await self._emit(
            ws_manager,
            event,
            {
                id_field: task_id,
                "smallcase_id": smallcase_id,
                "status": EngineTaskStatus.FAILED,
                "error": f"{type(exc).__name__}: {exc}",
                "persisted": persisted,
            },
        )
        return must_reraise

    # ------------------------------------------------------------------ test bench

    async def execute_test_bench(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: UUID,
        ws_manager: LiveBroadcaster | None,
        is_stress_test: bool = False,
    ) -> None:
        current_status = EngineTaskStatus.QUEUED
        smallcase_id: UUID | None = None
        try:
            async with session_factory() as session:
                claimed = await self._compare_and_swap_status(
                    session,
                    TestBenchRunModel,
                    run_id,
                    expected=EngineTaskStatus.QUEUED,
                    target=EngineTaskStatus.RUNNING,
                )
                if not claimed:
                    await session.rollback()
                    logger.warning("Test bench run %s was not QUEUED; skipping duplicate dispatch.", run_id)
                    return
                await session.commit()
                current_status = EngineTaskStatus.RUNNING

                run = await session.get(TestBenchRunModel, run_id, populate_existing=True)
                if run is None:
                    raise CoreDomainError(f"Test bench run {run_id} disappeared after claim.")
                smallcase = await session.get(SmallcaseRegistryModel, run.smallcase_id, populate_existing=True)
                if smallcase is None:
                    raise SmallcaseNotFoundError(run.smallcase_id)
                smallcase_id = smallcase.id
                pipeline = [str(name) for name in (smallcase.pipeline_config or [])]
                match_context = dict(run.match_context or {})
                stress_mode = bool(is_stress_test or run.is_stress_test)

            await self._emit(
                ws_manager,
                "test_bench.state",
                {
                    "run_id": run_id,
                    "smallcase_id": smallcase_id,
                    "status": EngineTaskStatus.RUNNING,
                    "is_stress_test": stress_mode,
                    "total_steps": len(pipeline),
                },
            )

            tick = time.perf_counter()
            steps, outcome = await self._run_pipeline(
                run_id=run_id,
                smallcase_id=smallcase_id,
                pipeline=pipeline,
                match_context=match_context,
                ws_manager=ws_manager,
                is_stress_test=stress_mode,
            )
            elapsed_ms = round((time.perf_counter() - tick) * 1000.0, 3)
            completed_at = _utcnow()

            async with session_factory() as session:
                finalized = await self._compare_and_swap_status(
                    session,
                    TestBenchRunModel,
                    run_id,
                    expected=EngineTaskStatus.RUNNING,
                    target=EngineTaskStatus.COMPLETED,
                    pipeline_execution_steps=steps,
                    predicted_outcome=outcome,
                    execution_time_ms=elapsed_ms,
                    error_detail=None,
                    completed_at=completed_at,
                )
                if not finalized:
                    await session.rollback()
                    raise EngineConcurrencyError(f"Test bench run {run_id} left RUNNING before finalisation.")
                await session.execute(
                    update(SmallcaseRegistryModel)
                    .where(SmallcaseRegistryModel.id == smallcase_id)
                    .values(last_tested_at=completed_at, updated_at=completed_at)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            current_status = EngineTaskStatus.COMPLETED

            await self._emit(
                ws_manager,
                "test_bench.state",
                {
                    "run_id": run_id,
                    "smallcase_id": smallcase_id,
                    "status": EngineTaskStatus.COMPLETED,
                    "execution_time_ms": elapsed_ms,
                    "predicted_outcome": outcome,
                },
            )
        except BaseException as exc:
            if await self._handle_worker_failure(
                exc=exc,
                model=TestBenchRunModel,
                task_id=run_id,
                current_status=current_status,
                session_factory=session_factory,
                ws_manager=ws_manager,
                event="test_bench.state",
                id_field="run_id",
                smallcase_id=smallcase_id,
            ):
                raise

    def _parse_match_context(self, match_context: Mapping[str, Any]) -> tuple[BaseModel | None, str | None]:
        try:
            return self._context_schema.model_validate(dict(match_context)), None
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0]["msg"] if errors else "invalid"
            return None, f"{exc.error_count()} validation error(s); first: {first}"

    async def _invoke_native_model(self, model_cls: type[Any], context: BaseModel) -> dict[str, Any]:
        instance = model_cls()
        target: Any = None
        for method_name in NATIVE_METHOD_CANDIDATES:
            candidate = getattr(instance, method_name, None)
            if callable(candidate):
                target = candidate
                break
        if target is None:
            if callable(instance):
                target = instance
            else:
                raise TypeError(f"{model_cls.__name__} exposes no callable prediction entrypoint.")

        async def _call() -> Any:
            if inspect.iscoroutinefunction(target):
                return await target(context)
            result = await asyncio.to_thread(target, context)
            if inspect.isawaitable(result):
                result = await result
            return result

        result = await asyncio.wait_for(_call(), timeout=self._native_step_timeout_s)
        payload = _to_jsonable(result)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload["_result_type"] = "PredictionResult" if isinstance(result, PredictionResult) else type(result).__name__
        return payload

    async def _run_pipeline(
        self,
        *,
        run_id: UUID,
        smallcase_id: UUID | None,
        pipeline: Sequence[str],
        match_context: Mapping[str, Any],
        ws_manager: LiveBroadcaster | None,
        is_stress_test: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not pipeline:
            raise InvalidSmallcaseStateError("Smallcase pipeline is empty; nothing to put on the conveyor belt.")

        context, context_error = self._parse_match_context(match_context)
        odds, anomalies = _extract_odds(match_context)
        state = _PipelineState(odds=odds, anomalies=list(anomalies))
        if context_error:
            state.anomalies.append(f"context_validation: {context_error}")

        steps: list[dict[str, Any]] = []
        total_steps = len(pipeline)
        for index, raw_name in enumerate(pipeline):
            model_name = str(raw_name)
            stage_kind = _classify_stage(model_name)
            rng = random.Random(f"{run_id}:{index}:{model_name}")
            started_at = _utcnow()
            tick = time.perf_counter()
            error: str | None = None
            model_cls = self._model_registry.get(model_name)

            if model_cls is not None and context is not None:
                try:
                    output = await self._invoke_native_model(model_cls, context)
                    mode = "NATIVE"
                except Exception as exc:  # noqa: BLE001 - graceful degradation to simulation
                    error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Native stage %s failed on run %s: %s", model_name, run_id, error)
                    output = self._simulate_stage(stage_kind, model_name, match_context, state, rng)
                    mode = "DEGRADED"
            else:
                output = self._simulate_stage(stage_kind, model_name, match_context, state, rng)
                if model_cls is None:
                    mode = "SIMULATED"
                else:
                    mode = "DEGRADED"
                    error = "MatchContext failed validation; native model bypassed."

            self._absorb_output(stage_kind, output, state)
            step = {
                "index": index,
                "model": model_name,
                "stage_kind": stage_kind,
                "mode": mode,
                "status": "DEGRADED" if mode == "DEGRADED" else "OK",
                "duration_ms": round((time.perf_counter() - tick) * 1000.0, 3),
                "started_at": started_at.isoformat(),
                "output": _to_jsonable(output),
                "error": error,
            }
            steps.append(step)
            await self._emit(
                ws_manager,
                "test_bench.step",
                {"run_id": run_id, "smallcase_id": smallcase_id, "total_steps": total_steps, "step": step},
            )

        outcome = self._build_outcome(
            pipeline=pipeline, steps=steps, state=state, context_validated=context is not None, is_stress_test=is_stress_test
        )
        return steps, outcome

    def _simulate_stage(
        self,
        stage_kind: str,
        model_name: str,
        match_context: Mapping[str, Any],
        state: _PipelineState,
        rng: random.Random,
    ) -> dict[str, Any]:
        if stage_kind == "staking":
            return self._simulate_staking(state)
        if stage_kind == "value_filter":
            return self._simulate_value_filter(state)
        if stage_kind == "blender":
            return self._simulate_blender(state)
        return self._simulate_probability(model_name, match_context, state, rng)

    @staticmethod
    def _simulate_probability(
        model_name: str, match_context: Mapping[str, Any], state: _PipelineState, rng: random.Random
    ) -> dict[str, Any]:
        expected_goals = _extract_expected_goals(match_context)
        base: dict[str, float] | None = None
        method = "uniform_prior"
        if expected_goals is not None:
            base = _poisson_outcome_probabilities(*expected_goals)
            method = "poisson_expected_goals"
        if base is None and state.current_probs is not None:
            base = state.current_probs
            method = "upstream_refinement"
        if base is None:
            base = _implied_probabilities(state.odds)
            method = "implied_odds"
        if base is None:
            base = _uniform()
            method = "uniform_prior"
        scale = 0.05 if "monte" in model_name.lower() else 0.025
        probs = _perturb(base, rng, scale)
        return {"probabilities": _round_probs(probs), "method": method, "simulated": True}

    @staticmethod
    def _simulate_value_filter(state: _PipelineState) -> dict[str, Any]:
        probs = state.best_probabilities()
        edges = {
            o: round(probs[o] * state.odds[o] - 1.0, 6) if state.odds[o] > 1.0 else -1.0 for o in OUTCOMES
        }
        value_outcomes = [o for o in OUTCOMES if edges[o] > 0.0]
        return {"edges": edges, "value_outcomes": value_outcomes, "probabilities": _round_probs(probs), "simulated": True}

    @staticmethod
    def _simulate_blender(state: _PipelineState) -> dict[str, Any]:
        sources = state.history or [state.best_probabilities()]
        blended = _normalize({o: sum(s[o] for s in sources) / len(sources) for o in OUTCOMES}) or _uniform()
        return {
            "probabilities": _round_probs(blended),
            "blended_sources": len(sources),
            "method": "consensus_mean",
            "simulated": True,
        }

    @staticmethod
    def _simulate_staking(state: _PipelineState) -> dict[str, Any]:
        probs = state.best_probabilities()
        candidates = state.value_outcomes if state.value_outcomes is not None else list(OUTCOMES)
        stakes = {
            o: round(_kelly_fraction(probs[o], state.odds[o]), 6) if o in candidates else 0.0 for o in OUTCOMES
        }
        selection: str | None = max(OUTCOMES, key=lambda o: stakes[o])
        fraction = stakes[selection]
        if fraction <= 0.0:
            selection = None
            fraction = 0.0
        return {
            "stake_fraction": fraction,
            "selection": selection,
            "stakes": stakes,
            "kelly_cap": MAX_KELLY_FRACTION,
            "probabilities": _round_probs(probs),
            "simulated": True,
        }

    @staticmethod
    def _absorb_output(stage_kind: str, output: Mapping[str, Any], state: _PipelineState) -> None:
        if stage_kind == "staking":
            state.kelly = dict(output)
            return
        if stage_kind == "value_filter":
            edges = output.get("edges")
            values = output.get("value_outcomes")
            state.edges = dict(edges) if isinstance(edges, Mapping) else None
            state.value_outcomes = [v for v in values if v in OUTCOMES] if isinstance(values, list) else None
            return
        probs = _extract_probabilities(output)
        if probs is not None:
            state.current_probs = probs
            state.history.append(probs)

    def _build_outcome(
        self,
        *,
        pipeline: Sequence[str],
        steps: Sequence[Mapping[str, Any]],
        state: _PipelineState,
        context_validated: bool,
        is_stress_test: bool,
    ) -> dict[str, Any]:
        probs = state.best_probabilities()
        pick = max(OUTCOMES, key=lambda o: probs[o])
        kelly = state.kelly or {}
        stake = _safe_float(kelly.get("stake_fraction"), 0.0) or 0.0
        outcome: dict[str, Any] = {
            "pick": pick,
            "confidence": round(probs[pick], 6),
            "probabilities": _round_probs(probs),
            "odds": {o: round(state.odds[o], 6) for o in OUTCOMES},
            "selection": kelly.get("selection"),
            "stake_fraction": round(stake, 6),
            "value_outcomes": state.value_outcomes or [],
            "pipeline": list(pipeline),
            "native_steps": sum(1 for s in steps if s["mode"] == "NATIVE"),
            "simulated_steps": sum(1 for s in steps if s["mode"] == "SIMULATED"),
            "degraded_steps": sum(1 for s in steps if s["mode"] == "DEGRADED"),
            "context_validated": context_validated,
            "anomalies": list(state.anomalies),
            "stress_test": is_stress_test,
        }
        if is_stress_test:
            outcome["stress_report"] = self._build_stress_report(probs, stake, state)
        return _to_jsonable(outcome)

    @staticmethod
    def _build_stress_report(probs: Mapping[str, float], stake: float, state: _PipelineState) -> dict[str, Any]:
        total = sum(probs.values())
        stakes = (state.kelly or {}).get("stakes") or {}
        checks = {
            "probability_sum_ok": abs(total - 1.0) <= 1e-6,
            "probabilities_bounded": all(0.0 <= p <= 1.0 for p in probs.values()),
            "stake_bounded": 0.0 <= stake <= MAX_KELLY_FRACTION,
            "no_bet_on_invalid_odds": all(
                (_safe_float(stakes.get(o), 0.0) or 0.0) <= 0.0 for o in OUTCOMES if state.odds[o] <= 1.0
            ),
        }
        return {
            **checks,
            "probability_sum": round(total, 9),
            "anomalies_detected": len(state.anomalies),
            "passed": all(checks.values()),
        }

    # ------------------------------------------------------------------ backtest

    def _pipeline_profile(self, pipeline: Sequence[str]) -> dict[str, Any]:
        stages = [_classify_stage(name) for name in pipeline]
        probability_models = [name for name, kind in zip(pipeline, stages, strict=True) if kind == "probability"]
        noise = 0.14
        noise -= 0.02 * len(probability_models)
        noise -= 0.01 * sum(1 for name in probability_models if name in self._model_registry)
        noise -= 0.02 if "blender" in stages else 0.0
        return {
            "noise": max(noise, 0.03),
            "uses_kelly": "staking" in stages,
            "edge_threshold": 0.03 if "value_filter" in stages else 0.0,
        }

    async def _simulate_backtest(
        self,
        *,
        job_id: UUID,
        smallcase_id: UUID,
        pipeline: Sequence[str],
        start_date: date,
        end_date: date,
        ws_manager: LiveBroadcaster | None,
    ) -> dict[str, Any]:
        if not pipeline:
            raise InvalidSmallcaseStateError("Smallcase pipeline is empty; nothing to backtest.")
        profile = self._pipeline_profile(pipeline)
        noise: float = profile["noise"]
        uses_kelly: bool = profile["uses_kelly"]
        edge_threshold: float = profile["edge_threshold"]

        days = (end_date - start_date).days + 1
        total = min(max(days * MATCHES_PER_DAY, MIN_BACKTEST_MATCHES), MAX_BACKTEST_MATCHES)
        rng = random.Random(f"backtest:{job_id}:{smallcase_id}")

        bankroll = STARTING_BANKROLL
        peak = bankroll
        max_drawdown = 0.0
        total_staked = 0.0
        total_returned = 0.0
        correct = 0
        bets = 0
        simulated = 0
        ruined = False
        fold_hits = [0] * CROSS_VAL_FOLDS
        fold_counts = [0] * CROSS_VAL_FOLDS

        for match_index in range(total):
            true_probs = _normalize(
                {
                    "home": rng.gammavariate(2.4, 1.0),
                    "draw": rng.gammavariate(1.5, 1.0),
                    "away": rng.gammavariate(1.9, 1.0),
                }
            ) or _uniform()
            margin = rng.uniform(0.03, 0.07)
            odds = {o: max(1.01, 1.0 / (true_probs[o] * (1.0 + margin))) for o in OUTCOMES}
            model_probs = _normalize(
                {o: max(true_probs[o] * (1.0 + rng.gauss(0.0, noise)), 1e-6) for o in OUTCOMES}
            ) or true_probs

            predicted = max(OUTCOMES, key=lambda o: model_probs[o])
            actual = _weighted_choice(true_probs, rng)
            hit = predicted == actual
            correct += int(hit)
            fold = match_index % CROSS_VAL_FOLDS
            fold_counts[fold] += 1
            fold_hits[fold] += int(hit)

            edges = {o: model_probs[o] * odds[o] - 1.0 for o in OUTCOMES}
            selection = max(OUTCOMES, key=lambda o: edges[o])
            if edges[selection] > edge_threshold:
                if uses_kelly:
                    fraction = _kelly_fraction(model_probs[selection], odds[selection]) * BACKTEST_KELLY_MULTIPLIER
                else:
                    fraction = FLAT_STAKE_FRACTION
                fraction = min(fraction, MAX_BACKTEST_STAKE_FRACTION)
                stake = bankroll * fraction
                if stake > 0.0:
                    bets += 1
                    total_staked += stake
                    if actual == selection:
                        payout = stake * odds[selection]
                        total_returned += payout
                        bankroll += payout - stake
                    else:
                        bankroll -= stake
                    peak = max(peak, bankroll)
                    if peak > 0.0:
                        max_drawdown = max(max_drawdown, (peak - bankroll) / peak)

            simulated += 1
            if bankroll <= STARTING_BANKROLL * 1e-6:
                ruined = True
                break
            if simulated % BACKTEST_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            if simulated % BACKTEST_PROGRESS_EVERY == 0:
                await self._emit(
                    ws_manager,
                    "backtest.progress",
                    {
                        "job_id": job_id,
                        "smallcase_id": smallcase_id,
                        "simulated": simulated,
                        "total": total,
                        "progress_pct": round(simulated / total * 100.0, 2),
                        "bankroll": round(bankroll, 4),
                    },
                )

        roi_pct = ((total_returned - total_staked) / total_staked * 100.0) if total_staked > 0.0 else 0.0
        accuracy_pct = round(correct / simulated * 100.0, 4) if simulated else 0.0
        fold_scores = [hits / count for hits, count in zip(fold_hits, fold_counts, strict=True) if count]
        cross_val_score = round(sum(fold_scores) / len(fold_scores), 6) if fold_scores else 0.0
        return {
            "total_matches_simulated": simulated,
            "roi_pct": round(max(roi_pct, -100.0), 4),
            "accuracy_pct": min(max(accuracy_pct, 0.0), 100.0),
            "max_drawdown_pct": round(min(max(max_drawdown * 100.0, 0.0), 100.0), 4),
            "cross_val_score": min(max(cross_val_score, 0.0), 1.0),
            "bets_placed": bets,
            "final_bankroll": round(bankroll, 4),
            "ruined": ruined,
        }

    async def execute_backtest_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        job_id: UUID,
        ws_manager: LiveBroadcaster | None,
    ) -> None:
        current_status = EngineTaskStatus.QUEUED
        smallcase_id: UUID | None = None
        try:
            async with session_factory() as session:
                claimed = await self._compare_and_swap_status(
                    session,
                    BacktestJobModel,
                    job_id,
                    expected=EngineTaskStatus.QUEUED,
                    target=EngineTaskStatus.RUNNING,
                )
                if not claimed:
                    await session.rollback()
                    logger.warning("Backtest job %s was not QUEUED; skipping duplicate dispatch.", job_id)
                    return
                await session.commit()
                current_status = EngineTaskStatus.RUNNING

                job = await session.get(BacktestJobModel, job_id, populate_existing=True)
                if job is None:
                    raise CoreDomainError(f"Backtest job {job_id} disappeared after claim.")
                smallcase = await session.get(SmallcaseRegistryModel, job.smallcase_id, populate_existing=True)
                if smallcase is None:
                    raise SmallcaseNotFoundError(job.smallcase_id)
                smallcase_id = smallcase.id
                pipeline = [str(name) for name in (smallcase.pipeline_config or [])]
                start_date, end_date = job.start_date, job.end_date

            await self._emit(
                ws_manager,
                "backtest.state",
                {"job_id": job_id, "smallcase_id": smallcase_id, "status": EngineTaskStatus.RUNNING},
            )

            metrics = await self._simulate_backtest(
                job_id=job_id,
                smallcase_id=smallcase_id,
                pipeline=pipeline,
                start_date=start_date,
                end_date=end_date,
                ws_manager=ws_manager,
            )
            completed_at = _utcnow()

            async with session_factory() as session:
                finalized = await self._compare_and_swap_status(
                    session,
                    BacktestJobModel,
                    job_id,
                    expected=EngineTaskStatus.RUNNING,
                    target=EngineTaskStatus.COMPLETED,
                    total_matches_simulated=metrics["total_matches_simulated"],
                    roi_pct=metrics["roi_pct"],
                    accuracy_pct=metrics["accuracy_pct"],
                    max_drawdown_pct=metrics["max_drawdown_pct"],
                    error_detail=None,
                    completed_at=completed_at,
                )
                if not finalized:
                    await session.rollback()
                    raise EngineConcurrencyError(f"Backtest job {job_id} left RUNNING before finalisation.")
                await session.execute(
                    update(SmallcaseRegistryModel)
                    .where(SmallcaseRegistryModel.id == smallcase_id)
                    .values(
                        total_backtests_run=SmallcaseRegistryModel.total_backtests_run + 1,
                        current_accuracy=metrics["accuracy_pct"] / 100.0,
                        cross_val_score=metrics["cross_val_score"],
                        last_tested_at=completed_at,
                        updated_at=completed_at,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            current_status = EngineTaskStatus.COMPLETED

            await self._emit(
                ws_manager,
                "backtest.state",
                {"job_id": job_id, "smallcase_id": smallcase_id, "status": EngineTaskStatus.COMPLETED, **metrics},
            )
        except BaseException as exc:
            if await self._handle_worker_failure(
                exc=exc,
                model=BacktestJobModel,
                task_id=job_id,
                current_status=current_status,
                session_factory=session_factory,
                ws_manager=ws_manager,
                event="backtest.state",
                id_field="job_id",
                smallcase_id=smallcase_id,
            ):
                raise
