"""A Hive bot's pipeline: what one bot makes of one live signal.

1. Market gates. The signal's market must be one of the bot's target bet types; the line must be
   quoted by at least ``min_quoting_books`` fresh books (a lone book is a "ghost line"); a
   ``min_market_liquidity`` above zero needs a traded volume the source actually reports.
2. Math. Every selected probability model that can run on the live market gives its own estimate
   of the selection's fair probability: the de-vig methods from the current books, the time-series
   models from the selection's consensus-probability history. A model that cannot produce one has
   no opinion; it never counts as a confirmation. Conviction is the mean of the estimates; EV
   ``p * net_odds - 1``, net of the venue's commission on winnings, must clear the bot's minimum edge
   (and Aryabhata's +0.5% floor, under +25%).
3. Staking. Kelly on the conviction at the net price (``math.kelly_criterion``, required in every pipeline), times the
   bot's multiplier, of its sub-bankroll's equity, capped at ``max_stake_pct``.
4. Risk. Each selected risk model reads the bot's own history (per-bet returns, equity curve, open
   stakes) against a limit: under half of it, no change; between half and all of it, the stake
   scales down linearly; at the limit, a veto. Too little history is "warming up", not a pass.

Everything is pure (no I/O) once the inputs are loaded: ``load_market`` and ``load_risk_context``.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.live_odds import tick_history_key
from app.domain.math.models_v2.arima import ARIMAConfig, ARIMAModel
from app.domain.math.models_v2.moving_averages import MovingAverageConfig, MovingAverageModel
from app.domain.math.models_v3.kalman_filter import KalmanFilterModel
from app.domain.math.models_v3.ornstein_uhlenbeck import OrnsteinUhlenbeckModel
from app.domain.risk.concentration import calculate_hhi
from app.domain.risk.cvar import calculate_cvar
from app.domain.risk.drawdown import calculate_current_drawdown
from app.domain.risk.entropic_risk import entropic_risk_measure
from app.domain.risk.exposure import calculate_exposure
from app.domain.risk.kelly_portfolio import portfolio_kelly
from app.domain.risk.model_risk import edge_decay_penalty
from app.domain.risk.ratios import sortino_ratio
from app.domain.risk.spectral import exponential_spectral_risk
from app.domain.risk.var import calculate_historical_var
from app.models.cfo_vault import LedgerStatus, OPEN_STATUSES, PhantomLedger
from app.models.hive_bots import BotExecutionMode, HiveShadowPosition, ShadowStatus, TradingBot
from app.schemas.aryabhata import EdgeSignal
from app.services.aryabhata_engine import (
    MAX_PLAUSIBLE_EV,
    MIN_EV,
    BookLine,
    consensus_probabilities,
    devig,
    kelly_fraction,
    mpo_probabilities,
    multiplicative_probabilities,
    ordered_labels,
    robust_center,
    shin_probabilities,
)
from app.services.aryabhata_pipeline import read_market_books
from app.services.cfo_ledger import read_account
from app.services.hive_registry import SIGNAL_MARKETS, STAKING_MODEL

logger = logging.getLogger("betdoc.hive")

ZERO, ONE, PAISA, HUNDRED = Decimal(0), Decimal(1), Decimal("0.01"), Decimal(100)
MIN_STAKE = Decimal("1.00")
_P_FLOOR, _P_CEIL = 0.001, 0.999
MIN_RISK_HISTORY = 5  # settled bets before a returns-based risk model has a view


# ---------------------------------------------------------------- flash crash
@dataclass(frozen=True, slots=True)
class FlashSwing:
    swing_pct: float
    low: float
    high: float
    points: int


def flash_swing(probabilities: Sequence[float], *, threshold_pct: float, min_points: int, min_probability: float) -> FlashSwing | None:
    """A consensus-probability swing big enough to halt every bot, or None.

    The swing is relative, ``(high - low) / low``, so a longshot's ordinary drift (3% -> 4% is +33%)
    would read as a crash. Only a selection the market rates at ``min_probability`` or more at some
    point in the window (``HIVE_FLASH_MIN_PROBABILITY``, 10%: decimal odds 10.0 or shorter) can
    trigger one; a favourite collapsing below 10% still counts, because its high is above the floor.
    The live scan (``HiveEngine.flash_crash_scan``) and the backtester (``ReplayEngine._shock``) both
    decide with this function, so a backtest halts exactly where production would."""
    clean = [p for p in probabilities if math.isfinite(p) and 0.0 < p < 1.0]
    if len(clean) < min_points:
        return None
    low, high = min(clean), max(clean)
    if high < min_probability:
        return None
    swing = (high - low) / low * 100
    if swing <= threshold_pct:
        return None
    return FlashSwing(swing, low, high, len(clean))


# ---------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class LiveMarket:
    edge: EdgeSignal
    labels: tuple[str, ...]
    books: tuple[BookLine, ...]  # fresh, quoting, with every label of the market
    history: tuple[float, ...]  # the selection's consensus probability, oldest first
    volume_inr: Decimal | None = None  # traded volume, where the source reports it (bookmaker feeds don't)

    @property
    def bet_type(self) -> str | None:
        return SIGNAL_MARKETS.get(self.edge.market_type)


@dataclass(frozen=True, slots=True)
class RiskContext:
    equity: Decimal
    available: Decimal
    allocated: Decimal
    returns: tuple[float, ...]  # each settled bet's P&L / allocated capital, oldest first
    equity_curve: tuple[float, ...]
    open_stakes: Mapping[str, Decimal]  # fixture -> stake riding on it
    open_edges: tuple[tuple[float, float], ...]  # (edge, odds) of each open bet
    pipeline_age_days: float


async def load_market(redis: Redis, settings: Settings, edge: EdgeSignal, now: datetime) -> LiveMarket:
    market_key = f"{edge.fixture_id}|{edge.market_type}"
    try:
        books = (await read_market_books(redis, settings, [market_key])).get(market_key, ())
    except (RedisError, OSError, TimeoutError):
        books = ()
    max_age = timedelta(seconds=settings.ARYABHATA_BOOK_MAX_AGE_SECONDS)
    fresh = [b for b in books if not b.is_suspended and now - b.seen_at <= max_age]
    shapes = Counter(frozenset(b.prices) for b in fresh)
    labels = ordered_labels(shapes.most_common(1)[0][0]) if shapes else ()
    quoting = tuple(b for b in fresh if frozenset(b.prices) == frozenset(labels))
    history: list[float] = []
    try:
        members = await redis.zrange(tick_history_key(settings, f"{market_key}|{edge.selection}"), -settings.HIVE_HISTORY_POINTS, -1)
    except (RedisError, OSError):
        members = []
    for member in members:
        parts = str(member).split("|")
        try:
            prob = float(parts[2])
        except (IndexError, ValueError):
            continue
        if math.isfinite(prob) and 0.0 < prob < 1.0:
            history.append(prob)
    return LiveMarket(edge, labels, quoting, tuple(history))


def _open_edge(true_prob: Decimal, odds: Decimal, commission: Decimal | None) -> tuple[float, float]:
    """(EV, net odds) of an open bet, both net of the venue's commission."""
    net = (Decimal(odds) - ONE) * (ONE - Decimal(commission or ZERO)) + ONE
    return float(Decimal(true_prob) * net - ONE), float(net)


async def load_risk_context(session: AsyncSession, bot: TradingBot, now: datetime) -> RiskContext:
    """The bot's own money and record: its sub-account (or, in shadow mode, its hypothetical book)."""
    allocated = Decimal(bot.allocated_capital)
    pnls: list[Decimal] = []
    open_stakes: dict[str, Decimal] = {}
    open_edges: list[tuple[float, float]] = []
    if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
        rows = (await session.execute(select(HiveShadowPosition).where(HiveShadowPosition.bot_id == bot.id).order_by(HiveShadowPosition.created_at))).scalars().all()
        for row in rows:
            if row.status is ShadowStatus.OPEN:
                open_stakes[row.fixture_id] = open_stakes.get(row.fixture_id, ZERO) + Decimal(row.stake_inr)
                if row.true_prob is not None:
                    open_edges.append(_open_edge(row.true_prob, row.odds, row.commission_rate))
            elif row.status in (ShadowStatus.WON, ShadowStatus.LOST) and row.pnl_inr is not None:
                pnls.append(Decimal(row.pnl_inr))
        equity = allocated + sum(pnls, ZERO)
        available = equity - sum(open_stakes.values(), ZERO)
    else:
        account = await read_account(session, bot.user_id, bot.id)
        equity = account.equity if account is not None else ZERO
        available = account.available_balance if account is not None else ZERO
        rows = (
            await session.execute(
                select(PhantomLedger).where(PhantomLedger.bot_id == bot.id).order_by(PhantomLedger.settled_at.is_(None), PhantomLedger.settled_at, PhantomLedger.created_at)
            )
        ).scalars().all()
        for row in rows:
            if row.status in OPEN_STATUSES:
                open_stakes[row.fixture_id] = open_stakes.get(row.fixture_id, ZERO) + Decimal(row.stake_inr)
                if row.true_prob is not None:
                    open_edges.append(_open_edge(row.true_prob, row.odds, row.commission_rate))
            elif row.status in (LedgerStatus.WON, LedgerStatus.LOST) and row.realized_pnl is not None:
                pnls.append(Decimal(row.realized_pnl))
    base = float(allocated) if allocated > 0 else 1.0
    returns = tuple(float(p) / base for p in pnls)
    start = float(allocated) - float(sum(pnls, ZERO))
    curve, running = [start], start
    for p in pnls:
        running += float(p)
        curve.append(running)
    updated = bot.pipeline_updated_at if bot.pipeline_updated_at.tzinfo else bot.pipeline_updated_at.replace(tzinfo=UTC)
    return RiskContext(equity, max(available, ZERO), allocated, returns, tuple(curve), open_stakes, tuple(open_edges), max((now - updated).total_seconds() / 86_400, 0.0))


# ---------------------------------------------------------------- math adapters
def _clip(value: float) -> Decimal | None:
    if not math.isfinite(value):
        return None
    return Decimal(str(round(min(max(value, _P_FLOOR), _P_CEIL), 10)))


def _per_book(method: Callable[[Sequence[Decimal]], Any]) -> Callable[[LiveMarket], Decimal | None]:
    def estimate(market: LiveMarket) -> Decimal | None:
        if market.edge.selection not in market.labels:
            return None
        at = market.labels.index(market.edge.selection)
        fair: list[Decimal] = []
        for book in market.books:
            try:
                out = method([book.prices[label] for label in market.labels])
            except (ArithmeticError, ValueError, Exception):  # noqa: BLE001 - one bad book is one missing opinion
                continue
            probabilities = out[0] if isinstance(out, tuple) and out and isinstance(out[0], tuple) else out
            fair.append(Decimal(probabilities[at]))
        return robust_center(fair) if fair else None

    return estimate


def _consensus(market: LiveMarket) -> Decimal | None:
    fair_books = []
    for book in market.books:
        try:
            result = devig([book.prices[label] for label in market.labels])
        except Exception:  # noqa: BLE001
            continue
        fair_books.append(dict(zip(market.labels, result.probabilities, strict=True)))
    if not fair_books or market.edge.selection not in market.labels:
        return None
    try:
        return consensus_probabilities(fair_books, market.labels)[market.edge.selection]
    except Exception:  # noqa: BLE001
        return None


def _series(market: LiveMarket, minimum: int) -> np.ndarray | None:
    return np.asarray(market.history, dtype=np.float64) if len(market.history) >= minimum else None


def _ema_steam(market: LiveMarket) -> Decimal | None:
    series = _series(market, 2)
    if series is None:
        return None
    alpha, value = 2.0 / 13.0, float(series[0])  # the 12-period EMA Aryabhata's steam detector uses
    for x in series[1:]:
        value = alpha * float(x) + (1 - alpha) * value
    return _clip(value)


def _moving_average(market: LiveMarket) -> Decimal | None:
    series = _series(market, 3)
    if series is None:
        return None
    model = MovingAverageModel(MovingAverageConfig(span=10.0))
    model.fit(series.reshape(-1, 1))
    return _clip(float(model.predict(series.reshape(-1, 1))[-1, 0]))


def _kalman(market: LiveMarket) -> Decimal | None:
    series = _series(market, 5)
    if series is None:
        return None
    model = KalmanFilterModel()
    model.fit(series)
    means = np.asarray(model.predict(series))
    return _clip(float(means.reshape(len(series), -1)[-1, 0]))


def _arima(market: LiveMarket) -> Decimal | None:
    series = _series(market, 10)
    if series is None:
        return None
    model = ARIMAModel(ARIMAConfig(p=1, d=0, q=0))
    model.fit(series)
    return _clip(float(np.asarray(model.predict(1)).ravel()[0]))


def _ornstein_uhlenbeck(market: LiveMarket) -> Decimal | None:
    series = _series(market, 10)
    if series is None or float(np.std(series)) == 0.0:
        return _clip(float(series[-1])) if series is not None else None  # a flat line: its own level
    model = OrnsteinUhlenbeckModel()
    model.fit(series)
    return _clip(float(np.asarray(model.predict(series, horizon=1)).ravel()[0]))


MATH_ADAPTERS: dict[str, Callable[[LiveMarket], Decimal | None]] = {
    "math.devig_shin": _per_book(shin_probabilities),
    "math.devig_mpo": _per_book(mpo_probabilities),
    "math.devig_multiplicative": _per_book(multiplicative_probabilities),
    "math.consensus": _consensus,
    "math.ema_steam": _ema_steam,
    "math.moving_averages": _moving_average,
    "math.kalman_filter": _kalman,
    "math.arima": _arima,
    "math.ornstein_uhlenbeck": _ornstein_uhlenbeck,
}
STAKING_MODELS = frozenset({STAKING_MODEL})


@dataclass(slots=True)
class MarketReadings:
    """Each model's estimate for one signal, computed once and shared by every bot that asks."""

    market: LiveMarket
    cache: dict[str, Decimal | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> Decimal | None:
        if key not in self.cache:
            adapter = MATH_ADAPTERS.get(key)
            try:
                self.cache[key] = adapter(self.market) if adapter is not None else None
            except Exception as exc:  # noqa: BLE001 - a model that fails has no opinion
                self.cache[key] = None
                self.errors[key] = type(exc).__name__
        return self.cache[key]


# ---------------------------------------------------------------- risk adapters
@dataclass(frozen=True, slots=True)
class RiskReading:
    key: str
    metric: float | None
    limit: float | None
    multiplier: Decimal
    veto: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "metric": self.metric, "limit": self.limit, "multiplier": str(self.multiplier), "veto": self.veto, "note": self.note}


def _utilisation(key: str, metric: float, limit: float, note: str) -> RiskReading:
    """Under half the limit: no change. Half to all of it: the stake scales down to zero. At it: veto."""
    if limit <= 0 or not math.isfinite(metric):
        return RiskReading(key, metric, limit, ONE, False, note)
    u = metric / limit
    if u >= 1:
        return RiskReading(key, metric, limit, ZERO, True, note)
    multiplier = ONE if u <= 0.5 else Decimal(str(round(2 * (1 - u), 6)))
    return RiskReading(key, metric, limit, multiplier, False, note)


def _warming(key: str, have: int) -> RiskReading:
    return RiskReading(key, None, None, ONE, False, f"warming up: {have}/{MIN_RISK_HISTORY} settled bets")


def _param(bot: TradingBot, key: str, name: str, default: float) -> float:
    try:
        return float((bot.risk_params or {}).get(key, {}).get(name, default))
    except (TypeError, ValueError, AttributeError):
        return default


RiskAdapter = Callable[[TradingBot, RiskContext, Decimal, float, float], RiskReading]


def _returns_tail(key: str, fn: Callable[[list[float]], float], default_limit: float, label: str) -> RiskAdapter:
    def adapter(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
        if len(ctx.returns) < MIN_RISK_HISTORY:
            return _warming(key, len(ctx.returns))
        return _utilisation(key, max(fn(list(ctx.returns)), 0.0), _param(bot, key, "limit", default_limit), label)

    return adapter


def _drawdown(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    limit = _param(bot, "risk.drawdown", "limit", float(bot.drawdown_limit_pct) / 100)
    return _utilisation("risk.drawdown", calculate_current_drawdown(list(ctx.equity_curve)), limit, "decline from the equity peak")


def _ratios(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    key = "risk.performance_ratios"
    if len(ctx.returns) < 2 * MIN_RISK_HISTORY:
        return RiskReading(key, None, None, ONE, False, f"warming up: {len(ctx.returns)}/{2 * MIN_RISK_HISTORY} settled bets")
    sortino = sortino_ratio(list(ctx.returns))
    floor = _param(bot, key, "floor", 0.0)
    if sortino < floor:
        return RiskReading(key, sortino, floor, Decimal("0.5"), False, "Sortino below its floor: half stakes")
    return RiskReading(key, sortino, floor, ONE, False, "Sortino")


def _exposure(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    stakes = [float(s) for s in ctx.open_stakes.values()] + [float(stake)]
    _, pct = calculate_exposure(stakes, float(ctx.equity))
    return _utilisation("risk.exposure", pct, _param(bot, "risk.exposure", "limit_pct", 50.0), "open stakes after this one, % of equity")


def _concentration(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    key = "risk.concentration"
    if len(ctx.open_stakes) < 2:
        return RiskReading(key, None, None, ONE, False, "fewer than 3 fixtures: no concentration to measure")
    weights = [float(s) for s in ctx.open_stakes.values()] + [float(stake)]
    return _utilisation(key, calculate_hhi(weights), _param(bot, key, "limit", 0.6), "HHI of stakes across fixtures")


def _kelly_portfolio(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    key = "risk.kelly_portfolio"
    cap = _param(bot, key, "max_total", 0.5)
    edges = [e for e, _ in ctx.open_edges] + [edge]
    prices = [o for _, o in ctx.open_edges] + [odds]
    scaled = portfolio_kelly(edges, prices, cap)
    alone = portfolio_kelly([edge], [odds], 10.0)
    if not alone or alone[0] <= 0 or not scaled:
        return RiskReading(key, None, cap, ONE, False, "no Kelly allocation")
    ratio = min(max(scaled[-1] / alone[0], 0.0), 1.0)
    return RiskReading(key, sum(scaled), cap, Decimal(str(round(ratio, 6))), ratio <= 0, "simultaneous Kelly scaled to the total cap")


def _model_risk(bot: TradingBot, ctx: RiskContext, stake: Decimal, edge: float, odds: float) -> RiskReading:  # noqa: ARG001
    key = "risk.model_risk"
    half_life = _param(bot, key, "half_life_days", 30.0)
    penalty = edge_decay_penalty(ctx.pipeline_age_days, half_life)
    return RiskReading(key, ctx.pipeline_age_days, half_life, Decimal(str(round(penalty, 6))), penalty < 0.05, "edge half-life since the pipeline last changed")


RISK_ADAPTERS: dict[str, RiskAdapter] = {
    "risk.cvar": _returns_tail("risk.cvar", lambda r: calculate_cvar(r, 0.95), 0.05, "CVaR 95% of per-bet returns"),
    "risk.var": _returns_tail("risk.var", lambda r: calculate_historical_var(r, 0.95), 0.04, "historical VaR 95%"),
    "risk.entropic_risk": _returns_tail("risk.entropic_risk", lambda r: entropic_risk_measure(r, 1.0), 0.03, "entropic risk (theta 1)"),
    "risk.spectral_risk": _returns_tail("risk.spectral_risk", lambda r: exponential_spectral_risk(r, 1.0), 0.04, "exponential spectral risk"),
    "risk.drawdown": _drawdown,
    "risk.performance_ratios": _ratios,
    "risk.exposure": _exposure,
    "risk.concentration": _concentration,
    "risk.kelly_portfolio": _kelly_portfolio,
    "risk.model_risk": _model_risk,
}
LIVE_COMPONENTS = frozenset(MATH_ADAPTERS) | STAKING_MODELS | frozenset(RISK_ADAPTERS) | frozenset(SIGNAL_MARKETS.values()) | {"bet.single"}


# ---------------------------------------------------------------- the decision
@dataclass(frozen=True, slots=True)
class Proposal:
    bot_id: uuid.UUID
    conviction: Decimal
    ev: Decimal
    stake: Decimal
    kelly_stake: Decimal
    readings: dict[str, str | None]
    risk: tuple[RiskReading, ...]

    def detail(self) -> dict[str, Any]:
        return {"math": self.readings, "risk": [r.as_dict() for r in self.risk], "kelly_stake": str(self.kelly_stake), "ev": str(self.ev)}


@dataclass(frozen=True, slots=True)
class Rejection:
    bot_id: uuid.UUID
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def evaluate(bot: TradingBot, readings: MarketReadings, ctx: RiskContext) -> Proposal | Rejection:
    market = readings.market
    edge = market.edge
    if market.bet_type is None or market.bet_type not in (bot.target_bet_types or []):
        return Rejection(bot.id, "BET_TYPE_NOT_TARGETED", {"market": edge.market_type})
    quoting = len(market.books)
    if quoting < bot.min_quoting_books:
        return Rejection(bot.id, "LIQUIDITY_THIN", {"books": quoting, "required": bot.min_quoting_books})
    if Decimal(bot.min_market_liquidity) > 0:
        if market.volume_inr is None:
            return Rejection(bot.id, "LIQUIDITY_UNREPORTED", {"required_inr": str(bot.min_market_liquidity)})
        if market.volume_inr < Decimal(bot.min_market_liquidity):
            return Rejection(bot.id, "LIQUIDITY_LOW", {"volume_inr": str(market.volume_inr), "required_inr": str(bot.min_market_liquidity)})
    if STAKING_MODEL not in (bot.math_models or []):
        return Rejection(bot.id, "NO_STAKING_MODEL")

    estimates = {key: readings.get(key) for key in bot.math_models if key not in STAKING_MODELS}
    opinions = [v for v in estimates.values() if v is not None]
    shown = {k: (None if v is None else str(v)) for k, v in estimates.items()}
    if not opinions:
        return Rejection(bot.id, "MODEL_NO_OPINION", {"math": shown})
    conviction = sum(opinions, ZERO) / len(opinions)
    odds, commission = Decimal(edge.odds), Decimal(edge.commission)
    net = (odds - ONE) * (ONE - commission) + ONE
    ev = conviction * net - ONE
    floor = max(Decimal(bot.min_edge_pct) / HUNDRED, MIN_EV)
    if ev < floor:
        return Rejection(bot.id, "EDGE_BELOW_MINIMUM", {"ev": str(ev.quantize(Decimal("0.0001"))), "required": str(floor), "math": shown})
    if ev > MAX_PLAUSIBLE_EV:
        return Rejection(bot.id, "EDGE_IMPLAUSIBLE", {"ev": str(ev.quantize(Decimal("0.0001"))), "math": shown})

    kelly_stake = (kelly_fraction(conviction, odds, commission) * Decimal(bot.kelly_multiplier) * ctx.equity).quantize(PAISA, rounding=ROUND_DOWN)
    cap = (ctx.equity * Decimal(bot.max_stake_pct) / HUNDRED).quantize(PAISA, rounding=ROUND_DOWN)
    stake = min(kelly_stake, cap, ctx.available)
    risk: list[RiskReading] = []
    for key in bot.risk_models or []:
        adapter = RISK_ADAPTERS.get(key)
        if adapter is None:
            return Rejection(bot.id, "RISK_MODEL_NOT_LIVE", {"model": key})
        reading = adapter(bot, ctx, stake, float(ev), float(net))
        risk.append(reading)
        if reading.veto:
            return Rejection(bot.id, "RISK_VETO", {"model": key, "reading": reading.as_dict(), "math": shown})
        stake = (stake * reading.multiplier).quantize(PAISA, rounding=ROUND_DOWN)
    if stake < MIN_STAKE:
        return Rejection(bot.id, "STAKE_TOO_SMALL", {"stake": str(stake), "kelly_stake": str(kelly_stake), "equity": str(ctx.equity)})
    return Proposal(bot.id, conviction, ev, stake, kelly_stake, shown, tuple(risk))


def validate_pipeline(math_models: Sequence[str], risk_models: Sequence[str], bet_types: Sequence[str], registry: Mapping[str, Any]) -> list[str]:
    """Problems with a pipeline, empty when it can run live. Every key must be a seeded component of
    the right kind, and every component must be one a bot can actually run."""
    problems: list[str] = []
    for keys, kind in ((math_models, "MATH_MODEL"), (risk_models, "RISK_MODEL"), (bet_types, "BET_TYPE")):
        if len(set(keys)) != len(keys):
            problems.append(f"{kind}: duplicate components")
        for key in keys:
            row = registry.get(key)
            if row is None:
                problems.append(f"{key}: not in the model registry")
            elif str(row.component_kind) != kind:
                problems.append(f"{key}: is a {row.component_kind}, not a {kind}")
            elif not row.live_capable or key not in LIVE_COMPONENTS:
                problems.append(f"{key}: backtest only (The Core); a bot cannot run it on a live signal")
    if STAKING_MODEL not in math_models:
        problems.append(f"{STAKING_MODEL}: every pipeline ends with the Kelly staking model")
    if not [k for k in math_models if k in MATH_ADAPTERS]:
        problems.append("add at least one probability model (a de-vig method, the consensus or a time-series model)")
    if not [k for k in bet_types if k in SIGNAL_MARKETS.values()]:
        problems.append("target at least one market the live signal stream carries (1X2, over/under goals, Asian handicap)")
    return problems
