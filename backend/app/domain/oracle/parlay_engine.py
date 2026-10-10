"""Ashoka's scenario engine: from priced legs to vetted slips (Group 69).

1. Legs. Every candidate leg carries its bookmakers' quotes and, from ``match_model``, what each model
   says about it: the market consensus (de-vigged across books), Poisson, Dixon-Coles, xG and Elo when
   their inputs exist. The ensemble is the average of the models' outcome distributions.
2. The anti-correlation gate. A standard multiple may not hold two legs of the same fixture (the
   bookmakers' own "related contingency" rule: Parimatch and 1xBet reject them at checkout), nor the same
   team twice inside a day and a half (one fixture under two ids). Same-fixture pairs are reported with
   their correlation from the joint scoreline distribution.
3. The scenario engine. ``ASHOKA_MC_PATHS`` (10,000) paths per slip. On each path every fixture draws
   one model (model uncertainty) and each leg a settlement result from that model; the slip's return is
   the product of its legs' payout factors (systems: the mean over their lines). The paths give the true
   joint probability (the chance the slip returns more than its stake), the joint EV, the variance, the
   5% tail, and the growth-optimal stake. Independent legs are cross-checked against the exact product.
4. The "1000%" confidence gate. VETTED needs joint EV >= ``ASHOKA_MIN_JOINT_EV`` (+7.5%), true joint
   probability >= ``ASHOKA_MIN_JOINT_PROBABILITY`` (55%; the brief names doubles and trebles, and singles
   are held to it too: a "1000% vetted" 17% longshot would mislead), no correlation clash, no model that
   thinks a leg loses money, fresh prices and a real consensus. A slip
   that clears the EV, independence and freshness bars but not the probability one is VALUE: offered
   only with a strictly bounded Kelly stake. Everything else is REJECTED. "1000% vetted" names how
   strict the filter is; the numbers on the slip are what it is worth: no slip is a certainty.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np
from scipy.optimize import minimize_scalar

from app.domain.bookmakers.adapters import EXCHANGE_BOOKMAKERS
from app.domain.oracle.markets import LegResult, MarketKind, MarketRef
from app.domain.oracle.match_model import RESULTS, FixtureModels, _result_grid, expected_factor, leg_correlation, win_probability

_RESULT_FACTOR_INDEX = {r: i for i, r in enumerate(RESULTS)}


class SlipKind(StrEnum):
    SINGLE = "SINGLE"
    DOUBLE = "DOUBLE"
    TREBLE = "TREBLE"
    ACCUMULATOR = "ACCUMULATOR"  # four or more legs, one line
    TRIXIE = "TRIXIE"  # 3 legs: 3 doubles + 1 treble
    YANKEE = "YANKEE"  # 4 legs: 6 doubles, 4 trebles, 1 fourfold
    CANADIAN = "CANADIAN"  # 5 legs: 26 lines
    HEINZ = "HEINZ"  # 6 legs: 57 lines
    PATENT = "PATENT"  # 3 legs: 3 singles, 3 doubles, 1 treble (Group 77)
    SUPER_HEINZ = "SUPER_HEINZ"  # 7 legs: 120 lines
    GOLIATH = "GOLIATH"  # 8 legs: 247 lines


SYSTEMS: dict[SlipKind, tuple[int, tuple[int, ...]]] = {
    SlipKind.TRIXIE: (3, (2, 3)),
    SlipKind.YANKEE: (4, (2, 3, 4)),
    SlipKind.CANADIAN: (5, (2, 3, 4, 5)),
    SlipKind.HEINZ: (6, (2, 3, 4, 5, 6)),
    SlipKind.PATENT: (3, (1, 2, 3)),
    SlipKind.SUPER_HEINZ: (7, (2, 3, 4, 5, 6, 7)),
    SlipKind.GOLIATH: (8, (2, 3, 4, 5, 6, 7, 8)),
}
AUTO_SYSTEMS = (SlipKind.TRIXIE, SlipKind.YANKEE, SlipKind.CANADIAN, SlipKind.HEINZ)  # what Ashoka's own scan builds; the rest are the user's to choose


class Tier(StrEnum):
    VETTED = "VETTED"
    VALUE = "VALUE"
    REJECTED = "REJECTED"


def kind_for(n_legs: int) -> SlipKind:
    return {1: SlipKind.SINGLE, 2: SlipKind.DOUBLE, 3: SlipKind.TREBLE}.get(n_legs, SlipKind.ACCUMULATOR)


def lines_of(kind: SlipKind, n_legs: int) -> list[tuple[int, ...]]:
    """The leg-index combinations a structure stakes (one for a straight multiple)."""
    if kind in SYSTEMS:
        required, folds = SYSTEMS[kind]
        if n_legs != required:
            raise ValueError(f"{kind} needs exactly {required} legs, got {n_legs}")
        return [combo for k in folds for combo in itertools.combinations(range(n_legs), k)]
    return [tuple(range(n_legs))]


# ================================================================ legs
@dataclass(frozen=True, slots=True)
class Quote:
    bookmaker: str  # canonical where known (parimatch, 1xbet, stake, pinnacle, betfair), else the feed's key
    odds: float
    observed_at: datetime
    commission: float = 0.0  # on net winnings (an exchange)

    @property
    def net_odds(self) -> float:
        return (self.odds - 1.0) * (1.0 - self.commission) + 1.0

    def age(self, now: datetime) -> float:
        seen = self.observed_at if self.observed_at.tzinfo else self.observed_at.replace(tzinfo=UTC)
        return max(0.0, (now - seen).total_seconds())


def _market_distribution(consensus: float, shape: Mapping[LegResult, float] | None) -> dict[LegResult, float]:
    """The market's view of a leg: its de-vigged win share, with the push structure (voids, halves)
    of the line taken from the scoreline models (a price says nothing about how often a line pushes)."""
    if shape is None:
        return {LegResult.WON: consensus, LegResult.HALF_WON: 0.0, LegResult.VOID: 0.0, LegResult.HALF_LOST: 0.0, LegResult.LOST: 1.0 - consensus}
    void = shape.get(LegResult.VOID, 0.0)
    pos = shape.get(LegResult.WON, 0.0) + shape.get(LegResult.HALF_WON, 0.0)
    neg = shape.get(LegResult.HALF_LOST, 0.0) + shape.get(LegResult.LOST, 0.0)
    decisive = 1.0 - void
    out = {LegResult.VOID: void}
    for result, side, share in ((LegResult.WON, pos, consensus), (LegResult.HALF_WON, pos, consensus), (LegResult.HALF_LOST, neg, 1 - consensus), (LegResult.LOST, neg, 1 - consensus)):
        within = shape.get(result, 0.0) / side if side > 0 else (1.0 if result in (LegResult.WON, LegResult.LOST) else 0.0)
        out[result] = decisive * share * within
    return out


@dataclass(slots=True)
class LegCandidate:
    fixture_id: str
    home: str
    away: str
    market: MarketRef
    selection: str
    quotes: dict[str, Quote]
    consensus: float | None = None  # the market's de-vigged probability for this selection (two-way share)
    consensus_books: int = 0
    models: FixtureModels | None = None
    sport_key: str | None = None
    league: str | None = None
    kickoff: datetime | None = None

    @property
    def leg_id(self) -> str:
        return f"{self.fixture_id}|{self.market.key}|{self.selection}"

    def distributions(self) -> dict[str, dict[LegResult, float]]:
        """Each available model's outcome distribution for this leg."""
        out: dict[str, dict[LegResult, float]] = {}
        shape = None
        if self.models is not None:
            for name in self.models.names:
                dist = self.models.distribution(name, self.market, self.selection)
                if dist is not None:
                    out[name] = dist
            if self.models.matrices:
                shape = self.models.distribution(next(iter(self.models.matrices)), self.market, self.selection)
        if self.consensus is not None:
            out["market"] = _market_distribution(self.consensus, shape)
        return out

    def ensemble(self) -> dict[LegResult, float]:
        dists = self.distributions()
        if not dists:
            raise ValueError(f"no model prices {self.leg_id}")
        return {r: sum(d.get(r, 0.0) for d in dists.values()) / len(dists) for r in RESULTS}

    @property
    def probability(self) -> float:
        return win_probability(self.ensemble())

    def ev(self, odds: float) -> float:
        return expected_factor(self.ensemble(), odds) - 1.0

    def model_evs(self, odds: float) -> dict[str, float]:
        return {name: expected_factor(dist, odds) - 1.0 for name, dist in self.distributions().items()}

    def best_quote(self, now: datetime, max_age: float, books: Iterable[str] | None = None, *, multiples: bool = False) -> Quote | None:
        allowed = set(books) if books is not None else None
        fresh = [
            q for q in self.quotes.values()
            if q.age(now) <= max_age and (allowed is None or q.bookmaker in allowed) and not (multiples and q.bookmaker in EXCHANGE_BOOKMAKERS)
        ]
        return max(fresh, key=lambda q: q.net_odds, default=None)


# ================================================================ the anti-correlation gate
@dataclass(frozen=True, slots=True)
class Clash:
    a: str
    b: str
    reason: str  # SAME_FIXTURE | SAME_TEAM | NEGATIVE_CORRELATION
    correlation: float | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    clashes: tuple[Clash, ...] = ()


def _team(name: str) -> str:
    return " ".join("".join(ch for ch in name.casefold() if ch.isalnum() or ch.isspace()).split())


class AntiCorrelationGate:
    """Legs of one standard multiple must be independent events."""

    def __init__(self, same_team_window: timedelta = timedelta(hours=36)) -> None:
        self.window = same_team_window

    def check(self, legs: Sequence[LegCandidate]) -> GateResult:
        clashes: list[Clash] = []
        for a, b in itertools.combinations(legs, 2):
            if a.fixture_id == b.fixture_id:
                corr = None
                if a.models is not None and a.models.matrices:
                    corr = leg_correlation(a.models.mixture(), (a.market, a.selection), (b.market, b.selection))
                clashes.append(Clash(a.leg_id, b.leg_id, "SAME_FIXTURE", None if corr is None else round(corr, 4)))
                if corr is not None and corr < -1e-6:
                    clashes.append(Clash(a.leg_id, b.leg_id, "NEGATIVE_CORRELATION", round(corr, 4)))
                continue
            teams_a, teams_b = {_team(a.home), _team(a.away)}, {_team(b.home), _team(b.away)}
            if teams_a & teams_b:
                close = a.kickoff is None or b.kickoff is None or abs(a.kickoff - b.kickoff) <= self.window
                if close:
                    clashes.append(Clash(a.leg_id, b.leg_id, "SAME_TEAM"))
        return GateResult(not clashes, tuple(clashes))


# ================================================================ the scenario engine
@dataclass(frozen=True, slots=True)
class SimulationResult:
    paths: int
    joint_probability: float  # P(the slip returns more than its stake)
    joint_probability_se: float
    full_win_probability: float  # every leg (fully) won
    probability_band: tuple[float, float]  # 5th-95th percentile across the models' disagreement
    joint_ev: float  # mean return per unit staked, minus one
    joint_ev_se: float
    analytic_ev: float | None  # exact, when every leg is on its own fixture
    std_return: float
    p_total_loss: float
    var_95: float  # the 5th percentile of profit per unit staked
    cvar_95: float  # the mean of the worst 5%
    kelly_fraction: float  # growth-optimal share of bankroll on this slip (full Kelly)


class ScenarioEngine:
    def __init__(self, paths: int = 10_000, seed: int | None = None) -> None:
        if paths < 100:
            raise ValueError("at least 100 paths")
        self.paths = paths
        self.seed = seed

    def _rng(self, legs: Sequence[LegCandidate], odds: Sequence[float]) -> np.random.Generator:
        """Deterministic per slip (the same slip always simulates the same), unless a seed is given."""
        if self.seed is not None:
            return np.random.default_rng(self.seed)
        digest = hashlib.blake2b("|".join(f"{leg.leg_id}@{o:.4f}" for leg, o in zip(legs, odds, strict=True)).encode(), digest_size=8).digest()
        return np.random.default_rng(int.from_bytes(digest, "big"))

    def simulate(self, legs: Sequence[LegCandidate], odds: Sequence[float], kind: SlipKind | None = None) -> SimulationResult:
        if not legs or len(legs) != len(odds):
            raise ValueError("one price per leg")
        kind = kind or kind_for(len(legs))
        rng = self._rng(legs, odds)
        n, paths = len(legs), self.paths
        factors = np.empty((paths, n))
        win_prob = np.empty((paths, n))  # the drawn model's win chance per leg, for the probability band
        by_fixture: dict[str, list[int]] = {}
        for i, leg in enumerate(legs):
            by_fixture.setdefault(leg.fixture_id, []).append(i)
        for indices in by_fixture.values():
            first = legs[indices[0]]
            if len(indices) == 1:
                dists = list(first.distributions().values())
                if not dists:
                    raise ValueError(f"no model prices {first.leg_id}")
                probs = np.array([[d.get(r, 0.0) for r in RESULTS] for d in dists])
                probs = probs / probs.sum(axis=1, keepdims=True)
                pick = rng.integers(0, len(dists), size=paths)
                cum = np.cumsum(probs, axis=1)[pick]
                result = (rng.random(paths)[:, None] > cum).sum(axis=1).clip(0, len(RESULTS) - 1)
                i = indices[0]
                factors[:, i] = self._factor_table(odds[i])[result]
                win_prob[:, i] = (probs[:, 0] + probs[:, 1])[pick]
                continue
            # Several legs on one fixture: one scoreline per path, so the legs move together
            models = first.models
            if models is None or not models.matrices:
                raise ValueError(f"same-fixture legs on {first.fixture_id} need a scoreline model")
            matrices = list(models.matrices.values())
            pick = rng.integers(0, len(matrices), size=paths)
            flat = np.stack([m.ravel() / m.sum() for m in matrices])
            cum = np.cumsum(flat, axis=1)[pick]
            cells = (rng.random(paths)[:, None] > cum).sum(axis=1).clip(0, flat.shape[1] - 1)
            for i in indices:
                grid = _result_grid(legs[i].market.kind, legs[i].market.line, legs[i].selection).ravel()
                factors[:, i] = self._factor_table(odds[i])[grid[cells]]
                per_model = np.array([flat[k][(grid == 0) | (grid == 1)].sum() for k in range(len(matrices))])
                win_prob[:, i] = per_model[pick]
        lines = lines_of(kind, n)
        returns = np.mean([np.prod(factors[:, list(line)], axis=1) for line in lines], axis=0)
        profit = returns - 1.0
        joint = float(np.mean(profit > 1e-12))
        tail_cut = np.quantile(profit, 0.05)
        conditional = np.prod(win_prob, axis=1) if kind not in SYSTEMS else None
        band = (float(np.quantile(conditional, 0.05)), float(np.quantile(conditional, 0.95))) if conditional is not None else (joint, joint)
        analytic = None
        if len(by_fixture) == n:  # independent legs: the exact expectation
            expected = [expected_factor(leg.ensemble(), o) for leg, o in zip(legs, odds, strict=True)]
            analytic = float(np.mean([np.prod([expected[j] for j in line]) for line in lines]) - 1.0)
        return SimulationResult(
            paths=paths,
            joint_probability=joint,
            joint_probability_se=math.sqrt(joint * (1 - joint) / paths),
            full_win_probability=float(np.mean(np.all(factors > np.asarray(odds) - 1e-9, axis=1))),
            probability_band=band,
            joint_ev=float(profit.mean()),
            joint_ev_se=float(profit.std(ddof=1) / math.sqrt(paths)),
            analytic_ev=analytic,
            std_return=float(returns.std(ddof=1)),
            p_total_loss=float(np.mean(returns < 1e-12)),
            var_95=float(tail_cut),
            cvar_95=float(profit[profit <= tail_cut].mean()),
            kelly_fraction=self._kelly(profit),
        )

    @staticmethod
    def _factor_table(odds: float) -> np.ndarray:
        return np.array([odds, (odds + 1.0) / 2.0, 1.0, 0.5, 0.0])

    @staticmethod
    def _kelly(profit: np.ndarray) -> float:
        """argmax_f E[log(1 + f * profit)]: the growth-optimal fraction (0 when the slip loses money)."""
        if profit.mean() <= 0:
            return 0.0
        # f < 1: a slip that can lose its whole stake makes log(1 - f) the binding term
        result = minimize_scalar(lambda f: -float(np.mean(np.log1p(f * profit))), bounds=(0.0, 0.999), method="bounded", options={"xatol": 1e-5})
        return float(max(result.x, 0.0)) if result.success else 0.0


# ================================================================ the "1000%" confidence gate
@dataclass(frozen=True, slots=True)
class GateThresholds:
    min_joint_ev: float = 0.075
    min_joint_probability: float = 0.55
    max_quote_age_seconds: float = 180.0
    min_books: int = 2


@dataclass(frozen=True, slots=True)
class Verdict:
    tier: Tier
    checks: dict[str, bool]
    reasons: tuple[str, ...]


CHECK_LABELS = {
    "independent": "correlated legs",
    "joint_ev": "joint EV under the bar",
    "joint_probability": "probability under the bar",
    "models_agree": "a model dissents",
    "fresh_odds": "stale odds",
    "consensus": "thin consensus",
}


def judge(legs: Sequence[LegCandidate], quotes: Sequence[Quote], sim: SimulationResult, gate: GateResult, thresholds: GateThresholds, now: datetime, kind: SlipKind) -> Verdict:  # noqa: ARG001
    dissent = [f"{leg.leg_id}: {name} {ev:+.1%}" for leg, q in zip(legs, quotes, strict=True) for name, ev in leg.model_evs(q.net_odds).items() if ev <= 0]
    thin = [leg.leg_id for leg in legs if leg.consensus is None or leg.consensus_books < thresholds.min_books]
    stale = [q.bookmaker for q in quotes if q.age(now) > thresholds.max_quote_age_seconds]
    checks = {
        "independent": gate.ok,
        "joint_ev": sim.joint_ev >= thresholds.min_joint_ev,
        "joint_probability": sim.joint_probability >= thresholds.min_joint_probability,
        "models_agree": not dissent,
        "fresh_odds": not stale,
        "consensus": not thin,
    }
    reasons: list[str] = []
    if not gate.ok:
        reasons += [f"{c.reason}: {c.a} x {c.b}" + (f" (corr {c.correlation:+.2f})" if c.correlation is not None else "") for c in gate.clashes]
    if not checks["joint_ev"]:
        reasons.append(f"joint EV {sim.joint_ev:+.2%} is under {thresholds.min_joint_ev:+.1%}")
    if not checks["joint_probability"]:
        reasons.append(f"true joint probability {sim.joint_probability:.1%} is under {thresholds.min_joint_probability:.0%}")
    if dissent:
        reasons.append("a model prices a leg below its odds: " + "; ".join(dissent[:4]))
    if stale:
        reasons.append(f"prices older than {thresholds.max_quote_age_seconds:.0f}s on {', '.join(sorted(set(stale)))}")
    if thin:
        reasons.append(f"no consensus from {thresholds.min_books}+ books on {', '.join(thin[:3])}")
    if all(checks.values()):
        tier = Tier.VETTED
    elif checks["independent"] and checks["joint_ev"] and checks["fresh_odds"] and checks["consensus"]:
        tier = Tier.VALUE
    else:
        tier = Tier.REJECTED
    return Verdict(tier, checks, tuple(reasons))


# ================================================================ the generator
@dataclass(slots=True)
class SlipCandidate:
    slip_id: str
    kind: SlipKind
    legs: list[LegCandidate]
    book: str  # where the gate priced it: the best book quoting every leg
    quotes: list[Quote]  # that book's quote per leg
    sim: SimulationResult
    verdict: Verdict
    stake_fraction: float  # of bankroll, after the fractional-Kelly scaling and the tier's cap
    cross_league: bool = False
    leagues: tuple[str, ...] = ()

    @property
    def odds(self) -> float:
        """Straight multiples: the product of the legs' prices. Systems: the average line's."""
        prices = [q.odds for q in self.quotes]
        return float(np.mean([math.prod(prices[j] for j in line) for line in lines_of(self.kind, len(prices))]))


@dataclass(slots=True)
class GenerationReport:
    slips: list[SlipCandidate] = field(default_factory=list)
    scanned: int = 0
    simulated: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    legs_considered: int = 0


def slip_id(kind: SlipKind, legs: Sequence[LegCandidate]) -> str:
    return hashlib.blake2b(f"{kind}|{'|'.join(sorted(leg.leg_id for leg in legs))}".encode(), digest_size=10).hexdigest()


def common_book(legs: Sequence[LegCandidate], now: datetime, max_age: float, priority: Sequence[str], *, multiples: bool) -> tuple[str, list[Quote]] | None:
    """The bookmaker paying the most for the whole slip among those quoting every leg (ties go to the
    priority order: Parimatch, then 1xBet ...)."""
    best: tuple[float, int, str, list[Quote]] | None = None
    books = set.intersection(*(set(leg.quotes) for leg in legs)) if legs else set()
    for book in books:
        if multiples and book in EXCHANGE_BOOKMAKERS:
            continue
        quotes = [leg.quotes[book] for leg in legs]
        if any(q.age(now) > max_age for q in quotes):
            continue
        value = math.prod(q.net_odds for q in quotes)
        rank = priority.index(book) if book in priority else len(priority)
        if best is None or value > best[0] + 1e-12 or (abs(value - best[0]) <= 1e-12 and rank < best[1]):
            best = (value, rank, book, quotes)
    return None if best is None else (best[2], best[3])


class ParlayEngine:
    def __init__(
        self,
        thresholds: GateThresholds,
        *,
        paths: int = 10_000,
        kelly_fraction: float = 0.25,
        max_stake_pct: float = 0.02,
        value_max_stake_pct: float = 0.005,
        max_legs: int = 12,
        max_slips: int = 12,
        priority: Sequence[str] = ("parimatch", "1xbet", "stake", "pinnacle", "betfair"),
        seed: int | None = None,
    ) -> None:
        self.thresholds = thresholds
        self.engine = ScenarioEngine(paths, seed)
        self.gate = AntiCorrelationGate()
        self.kelly_fraction, self.max_stake_pct, self.value_max_stake_pct = kelly_fraction, max_stake_pct, value_max_stake_pct
        self.max_legs, self.max_slips, self.priority = max_legs, max_slips, tuple(priority)

    def evaluate(self, legs: Sequence[LegCandidate], kind: SlipKind | None, now: datetime) -> SlipCandidate | None:
        """One slip, gated and simulated, priced at the best book quoting all of it (None: no book does)."""
        kind = kind or kind_for(len(legs))
        multiples = len(legs) > 1
        # a fresh book if any quotes the whole slip; else a stale one, so the gate can say why it fails
        found = common_book(legs, now, self.thresholds.max_quote_age_seconds, self.priority, multiples=multiples) or common_book(
            legs, now, float("inf"), self.priority, multiples=multiples
        )
        if found is None:
            return None
        book, quotes = found
        gate = self.gate.check(legs)
        sim = self.engine.simulate(legs, [q.net_odds for q in quotes], kind)
        verdict = judge(legs, quotes, sim, gate, self.thresholds, now, kind)
        cap = self.max_stake_pct if verdict.tier is Tier.VETTED else self.value_max_stake_pct if verdict.tier is Tier.VALUE else 0.0
        leagues = tuple(sorted({leg.league or leg.sport_key or "?" for leg in legs}))
        return SlipCandidate(
            slip_id(kind, legs), kind, list(legs), book, quotes, sim, verdict,
            stake_fraction=min(sim.kelly_fraction * self.kelly_fraction, cap), cross_league=len(legs) > 1 and len(leagues) > 1, leagues=leagues,
        )

    def generate(self, candidates: Sequence[LegCandidate], now: datetime) -> GenerationReport:
        report = GenerationReport()
        max_age = self.thresholds.max_quote_age_seconds
        priced: list[tuple[float, LegCandidate]] = []
        for leg in candidates:
            try:
                quote = leg.best_quote(now, max_age)
                if quote is None:
                    report.rejected["no fresh price"] += 1
                    continue
                ev = leg.ev(quote.net_odds)
            except ValueError:
                report.rejected["unpriced"] += 1
                continue
            if ev > 0:
                priced.append((ev, leg))
            else:
                report.rejected["leg EV <= 0"] += 1
        priced.sort(key=lambda item: item[0], reverse=True)
        # Per fixture: its best-EV leg and, if different, its best likely leg (>= 50%). A longshot's EV
        # alone would crowd out the likely legs, the only ones a multiple can clear the probability bar with.
        per_fixture: dict[str, list[LegCandidate]] = {}
        for _, leg in priced:
            per_fixture.setdefault(leg.fixture_id, []).append(leg)
        pool: list[LegCandidate] = []
        likely_pool: list[LegCandidate] = []
        for legs in per_fixture.values():
            pool.append(legs[0])
            likely = next((leg for leg in legs if leg.probability >= 0.5), None)
            if likely is not None:
                likely_pool.append(likely)
                if likely is not legs[0]:
                    pool.append(likely)
        pool = pool[: self.max_legs * 2]
        report.legs_considered = len(pool)
        combos: list[tuple[SlipKind, tuple[LegCandidate, ...]]] = [(SlipKind.SINGLE, (leg,)) for _, leg in priced[: self.max_legs * 2]]
        for size in (2, 3):
            combos += [(kind_for(size), combo) for combo in itertools.combinations(pool, size) if len({leg.fixture_id for leg in combo}) == size]
        distinct = list({leg.fixture_id: leg for leg in reversed(pool)}.values())[::-1]  # the best leg of each fixture, best first
        for source in (likely_pool, distinct):
            for kind in AUTO_SYSTEMS:
                required = SYSTEMS[kind][0]
                if len(source) >= required:
                    combos.append((kind, tuple(source[:required])))
        seen: set[str] = set()
        for kind, legs in combos:
            report.scanned += 1
            # cheap pre-screen: the exact EV at each leg's best price must be near the bar
            exact = [leg.ev(q.net_odds) + 1 for leg in legs if (q := leg.best_quote(now, max_age, multiples=len(legs) > 1)) is not None]
            if len(exact) != len(legs):
                report.rejected["no common book"] += 1
                continue
            rough = float(np.mean([math.prod(exact[j] for j in line) for line in lines_of(kind, len(legs))])) - 1.0
            if rough < self.thresholds.min_joint_ev - 0.02:
                report.rejected["joint EV under the bar"] += 1
                continue
            slip = self.evaluate(legs, kind, now)
            report.simulated += 1
            if slip is None:
                report.rejected["no common book"] += 1
                continue
            if slip.verdict.tier is Tier.REJECTED or slip.slip_id in seen:
                failed = next((name for name, ok in slip.verdict.checks.items() if not ok), None)
                report.rejected[CHECK_LABELS.get(failed or "", "duplicate")] += 1
                continue
            seen.add(slip.slip_id)
            report.slips.append(slip)
        report.slips.sort(key=lambda s: (s.verdict.tier is not Tier.VETTED, -(s.sim.joint_ev * s.sim.joint_probability)))
        report.slips = report.slips[: self.max_slips]
        return report


def league_label(sport_key: str | None) -> str | None:
    if not sport_key:
        return None
    known = {
        "soccer_epl": "EPL", "soccer_spain_la_liga": "La Liga", "soccer_italy_serie_a": "Serie A", "soccer_germany_bundesliga": "Bundesliga",
        "soccer_france_ligue_one": "Ligue 1", "soccer_uefa_champs_league": "Champions League", "soccer_netherlands_eredivisie": "Eredivisie",
        "soccer_india_isl": "ISL", "basketball_nba": "NBA", "americanfootball_nfl": "NFL", "cricket_ipl": "IPL",
    }
    return known.get(sport_key, sport_key.replace("_", " ").title())


__all__ = [
    "AntiCorrelationGate", "Clash", "GateResult", "GateThresholds", "GenerationReport", "LegCandidate", "MarketKind", "ParlayEngine", "Quote",
    "ScenarioEngine", "SimulationResult", "SlipCandidate", "SlipKind", "Tier", "Verdict", "judge", "kind_for", "league_label", "lines_of",
]
