"""Bankroll growth mathematics (Group 76): one sizing rule for the fortress and the CFO, and the forecasts behind it.

**Sizing.** For a bet at decimal odds ``o`` with model probability ``p``, full Kelly is ``f* = (b p - q) / b`` with
``b = o - 1``, ``q = 1 - p`` (0 when the edge is not positive). The executable fraction is

    f = min(f* x kappa x psi(BSS) x lambda(o), f_max) x phi(D)

* ``kappa``: the fractional-Kelly multiplier (``CFO_KELLY_FRACTION_DEFAULT``, quarter Kelly);
* ``psi(BSS) = min(cap, max(floor, 1 + slope x BSS))``: the models' Brier skill against the sharp close (Group 74);
  no measured skill is neutral (1), never assumed good;
* ``lambda(o) = max(floor, (pivot / o) ^ exponent)`` above the pivot odds: long shots carry the most model error;
* ``f_max``: the hard ceiling per bet (``CFO_MAX_SINGLE_STAKE_PCT``);
* ``phi(D)``: the drawdown damper, applied AFTER the ceiling, so it bites whatever the edge (applied before it,
  as min(f* kappa phi psi, f_max), a large edge would sail through the cautious regimes still at the cap):

      D < cautious                  1      OPTIMAL_GROWTH
      cautious  <= D < defensive    0.5    CAUTIOUS_THROTTLED
      defensive <= D < halt         0.25   DEFENSIVE_CAPITAL_PRESERVATION
      D >= halt                     0      CIRCUIT_BREAKER_HALT

  A halt latches: it stays until a supervisor signs it off, however the rolling drawdown recovers (LATCHED_HALT).

**Ruin.** In the continuous limit a fraction ``kappa`` of Kelly ever suffers a drawdown of ``D`` with probability
``(1 - D) ^ (2 / kappa - 1)``: full Kelly halves the bankroll with probability 1/2, half Kelly 1/8, quarter Kelly 1/128.

**The forecast.** ``simulate`` bootstraps the user's own settled bets (odds, the model probability the stake was
sized on, the return each actually made per rupee), sizes each draw with the strategy under test and the damper,
and runs ``paths`` equity curves over the horizon at the user's own betting rate. The edge it compounds is the one
the ledger realised, not the one the models claimed.

**Rebalancing.** Venue ``j`` holds ``V_j``; its EV flow ``E_j`` is the expected value its bets captured. The target is
``V_j* = W x E_j^gamma / sum_k E_k^gamma``, and ``transfers`` water-fills surplus into deficit, largest first,
emitting only moves of at least the minimum transfer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

import numpy as np

PAISA = Decimal("0.01")

OPTIMAL = "OPTIMAL_GROWTH"
CAUTIOUS = "CAUTIOUS_THROTTLED"
DEFENSIVE = "DEFENSIVE_CAPITAL_PRESERVATION"
HALT = "CIRCUIT_BREAKER_HALT"
LATCHED = "LATCHED_HALT"  # the drawdown recovered, the halt awaits a supervisor's sign-off


# ================================================================ policy
@dataclass(frozen=True, slots=True)
class Regime:
    name: str
    floor: float  # the drawdown (a fraction) from which it applies
    multiplier: float

    @property
    def halted(self) -> bool:
        return self.multiplier <= 0.0


@dataclass(frozen=True, slots=True)
class SizingPolicy:
    kelly_fraction: float
    max_fraction: float
    regimes: tuple[Regime, ...]  # ascending floors, the first at 0
    skill_slope: float
    skill_floor: float
    skill_cap: float
    longshot_pivot: float
    longshot_exponent: float
    longshot_floor: float

    @classmethod
    def from_settings(cls, settings: Any) -> SizingPolicy:
        cautious, defensive, halt = (settings.CFO_DRAWDOWN_CAUTIOUS_THRESHOLD_PCT / 100.0, settings.CFO_DRAWDOWN_DEFENSIVE_THRESHOLD_PCT / 100.0,
                                     settings.CFO_DRAWDOWN_HALT_THRESHOLD_PCT / 100.0)
        if not 0.0 < cautious < defensive < halt < 1.0:
            raise ValueError("CFO_DRAWDOWN_*_THRESHOLD_PCT: 0 < cautious < defensive < halt < 100")
        return cls(
            kelly_fraction=float(settings.CFO_KELLY_FRACTION_DEFAULT), max_fraction=float(settings.CFO_MAX_SINGLE_STAKE_PCT),
            regimes=(Regime(OPTIMAL, 0.0, 1.0), Regime(CAUTIOUS, cautious, float(settings.CFO_DRAWDOWN_CAUTIOUS_MULTIPLIER)),
                     Regime(DEFENSIVE, defensive, float(settings.CFO_DRAWDOWN_DEFENSIVE_MULTIPLIER)), Regime(HALT, halt, 0.0)),
            skill_slope=float(settings.CFO_SKILL_SLOPE), skill_floor=float(settings.CFO_SKILL_FLOOR), skill_cap=float(settings.CFO_SKILL_CAP),
            longshot_pivot=float(settings.CFO_LONGSHOT_PIVOT_ODDS), longshot_exponent=float(settings.CFO_LONGSHOT_EXPONENT),
            longshot_floor=float(settings.CFO_LONGSHOT_FLOOR),
        )

    @property
    def halt_at(self) -> float:
        return self.regimes[-1].floor

    def as_dict(self) -> dict[str, Any]:
        return {"kelly_fraction": self.kelly_fraction, "max_fraction": self.max_fraction,
                "regimes": [{"name": r.name, "from_drawdown": r.floor, "multiplier": r.multiplier} for r in self.regimes],
                "skill": {"slope": self.skill_slope, "floor": self.skill_floor, "cap": self.skill_cap},
                "longshot": {"pivot_odds": self.longshot_pivot, "exponent": self.longshot_exponent, "floor": self.longshot_floor}}


# ================================================================ the pieces
def kelly_fraction(odds: float, probability: float) -> float:
    """Full Kelly ``(b p - q) / b``; 0 without a positive edge or with nonsense inputs."""
    if not (math.isfinite(odds) and math.isfinite(probability)) or odds <= 1.0 or not 0.0 < probability < 1.0:
        return 0.0
    b = odds - 1.0
    return max(0.0, (b * probability - (1.0 - probability)) / b)


def damper(drawdown: float, policy: SizingPolicy, *, latched: bool = False) -> Regime:
    """phi(D): the regime a drawdown (a fraction) puts sizing in; a latched halt holds at 0."""
    if latched:
        return Regime(LATCHED, policy.halt_at, 0.0)
    regime = policy.regimes[0]
    for r in policy.regimes:
        if drawdown >= r.floor - 1e-12:
            regime = r
    return regime


def skill_multiplier(bss: float | None, policy: SizingPolicy) -> float:
    """psi(BSS); 1 when no skill has been measured."""
    if bss is None or not math.isfinite(bss):
        return 1.0
    return min(policy.skill_cap, max(policy.skill_floor, 1.0 + policy.skill_slope * bss))


def longshot_discount(odds: float, policy: SizingPolicy) -> float:
    """lambda(o): 1 up to the pivot odds, then a power-law cut to the floor."""
    if odds <= policy.longshot_pivot:
        return 1.0
    return max(policy.longshot_floor, (policy.longshot_pivot / odds) ** policy.longshot_exponent)


def ruin_probability(kappa: float, drawdown: float) -> float:
    """P(ever a drawdown of ``drawdown``) at ``kappa`` x Kelly, in the continuous limit: (1 - D)^(2/kappa - 1)."""
    if not 0.0 < kappa <= 1.0 or not 0.0 < drawdown < 1.0:
        raise ValueError("kappa in (0, 1], drawdown in (0, 1)")
    return (1.0 - drawdown) ** (2.0 / kappa - 1.0)


@dataclass(frozen=True, slots=True)
class Sizing:
    """One bet's executable fraction, and how it was reached."""

    fraction: float  # of bankroll, after every factor, the ceiling and the damper
    stake: Decimal
    halted: bool
    scaled: bool  # the damper cut it
    full_kelly: float
    regime: str = OPTIMAL
    damper: float = 1.0
    skill: float = 1.0
    longshot: float = 1.0
    capped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"full_kelly": round(self.full_kelly, 5), "fraction": round(self.fraction, 5), "stake_inr": str(self.stake), "regime": self.regime,
                "damper": self.damper, "skill_multiplier": round(self.skill, 4), "longshot_discount": round(self.longshot, 4), "capped": self.capped,
                "scaled": self.scaled, "halted": self.halted}


def round_stake(bankroll: Decimal, fraction: float, step: Decimal) -> Decimal:
    raw = bankroll * Decimal(str(fraction))
    return ((raw / step).to_integral_value(rounding=ROUND_DOWN) * step).quantize(PAISA) if step > 0 else raw.quantize(PAISA, rounding=ROUND_DOWN)


def size(full_kelly: float, odds: float, bankroll: Decimal, drawdown: float, policy: SizingPolicy, step: Decimal, *, bss: float | None = None,
         latched: bool = False, kappa: float | None = None) -> Sizing:
    """The fraction and the stake (rounded down to ``step``) for one bet."""
    regime = damper(drawdown, policy, latched=latched)
    psi, lam = skill_multiplier(bss, policy), longshot_discount(odds, policy)
    raw = max(full_kelly, 0.0) * (policy.kelly_fraction if kappa is None else kappa) * psi * lam
    capped = raw > policy.max_fraction
    fraction = min(raw, policy.max_fraction) * regime.multiplier
    stake = round_stake(bankroll, fraction, step) if fraction > 0 else Decimal(0).quantize(PAISA)
    return Sizing(fraction, stake, regime.halted, regime.multiplier < 1.0, full_kelly, regime.name, regime.multiplier, psi, lam, capped)


# ================================================================ strategies
@dataclass(frozen=True, slots=True)
class Strategy:
    code: str
    label: str
    kelly: float | None = None  # a Kelly multiplier ...
    fixed: float | None = None  # ... or a fixed fraction of bankroll per bet

    def fractions(self, full_kelly: np.ndarray, odds: np.ndarray, policy: SizingPolicy, bss: float | None) -> np.ndarray:
        """Each bet's fraction before the damper: the strategy's own sizing under the fleet's ceiling."""
        if self.fixed is not None:
            raw = np.full(full_kelly.shape, self.fixed)
        else:
            lam = np.array([longshot_discount(float(o), policy) for o in odds])
            raw = np.maximum(full_kelly, 0.0) * float(self.kelly) * skill_multiplier(bss, policy) * lam  # type: ignore[arg-type]
        return np.minimum(raw, policy.max_fraction)


def strategies(policy: SizingPolicy) -> list[Strategy]:
    """The comparison set; the configured Kelly multiplier is the active one (its own row when it is none of these)."""
    named = [Strategy("FULL_KELLY", "Full Kelly", kelly=1.0), Strategy("HALF_KELLY", "Half Kelly", kelly=0.5), Strategy("QUARTER_KELLY", "Quarter Kelly", kelly=0.25),
             Strategy("FIXED_FRACTION_1PCT", "Fixed 1% of bankroll", fixed=0.01), Strategy("FIXED_FRACTION_2PCT", "Fixed 2% of bankroll", fixed=0.02)]
    if all(s.kelly is None or abs(s.kelly - policy.kelly_fraction) > 1e-9 for s in named):
        named.append(Strategy("CONFIGURED_KELLY", f"Configured Kelly ({policy.kelly_fraction:g}x)", kelly=policy.kelly_fraction))
    return named


def active_code(policy: SizingPolicy, rows: Sequence[Strategy]) -> str:
    return next((s.code for s in rows if s.kelly is not None and abs(s.kelly - policy.kelly_fraction) <= 1e-9), "CONFIGURED_KELLY")


# ================================================================ the forecast
@dataclass(frozen=True, slots=True)
class History:
    """The settled bets a forecast bootstraps."""

    odds: np.ndarray
    probability: np.ndarray  # the model probability the stake was sized on
    unit_return: np.ndarray  # P&L per rupee staked, as settled
    per_day: float  # the user's betting rate

    @property
    def full_kelly(self) -> np.ndarray:
        return np.array([kelly_fraction(float(o), float(p)) for o, p in zip(self.odds, self.probability, strict=True)])

    def as_dict(self) -> dict[str, Any]:
        return {"bets": int(self.odds.size), "per_day": round(self.per_day, 4), "hit_rate": round(float(np.mean(self.unit_return > 0)), 4),
                "mean_odds": round(float(np.mean(self.odds)), 4), "mean_model_probability": round(float(np.mean(self.probability)), 4),
                "realised_roi": round(float(np.mean(self.unit_return)), 6), "median_full_kelly": round(float(np.median(self.full_kelly)), 6)}


@dataclass(frozen=True, slots=True)
class Forecast:
    horizon_days: int
    paths: int
    trades: int
    start: float
    curve: list[dict[str, float]]  # per checkpoint day: p1, p5, p25, p50, p75, p95, p99
    median_end: float
    mean_end: float
    cagr: float
    sharpe: float | None
    sortino: float | None
    prob_halt: float  # P(the drawdown reaches the halt line): the circuit breaker trips
    prob_ruin: float  # P(the bankroll falls below ruin_level x start)
    median_max_drawdown: float
    p95_max_drawdown: float
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return {"horizon_days": self.horizon_days, "paths": self.paths, "trades": self.trades, "start": round(self.start, 2), "curve": self.curve,
                "median_end": round(self.median_end, 2), "mean_end": round(self.mean_end, 2), "cagr": round(self.cagr, 6),
                "sharpe": None if self.sharpe is None else round(self.sharpe, 4), "sortino": None if self.sortino is None else round(self.sortino, 4),
                "prob_halt": round(self.prob_halt, 6), "prob_ruin": round(self.prob_ruin, 6), "median_max_drawdown": round(self.median_max_drawdown, 6),
                "p95_max_drawdown": round(self.p95_max_drawdown, 6), "seed": self.seed}


PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


def checkpoints(horizon: int, points: int) -> list[int]:
    step = max(1, horizon // max(1, points))
    return sorted({*range(0, horizon + 1, step), horizon})


def _ratio(returns: np.ndarray, rf_daily: float, downside: bool) -> float | None:
    excess = returns - rf_daily
    spread = float(np.sqrt(np.mean(np.minimum(excess, 0.0) ** 2))) if downside else float(np.std(returns))
    return None if spread <= 1e-15 else float(np.mean(excess)) / spread * math.sqrt(365.0)


def simulate(history: History, strategy: Strategy, policy: SizingPolicy, *, start: float, horizon_days: int, paths: int, seed: int,
             bss: float | None = None, rf_annual: float = 0.0, ruin_level: float = 0.5, curve_points: int = 15) -> Forecast:
    """``paths`` equity curves over ``horizon_days``: each bet drawn from the history, sized by the strategy, the
    ceiling and the damper on the path's own drawdown; a path that reaches the halt line stays halted."""
    if history.odds.size == 0 or history.per_day <= 0:
        raise ValueError("no history to bootstrap")
    trades = int(math.floor(history.per_day * horizon_days + 1e-9))
    if trades < 1:
        raise ValueError("the betting rate gives no bet inside the horizon")
    rng = np.random.default_rng(seed)
    fractions = strategy.fractions(history.full_kelly, history.odds, policy, bss)
    floors = np.array([r.floor for r in policy.regimes])
    multipliers = np.array([r.multiplier for r in policy.regimes])
    wealth = np.full(paths, float(start))
    peak = wealth.copy()
    worst = np.zeros(paths)
    lowest = wealth.copy()
    halted = np.zeros(paths, dtype=bool)
    daily = np.empty((horizon_days + 1, paths))
    daily[0] = wealth
    done = 0
    for day in range(1, horizon_days + 1):
        due = min(trades, int(math.floor(day * history.per_day + 1e-9)))
        while done < due:
            pick = rng.integers(0, history.odds.size, paths)
            drawdown = np.where(peak > 0, (peak - wealth) / peak, 0.0)
            halted |= drawdown >= policy.halt_at - 1e-12
            phi = multipliers[np.searchsorted(floors, drawdown + 1e-12, side="right") - 1]
            phi[halted] = 0.0
            wealth = np.maximum(wealth * (1.0 + fractions[pick] * phi * history.unit_return[pick]), 0.0)
            peak = np.maximum(peak, wealth)
            worst = np.maximum(worst, np.where(peak > 0, (peak - wealth) / peak, 0.0))
            lowest = np.minimum(lowest, wealth)
            done += 1
        daily[day] = wealth
    halted |= worst >= policy.halt_at - 1e-12
    curve = [{"day": d, **{f"p{q}": round(float(v), 2) for q, v in zip(PERCENTILES, np.percentile(daily[d], PERCENTILES), strict=True)}}
             for d in checkpoints(horizon_days, curve_points)]
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.where(daily[:-1] > 0, daily[1:] / daily[:-1] - 1.0, 0.0).ravel()
    rf_daily = rf_annual / 365.0
    median_end = float(np.median(wealth))
    return Forecast(
        horizon_days, paths, trades, float(start), curve, median_end, float(np.mean(wealth)),
        (median_end / start) ** (365.0 / horizon_days) - 1.0 if start > 0 and median_end > 0 else -1.0,
        _ratio(returns, rf_daily, False), _ratio(returns, rf_daily, True),
        float(np.mean(halted)), float(np.mean(lowest < ruin_level * start)), float(np.median(worst)), float(np.percentile(worst, 95)), seed,
    )


# ================================================================ rebalancing
@dataclass(frozen=True, slots=True)
class Transfer:
    source: str
    destination: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    total: Decimal
    balances: dict[str, Decimal]
    targets: dict[str, Decimal]
    weights: dict[str, float]
    transfers: list[Transfer] = field(default_factory=list)


def targets(balances: Mapping[str, Decimal], ev_flow: Mapping[str, float], gamma: float) -> tuple[dict[str, Decimal], dict[str, float]] | None:
    """V_j* = W E_j^gamma / sum E_k^gamma over the venues holding money; None when none of them captured any EV."""
    total = sum(balances.values(), Decimal(0))
    scores = {v: max(float(ev_flow.get(v, 0.0)), 0.0) ** gamma for v in balances}
    norm = sum(scores.values())
    if norm <= 0:
        return None
    weights = {v: s / norm for v, s in scores.items()}
    out = {v: (total * Decimal(str(w))).quantize(PAISA, rounding=ROUND_DOWN) for v, w in weights.items()}
    return out, weights


def transfers(balances: Mapping[str, Decimal], target: Mapping[str, Decimal], min_transfer: Decimal) -> list[Transfer]:
    """Water-fill: the largest surplus into the largest deficit, moves of at least ``min_transfer`` only."""
    surplus = sorted(((v, balances[v] - target.get(v, Decimal(0))) for v in balances if balances[v] - target.get(v, Decimal(0)) >= min_transfer), key=lambda x: (-x[1], x[0]))
    deficit = sorted(((v, target[v] - balances.get(v, Decimal(0))) for v in target if target[v] - balances.get(v, Decimal(0)) >= min_transfer), key=lambda x: (-x[1], x[0]))
    s = [[v, a] for v, a in surplus]
    d = [[v, a] for v, a in deficit]
    out: list[Transfer] = []
    i = j = 0
    while i < len(s) and j < len(d):
        amount = min(s[i][1], d[j][1])
        if amount >= min_transfer:
            out.append(Transfer(s[i][0], d[j][0], amount.quantize(PAISA, rounding=ROUND_DOWN)))
        s[i][1] -= amount
        d[j][1] -= amount
        if s[i][1] < min_transfer:
            i += 1
        if d[j][1] < min_transfer:
            j += 1
    return out


def plan(balances: Mapping[str, Decimal], ev_flow: Mapping[str, float], gamma: float, min_transfer: Decimal) -> RebalancePlan | None:
    found = targets(balances, ev_flow, gamma)
    if found is None:
        return None
    target, weights = found
    return RebalancePlan(sum(balances.values(), Decimal(0)), dict(balances), target, weights, transfers(balances, target, min_transfer))
