"""A backtest from request to result: the bots, the data, the runs, the verdicts.

1. The bots. Saved Hive bots and ad-hoc strategies become ``SimBot``s. Every component key is
   resolved against the model registry rows seeded in Group 65 (``core_smallcase_registry``) and
   validated as live pipelines are (``validate_pipeline``): the backtest runs the same components a
   bot would run live, and records which registry rows it used.
2. The data. A store per horizon, read through the time lock: the full window (plus three days for
   results), and, for a walk-forward test, an in-sample store whose horizon is the split itself.
3. The runs. A Kelly sweep (on the in-sample window when walk-forward is on), the best multiplier
   locked; the out-of-sample run with exactly the locked parameters; the full-window run with them,
   which the equity curve, the metrics and the Monte Carlo describe. With ``walk_forward_folds > 1``
   (Group 77) the same is repeated on rolling folds (``engine_math.rolling_folds``), each tuned on its own
   in-sample store and judged on the window after it, with walk-forward efficiency across them.
4. The verdicts: the sweep table, in-sample against out-of-sample, the risk of ruin.

``run_backtest`` drives one ``LabBacktestRun`` row through RUNNING to COMPLETED (or FAILED, with
the reason), reporting progress as it goes. The CPU-bound part runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.backtesting.engine_math import Fold, rolling_folds, walk_forward_summary
from app.models.hive_bots import BotStatus, TradingBot
from app.models.lab_quant import BacktestStatus, LabBacktestRun, LabFixtureResult, LabOddsTick
from app.models.the_core import SmallcaseRegistryModel
from app.schemas.lab_quant import BacktestParams, StrategySpec
from app.services.backtesting.fx_router import HistoricalFxRouter
from app.services.backtesting.metrics import compute_metrics, curves, penalties, trade_rows
from app.services.backtesting.monte_carlo import risk_of_ruin
from app.services.backtesting.optimizer import LockedParameters, best_row, kelly_grid, overfit_verdict
from app.services.backtesting.reality import RealityConfig
from app.services.backtesting.replay_engine import HistoricalStore, ReplayEngine, SignalStream
from app.services.backtesting.simulator import RunResult, SimBot, Simulator
from app.services.backtesting.time_lock import SimulationClock
from app.services.hive_pipeline import validate_pipeline
from app.services.hive_registry import registry_components
from app.services.venue_costs import TermsTable

logger = logging.getLogger("betdoc.lab")
SETTLE_SLACK = timedelta(days=3)  # results land within hours of kick-off; the full store reads this far past the window
_STRATEGY_NAMESPACE = uuid.UUID("6f1c4d52-6b0e-4d8e-9a66-7a1b2c3d4e66")


class BacktestError(ValueError):
    """A request that cannot run: the reason is shown to the user as is."""


# ---------------------------------------------------------------- 1. the bots
@dataclass(frozen=True, slots=True)
class PreparedBots:
    bots: list[SimBot]
    components: dict[str, list[dict[str, Any]]]  # bot id -> the registry rows its pipeline uses


def _component_rows(keys: Sequence[str], registry: dict[str, SmallcaseRegistryModel]) -> list[dict[str, Any]]:
    rows = []
    for key in keys:
        row = registry[key]
        rows.append({"key": key, "registry_id": str(row.id), "name": row.name, "kind": str(row.component_kind), "implementation": row.implementation, "category": row.category})
    return rows


async def prepare_bots(session: AsyncSession, user_id: uuid.UUID, params: BacktestParams) -> PreparedBots:
    registry = await registry_components(session)
    if not registry:
        raise BacktestError("The model registry is empty: seed it first (python -m app.db.seed_110_models)")
    rows = []
    if params.bot_ids:
        rows = list((await session.execute(select(TradingBot).where(TradingBot.id.in_(params.bot_ids), TradingBot.user_id == user_id))).scalars())
        missing = set(params.bot_ids) - {b.id for b in rows}
        if missing:
            raise BacktestError(f"{len(missing)} bot(s) not found among yours")
        if any(b.status is BotStatus.ARCHIVED for b in rows):
            raise BacktestError("an archived bot cannot be backtested")
    bots: list[SimBot] = []
    components: dict[str, list[dict[str, Any]]] = {}
    specs: list[tuple[str, Any, str]] = [(str(b.id), b, "bot") for b in rows] + [(str(uuid.uuid5(_STRATEGY_NAMESPACE, f"{i}|{s.name}")), s, "strategy") for i, s in enumerate(params.strategies)]
    for key, item, origin in specs:
        math_models, risk_models, bet_types = list(item.math_models or []), list(item.risk_models or []), list(item.target_bet_types or [])
        problems = validate_pipeline(math_models, risk_models, bet_types, registry)
        if problems:
            raise BacktestError(f"{item.name}: " + "; ".join(problems))
        capital = params.capital_inr or (item.capital_inr if isinstance(item, StrategySpec) else (Decimal(item.allocated_capital) if Decimal(item.allocated_capital) > 0 else Decimal("100000")))
        bot = SimBot(
            id=uuid.UUID(key), name=item.name, math_models=tuple(math_models), risk_models=tuple(risk_models), target_bet_types=tuple(bet_types),
            kelly_multiplier=Decimal(item.kelly_multiplier), max_stake_pct=Decimal(item.max_stake_pct), min_edge_pct=Decimal(item.min_edge_pct),
            min_quoting_books=int(item.min_quoting_books), min_market_liquidity=Decimal(item.min_market_liquidity), enable_order_slicing=bool(item.enable_order_slicing),
            slice_size_inr=Decimal(item.slice_size_inr), max_bets_per_minute=int(item.max_bets_per_minute), drawdown_limit_pct=Decimal(item.drawdown_limit_pct),
            cooldown_seconds=int(item.cooldown_seconds), risk_params=dict(item.risk_params or {}), capital=Decimal(capital).quantize(Decimal("0.01")), origin=origin,
        )
        bots.append(bot)
        components[key] = _component_rows([*math_models, *risk_models, *bet_types], registry)
    if len({b.name for b in bots}) != len(bots):
        raise BacktestError("bots and strategies in one backtest need distinct names")
    return PreparedBots(bots, components)


# ---------------------------------------------------------------- 2. the data
async def dataset_window(session: AsyncSession) -> tuple[datetime, datetime] | None:
    first, last = (await session.execute(select(func.min(LabOddsTick.created_at), func.max(LabOddsTick.created_at)))).one()
    if first is None:
        return None
    return _aware(first), _aware(last)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime
    split: datetime

    def as_dict(self, params: BacktestParams) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "split": self.split.isoformat(), "train_ratio": params.train_ratio, "walk_forward": params.oos_enabled}


async def resolve_window(session: AsyncSession, params: BacktestParams) -> Window:
    span = await dataset_window(session)
    if span is None:
        raise BacktestError("No historical ticks loaded: seed them first (python -m app.db.seed_historical_ticks)")
    start = _aware(params.start) if params.start else span[0]
    end = _aware(params.end) if params.end else span[1]
    start, end = max(start, span[0]), min(end, span[1])
    if end - start < timedelta(days=7):
        raise BacktestError("the window must cover at least 7 days of the dataset")
    split = start + (end - start) * params.train_ratio
    return Window(start, end, split.replace(microsecond=0))


def reality_for(params: BacktestParams, settings: Settings) -> RealityConfig:
    return RealityConfig(
        latency_ms=(params.latency_min_ms, params.latency_max_ms), bets_per_second=params.bets_per_second, burst=params.burst,
        max_queue_seconds=params.max_queue_seconds, slippage_pct=params.slippage_pct, max_slippage_pct=params.max_slippage_pct,
        impact_threshold=params.impact_threshold_pct / Decimal(100), impact_coefficient=params.impact_coefficient, impact_model=params.impact_model,
        void_rate=float(params.void_rate_pct) / 100,
        unreported_liquidity_inr=params.unreported_liquidity_inr, fx_haircut=Decimal(str(settings.FX_HAIRCUT_PCT)) / Decimal(100), seed=params.seed,
    )


def fx_factory(store: HistoricalStore, settings: Settings) -> Callable[[SimulationClock], HistoricalFxRouter]:
    max_age = timedelta(hours=settings.LAB_FX_MAX_AGE_HOURS)
    haircut = Decimal(str(settings.FX_HAIRCUT_PCT)) / Decimal(100)
    return lambda clock: HistoricalFxRouter(store.fx, settings.LAB_STATIC_FX_RATES, clock, max_age=max_age, haircut=haircut)


def commissions_for(store: HistoricalStore, settings: Settings) -> dict[str, Decimal]:
    terms = TermsTable(settings)
    return {book: terms(book).commission for book in {t.bookmaker_id for t in store.ticks}}


# ---------------------------------------------------------------- 3 + 4. the runs and the verdicts
class Progress:
    """Written by the worker thread, read by the heartbeat: a stage and a fraction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stage, self.fraction = "starting", 0.0

    def set(self, stage: str, fraction: float) -> None:
        with self._lock:
            self.stage, self.fraction = stage, min(max(fraction, 0.0), 1.0)

    def read(self) -> tuple[str, float]:
        with self._lock:
            return self.stage, self.fraction


def _simulate(store: HistoricalStore, stream: SignalStream, bots: Sequence[SimBot], reality: RealityConfig, settings: Settings, start: datetime, end: datetime, resume: timedelta | None, label: str) -> RunResult:
    return Simulator(store, stream, bots, reality, settings, fx_factory(store, settings), start=start, end=end, resume_after=resume, label=label).run()


def _per_bot(run: RunResult) -> list[dict[str, Any]]:
    out = []
    for bot in run.bots:
        mine = [p for p in run.settled if p.bot_id == bot.id]
        graded = [p for p in mine if p.status in ("WON", "LOST")]
        staked = sum((p.cost_inr for p in graded), Decimal(0))
        pnl = sum((p.pnl_inr or Decimal(0) for p in mine), Decimal(0))
        curve = run.bot_curves.get(str(bot.id), [])
        out.append({
            "bot_id": str(bot.id), "name": bot.name, "origin": bot.origin, "trades": len(graded), "voids": len(mine) - len(graded),
            "staked_inr": str(staked.quantize(Decimal("0.01"))), "pnl_inr": str(pnl.quantize(Decimal("0.01"))),
            "roi_pct": float(round(pnl / staked * 100, 4)) if staked else None, "starting_capital_inr": str(bot.capital),
            "final_equity_inr": str(curve[-1][1]) if curve else str(bot.capital), "kelly_multiplier": str(bot.kelly_multiplier),
            "suspensions": sum(1 for e in run.events if e["event"] == "SUSPENDED" and e.get("bot") == bot.name),
        })
    return out


def risk_free(params: BacktestParams, settings: Settings) -> float:
    return settings.LAB_RISK_FREE_RATE if params.risk_free_rate is None else params.risk_free_rate


def folds_for(params: BacktestParams, window: Window) -> list[Fold]:
    return rolling_folds(window.start, window.end, params.walk_forward_folds, params.train_ratio) if params.oos_enabled and params.walk_forward_folds > 1 else []


def _rolling(bots: Sequence[SimBot], params: BacktestParams, folds: Sequence[Fold], fold_stores: Sequence[HistoricalStore], store: HistoricalStore, stream: SignalStream,
             reality: RealityConfig, settings: Settings, resume: timedelta | None, report: Progress, rf: float) -> dict[str, Any]:
    """Each fold tuned on its own in-sample window only (its store ends at its split), then judged on the window after it."""
    rows: list[dict[str, Any]] = []
    for fold, fold_store in zip(folds, fold_stores, strict=True):
        base = 0.78 + 0.08 * (fold.index - 1) / len(folds)
        report.set(f"rolling fold {fold.index}/{len(folds)}: in-sample replay", base)
        is_stream = ReplayEngine(fold_store, settings, fx_factory(fold_store, settings), commissions_for(fold_store, settings)).build(fold.start, fold.split)
        if params.sweep_enabled:
            scored = []
            for kelly in kelly_grid(params.kelly_min, params.kelly_max, params.sweep_steps):
                run = _simulate(fold_store, is_stream, [b.tuned(kelly) for b in bots], reality, settings, fold.start, fold.split, resume, f"fold{fold.index}:sweep:{kelly}")
                scored.append(({"kelly": str(kelly), **{k: v for k, v in compute_metrics(run, fold_store, rf).items() if k in ("sharpe", "roi_pct", "return_pct", "trades")}}, run))
            best = best_row([row for row, _ in scored])
            kelly_used: Decimal | None = Decimal(best["kelly"])
            is_run = next(run for row, run in scored if row["kelly"] == best["kelly"])
        else:
            kelly_used = None
            is_run = _simulate(fold_store, is_stream, bots, reality, settings, fold.start, fold.split, resume, f"fold{fold.index}:in_sample")
        tuned = [b.tuned(kelly_used) for b in bots] if kelly_used is not None else list(bots)
        report.set(f"rolling fold {fold.index}/{len(folds)}: out-of-sample", base + 0.04 / len(folds))
        oos_run = _simulate(store, stream, tuned, reality, settings, fold.split, fold.end, resume, f"fold{fold.index}:out_of_sample")
        is_m, oos_m = compute_metrics(is_run, fold_store, rf), compute_metrics(oos_run, store, rf)
        keep = ("sharpe", "sortino", "roi_pct", "return_pct", "max_drawdown_pct", "trades", "pnl_inr", "brier_skill_score")
        rows.append({**fold.as_dict(), "kelly": None if kelly_used is None else str(kelly_used), "in_sample": {k: is_m[k] for k in keep},
                     "out_of_sample": {k: oos_m[k] for k in keep}, "verdict": overfit_verdict(is_m, oos_m)})
    return {"folds": rows, "summary": walk_forward_summary(rows), "train_ratio": params.train_ratio}


def execute(
    bots: Sequence[SimBot], params: BacktestParams, window: Window, store: HistoricalStore, in_sample_store: HistoricalStore | None, settings: Settings, progress: Progress | None = None,
    fold_stores: Sequence[HistoricalStore] = (),
) -> dict[str, Any]:
    started = time.perf_counter()
    rf = risk_free(params, settings)
    report = progress or Progress()
    reality = reality_for(params, settings)
    resume = None if params.resume_after_hours == 0 else timedelta(hours=params.resume_after_hours)
    commissions = commissions_for(store, settings)
    names = {str(b.id): b.name for b in bots}
    warnings: list[str] = []

    report.set("replaying the full window", 0.02)
    stream = ReplayEngine(store, settings, fx_factory(store, settings), commissions).build(window.start, window.end, progress=lambda f: report.set("replaying the full window", 0.02 + 0.28 * f))
    is_stream: SignalStream | None = None
    is_store = store
    if params.oos_enabled:
        if in_sample_store is None:
            raise BacktestError("a walk-forward test needs the in-sample store")
        is_store = in_sample_store
        report.set("replaying the in-sample window", 0.30)
        is_stream = ReplayEngine(in_sample_store, settings, fx_factory(in_sample_store, settings), commissions_for(in_sample_store, settings)).build(
            window.start, window.split, progress=lambda f: report.set("replaying the in-sample window", 0.30 + 0.20 * f)
        )
    tune_stream, tune_end = (is_stream, window.split) if is_stream is not None else (stream, window.end)
    tune_window = "in_sample" if is_stream is not None else "full"

    sweep_rows: list[dict[str, Any]] = []
    sweep_runs: dict[str, RunResult] = {}
    if params.sweep_enabled:
        grid = kelly_grid(params.kelly_min, params.kelly_max, params.sweep_steps)
        for i, kelly in enumerate(grid):
            report.set(f"sweep {i + 1}/{len(grid)}: Kelly {kelly}", 0.50 + 0.25 * i / len(grid))
            run = _simulate(is_store, tune_stream, [b.tuned(kelly) for b in bots], reality, settings, window.start, tune_end, resume, f"sweep:{kelly}")
            metrics = compute_metrics(run, is_store, rf)
            sweep_rows.append({"kelly": str(kelly), **{k: metrics[k] for k in ("sharpe", "sortino", "calmar", "roi_pct", "return_pct", "max_drawdown_pct", "trades", "pnl_inr")}})
            sweep_runs[str(kelly)] = run
        best = best_row(sweep_rows)
        locked = LockedParameters(Decimal(best["kelly"]), "sweep:sharpe", tune_window)
        if best.get("sharpe") is None:
            warnings.append("no sweep run produced a Sharpe ratio (too few trades or a flat curve): the smallest multiplier was kept")
    else:
        locked = LockedParameters(None, "bot_settings", tune_window)
    final_bots = [b.tuned(locked.kelly_multiplier) if locked.kelly_multiplier is not None else b for b in bots]
    locked_view = locked.as_dict([{"id": str(b.id), **b.parameters()} for b in final_bots])

    walk_forward: dict[str, Any] = {"enabled": params.oos_enabled}
    if params.oos_enabled:
        if params.sweep_enabled:
            is_run = sweep_runs[str(locked.kelly_multiplier)]
        else:
            report.set("in-sample run", 0.62)
            is_run = _simulate(is_store, tune_stream, final_bots, reality, settings, window.start, window.split, resume, "in_sample")
        report.set("out-of-sample run with the locked parameters", 0.78)
        oos_run = _simulate(store, stream, final_bots, reality, settings, window.split, window.end, resume, "out_of_sample")
        is_metrics, oos_metrics = compute_metrics(is_run, is_store, rf), compute_metrics(oos_run, store, rf)
        walk_forward.update({
            "in_sample": is_metrics, "out_of_sample": oos_metrics, "verdict": overfit_verdict(is_metrics, oos_metrics),
            "locked_parameters": locked_view, "in_sample_curve": curves(is_run)["equity"], "out_of_sample_curve": curves(oos_run)["equity"],
        })
        folds = folds_for(params, window)
        if folds:
            if len(fold_stores) != len(folds):
                raise BacktestError("rolling walk-forward needs one in-sample store per fold")
            walk_forward["rolling"] = _rolling(bots, params, folds, fold_stores, store, stream, reality, settings, resume, report, rf)

    if not params.oos_enabled and params.sweep_enabled:
        display = sweep_runs[str(locked.kelly_multiplier)]
    else:
        report.set("full-window run", 0.86)
        display = _simulate(store, stream, final_bots, reality, settings, window.start, window.end, resume, "full")
    metrics = compute_metrics(display, store, rf)
    report.set("Monte Carlo resampling", 0.93)
    settled = sorted(display.settled, key=lambda p: (p.settled_at or p.filled_at, p.id))
    pnls = [float(p.pnl_inr or 0) for p in settled if p.status in ("WON", "LOST")]
    mc = risk_of_ruin(pnls, float(display.capital), iterations=params.monte_carlo_iterations, ruin_floor_pct=params.ruin_floor_pct, seed=params.seed)
    if metrics["trades"] < 30:
        warnings.append(f"only {metrics['trades']} graded trades: every statistic here has wide error bars")
    if any(d.startswith("synthetic") for d in store.datasets):
        warnings.append("the dataset is synthetic (app.db.seed_historical_ticks): these results test the engine and the strategy's mechanics, not a real-world edge")
    report.set("done", 1.0)
    span = store.span
    return {
        "dataset": {"fingerprint": store.fingerprint(), "datasets": list(store.datasets), "ticks": len(store.ticks), "fixtures": len(store.fixtures),
                    "first_tick": span[0].isoformat() if span else None, "last_tick": span[1].isoformat() if span else None, "horizon": store.horizon.isoformat()},
        "window": window.as_dict(params),
        "locked_parameters": locked_view,
        "metrics": metrics,
        "per_bot": _per_bot(display),
        "curves": curves(display),
        "penalties": penalties(display),
        "monte_carlo": mc,
        "sweep": {"enabled": params.sweep_enabled, "objective": "sharpe", "window": tune_window, "rows": sweep_rows,
                  "best_kelly": None if locked.kelly_multiplier is None else str(locked.kelly_multiplier)},
        "walk_forward": walk_forward,
        "events": display.events,
        "trades": trade_rows(display, store, names),
        "stream": {"frames": stream.frames, "evaluated": stream.evaluated, "signals": len(stream.signals), "shocks": len(stream.shocks), "skipped": dict(stream.skipped)},
        "warnings": warnings,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def summary_of(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    m = result.get("metrics", {})
    return {
        "roi_pct": m.get("roi_pct"), "sharpe": m.get("sharpe"), "max_drawdown_pct": m.get("max_drawdown_pct"), "trades": m.get("trades"),
        "risk_of_ruin_pct": result.get("monte_carlo", {}).get("risk_of_ruin_pct"), "verdict": (result.get("walk_forward", {}).get("verdict") or {}).get("verdict"),
        "best_kelly": result.get("sweep", {}).get("best_kelly"),
    }


# ---------------------------------------------------------------- the run row
async def run_backtest(session_factory: async_sessionmaker[AsyncSession], settings: Settings, run_id: uuid.UUID) -> None:
    async with session_factory() as session:
        run = await session.get(LabBacktestRun, run_id)
        if run is None or run.status not in (BacktestStatus.QUEUED, BacktestStatus.RUNNING):
            return
        run.status, run.started_at, run.heartbeat_at, run.stage, run.progress = BacktestStatus.RUNNING, datetime.now(UTC), datetime.now(UTC), "loading", 0.0
        user_id, raw = run.user_id, dict(run.params)
        await session.commit()
    progress = Progress()
    try:
        params = BacktestParams.model_validate(raw)
        async with session_factory() as session:
            prepared = await prepare_bots(session, user_id, params)
            window = await resolve_window(session, params)
            last_result = await session.scalar(select(func.max(LabFixtureResult.created_at)))
            horizon = window.end + SETTLE_SLACK
            if last_result is not None:
                horizon = min(horizon, max(_aware(last_result), window.end))
            store = await HistoricalStore.load(session, horizon=horizon)
            in_sample = await HistoricalStore.load(session, horizon=window.split) if params.oos_enabled else None
            fold_stores = [await HistoricalStore.load(session, horizon=fold.split) for fold in folds_for(params, window)]
        task = asyncio.create_task(asyncio.to_thread(execute, prepared.bots, params, window, store, in_sample, settings, progress, fold_stores))
        while not task.done():
            await asyncio.sleep(1.0)
            stage, fraction = progress.read()
            async with session_factory() as session:
                row = await session.get(LabBacktestRun, run_id)
                if row is not None:
                    row.stage, row.progress, row.heartbeat_at = stage[:120], round(fraction, 4), datetime.now(UTC)
                    await session.commit()
        result = task.result()
        result["bots"] = [{"id": str(b.id), "name": b.name, "origin": b.origin, "parameters": b.parameters(), "starting_capital_inr": str(b.capital), "components": prepared.components[str(b.id)]} for b in prepared.bots]
        async with session_factory() as session:
            row = await session.get(LabBacktestRun, run_id)
            if row is not None:
                row.status, row.result, row.progress, row.stage, row.finished_at = BacktestStatus.COMPLETED, result, 1.0, "done", datetime.now(UTC)
                await session.commit()
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with its reason
        known = isinstance(exc, BacktestError)
        if not known:
            logger.exception("Lab: backtest %s failed", run_id)
        message = str(exc) if known else f"{type(exc).__name__}: {exc}"
        async with session_factory() as session:
            row = await session.get(LabBacktestRun, run_id)
            if row is not None:
                row.status, row.error, row.finished_at, row.stage = BacktestStatus.FAILED, message[:4000], datetime.now(UTC), "failed"
                await session.commit()
