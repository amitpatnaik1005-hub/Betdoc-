"""The numeric toolkit every sport adapter prices with (Group 79). Developed for Amit Ashok Kumar Patnaik.

A sport model ends in a distribution, never in a single number: integer score margins and totals
(``IntPMF``) or a joint home/away score grid (``ScoreGrid``). Every line market is priced from that
distribution with the push and the quarter-ball split handled exactly, so a whole line (-3.0) carries
its push mass and a quarter line (-0.75) its half-win / half-loss mass:

* ``LineOutcome`` holds P(win), P(half win), P(push), P(half loss), P(loss) for one side of a line;
* its fair decimal odds solve ``EV = 0``:  ``o = 1 + (P_l + P_hl/2) / (P_w + P_hw/2)``;
* ``effective_probability`` is ``1 / o``, the number a bettor compares with a quoted price.

Several independent sub-models per sport are blended by ``blend`` into one consensus; the dispersion
between them is what pillar 1 of the twin reads as agreement.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

EPS = 1e-12
PROB_FLOOR = 1e-4
PROB_CEIL = 1.0 - 1e-4


# ================================================================ scalar helpers
def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def clamp_prob(p: float) -> float:
    return clamp(float(p), PROB_FLOOR, PROB_CEIL)


def fair_odds(p: float) -> float:
    """Unmargined decimal odds of a probability (floored so a near-impossible outcome stays finite)."""
    return 1.0 / clamp_prob(p)


def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def poisson_pmf(lam: float, size: int) -> np.ndarray:
    """P(X = k) for k in 0..size-1, with the tail mass folded into the last cell so the vector sums to 1."""
    lam = max(float(lam), 0.0)
    k = np.arange(size, dtype=float)
    if lam == 0.0:
        out = np.zeros(size)
        out[0] = 1.0
        return out
    log_p = -lam + k * math.log(lam) - np.array([math.lgamma(i + 1.0) for i in range(size)])
    pmf = np.exp(log_p)
    pmf[-1] += max(0.0, 1.0 - float(pmf.sum()))
    return pmf


def negative_binomial_pmf(mean: float, dispersion: float, size: int) -> np.ndarray:
    """Over-dispersed counts (variance = mean + mean^2 / dispersion): baseball runs, cricket overs.
    A dispersion of infinity is the Poisson."""
    mean = max(float(mean), 1e-9)
    if not math.isfinite(dispersion) or dispersion > 1e6:
        return poisson_pmf(mean, size)
    r = float(dispersion)
    p = r / (r + mean)
    k = np.arange(size, dtype=float)
    log_pmf = (np.array([math.lgamma(i + r) - math.lgamma(r) - math.lgamma(i + 1.0) for i in range(size)])
               + r * math.log(p) + k * math.log(1.0 - p))
    pmf = np.exp(log_pmf)
    pmf[-1] += max(0.0, 1.0 - float(pmf.sum()))
    return pmf


def split_line(line: float) -> list[float]:
    """Whole/half line -> [line]; quarter line -> its two halves (-0.75 -> [-1.0, -0.5])."""
    if not math.isfinite(line):
        raise ValueError("a line must be finite")
    quarters = round(line * 4.0)
    if abs(line * 4.0 - quarters) > 1e-9:
        return [line]  # an alternative line off the quarter grid (e.g. 2.3) settles as one line
    if quarters % 2 == 0:
        return [quarters / 4.0]
    return [(quarters - 1) / 4.0, (quarters + 1) / 4.0]


# ================================================================ line outcomes
@dataclass(frozen=True, slots=True)
class LineOutcome:
    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float

    @property
    def fair_odds(self) -> float:
        up = self.win + 0.5 * self.half_win
        down = self.loss + 0.5 * self.half_loss
        if up <= EPS:
            return 1.0 / PROB_FLOOR
        if down <= EPS:
            return 1.0 + PROB_FLOOR
        return 1.0 + down / up

    @property
    def effective_probability(self) -> float:
        return round(1.0 / self.fair_odds, 6)

    def as_dict(self) -> dict[str, float]:
        return {"win": round(self.win, 6), "half_win": round(self.half_win, 6), "push": round(self.push, 6),
                "half_loss": round(self.half_loss, 6), "loss": round(self.loss, 6), "fair_odds": round(self.fair_odds, 4)}


@dataclass(frozen=True)
class IntPMF:
    """A distribution over consecutive integers ``offset .. offset + len(probs) - 1``."""

    offset: int
    probs: np.ndarray

    @classmethod
    def from_array(cls, probs: Iterable[float], offset: int = 0) -> IntPMF:
        arr = np.clip(np.asarray(list(probs), dtype=float), 0.0, None)
        total = float(arr.sum())
        if total <= 0:
            raise ValueError("a distribution needs positive mass")
        return cls(offset, arr / total)

    @classmethod
    def from_normal(cls, mu: float, sigma: float, lo: int, hi: int) -> IntPMF:
        """A continuous normal discretised on integers (continuity correction), tails folded into the ends."""
        ks = np.arange(lo, hi + 1, dtype=float)
        upper = np.array([normal_cdf(k + 0.5, mu, sigma) for k in ks])
        lower = np.array([normal_cdf(k - 0.5, mu, sigma) for k in ks])
        probs = upper - lower
        probs[0] += lower[0]
        probs[-1] += 1.0 - upper[-1]
        return cls.from_array(probs, lo)

    @property
    def values(self) -> np.ndarray:
        return np.arange(self.offset, self.offset + len(self.probs))

    @property
    def mean(self) -> float:
        return float(np.dot(self.values, self.probs))

    @property
    def std(self) -> float:
        m = self.mean
        return math.sqrt(max(0.0, float(np.dot((self.values - m) ** 2, self.probs))))

    def prob_eq(self, k: int) -> float:
        i = k - self.offset
        return float(self.probs[i]) if 0 <= i < len(self.probs) else 0.0

    def prob_gt(self, x: float) -> float:
        return float(self.probs[self.values > x + 1e-9].sum())

    def prob_lt(self, x: float) -> float:
        return float(self.probs[self.values < x - 1e-9].sum())

    def quantile(self, q: float) -> int:
        cdf = np.cumsum(self.probs)
        return int(self.values[min(int(np.searchsorted(cdf, q)), len(cdf) - 1)])

    def line_outcome(self, line: float, *, over: bool) -> LineOutcome:
        """Over (or cover) ``line``: the side wins when value - line > 0 (over) or < 0 (under). For a handicap
        pass the margin PMF with ``line`` = -handicap and over=True: margin + handicap > 0. Vectorised over the support."""
        halves = split_line(line)
        sign = 1.0 if over else -1.0
        results = [np.sign(np.where(np.abs(sign * (self.values - h)) <= 1e-9, 0.0, sign * (self.values - h))) for h in halves]
        p = self.probs
        if len(results) == 1:
            r = results[0]
            return LineOutcome(float(p[r > 0].sum()), 0.0, float(p[r == 0].sum()), 0.0, float(p[r < 0].sum()))
        a, b = results
        total = a + b
        return LineOutcome(
            win=float(p[(a > 0) & (b > 0)].sum()),
            half_win=float(p[total == 1].sum()),
            push=float(p[((a == 0) & (b == 0)) | ((a != b) & (total == 0))].sum()),
            half_loss=float(p[total == -1].sum()),
            loss=float(p[(a < 0) & (b < 0)].sum()),
        )

    @classmethod
    def mix(cls, parts: Sequence[tuple[IntPMF, float]]) -> IntPMF:
        """A weighted mixture of distributions (weights need not sum to 1; non-positive weights are skipped)."""
        live = [(pmf, float(w)) for pmf, w in parts if w > 0]
        if not live:
            raise ValueError("a mixture needs a positive weight")
        lo = min(pmf.offset for pmf, _ in live)
        hi = max(pmf.offset + len(pmf.probs) - 1 for pmf, _ in live)
        out = np.zeros(hi - lo + 1)
        total = sum(w for _, w in live)
        for pmf, w in live:
            out[pmf.offset - lo: pmf.offset - lo + len(pmf.probs)] += pmf.probs * (w / total)
        return cls.from_array(out, lo)

    def shift(self, delta: int) -> IntPMF:
        return IntPMF(self.offset + delta, self.probs)

    def convolve(self, other: IntPMF) -> IntPMF:
        """The distribution of X + Y for independent X, Y."""
        return IntPMF(self.offset + other.offset, np.convolve(self.probs, other.probs))

    def negate(self) -> IntPMF:
        return IntPMF(-(self.offset + len(self.probs) - 1), self.probs[::-1].copy())


# ================================================================ joint score grids
@dataclass(frozen=True)
class ScoreGrid:
    """P(home = i, away = j). Built from independent marginals or a correlated (Dixon-Coles) construction."""

    matrix: np.ndarray

    @classmethod
    def independent(cls, home: np.ndarray, away: np.ndarray) -> ScoreGrid:
        grid = np.outer(home, away)
        return cls(grid / grid.sum())

    @classmethod
    def normalised(cls, matrix: np.ndarray) -> ScoreGrid:
        m = np.clip(matrix, 0.0, None)
        return cls(m / m.sum())

    @property
    def size(self) -> tuple[int, int]:
        return self.matrix.shape  # type: ignore[return-value]

    def outcome(self) -> tuple[float, float, float]:
        m = self.matrix
        home = float(np.tril(m, -1).sum())
        away = float(np.triu(m, 1).sum())
        draw = float(np.trace(m))
        return home, draw, away

    def home_pmf(self) -> IntPMF:
        return IntPMF(0, self.matrix.sum(axis=1))

    def away_pmf(self) -> IntPMF:
        return IntPMF(0, self.matrix.sum(axis=0))

    def margin_pmf(self) -> IntPMF:
        """home - away: row i puts m[i, j] on margin i - j, i.e. the reversed row lands on indices i .. i + cols - 1."""
        rows, cols = self.matrix.shape
        probs = np.zeros(rows + cols - 1)
        for i in range(rows):
            probs[i: i + cols] += self.matrix[i, ::-1]
        return IntPMF(-(cols - 1), probs)

    def total_pmf(self) -> IntPMF:
        rows, cols = self.matrix.shape
        probs = np.zeros(rows + cols - 1)
        for i in range(rows):
            probs[i: i + cols] += self.matrix[i]
        return IntPMF(0, probs)

    def btts(self) -> float:
        return float(self.matrix[1:, 1:].sum())

    def clean_sheet(self, side: str) -> float:
        """P(the side concedes nothing)."""
        return float(self.matrix[:, 0].sum()) if side == "home" else float(self.matrix[0, :].sum())

    def correct_scores(self, top: int = 10) -> list[tuple[str, float]]:
        flat = [(f"{i}-{j}", float(self.matrix[i, j])) for i in range(self.matrix.shape[0]) for j in range(self.matrix.shape[1])]
        flat.sort(key=lambda kv: kv[1], reverse=True)
        return flat[:top]

    def prob_score(self, home: int, away: int) -> float:
        if 0 <= home < self.matrix.shape[0] and 0 <= away < self.matrix.shape[1]:
            return float(self.matrix[home, away])
        return 0.0


# ================================================================ consensus (pillar 1)
@dataclass(frozen=True, slots=True)
class SubModelEstimate:
    """One independent analytical sub-model's verdict on a match."""

    name: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float = 0.0
    expected_total: float | None = None
    weight: float = 1.0

    def normalised(self) -> SubModelEstimate:
        total = self.home_win_prob + self.away_win_prob + self.draw_prob
        if total <= 0:
            raise ValueError(f"sub-model {self.name} produced no probability mass")
        return SubModelEstimate(self.name, self.home_win_prob / total, self.away_win_prob / total, self.draw_prob / total,
                                self.expected_total, self.weight)

    def as_dict(self) -> dict[str, float | str | None]:
        return {"name": self.name, "home_win_prob": round(self.home_win_prob, 4), "away_win_prob": round(self.away_win_prob, 4),
                "draw_prob": round(self.draw_prob, 4),
                "expected_total": None if self.expected_total is None else round(self.expected_total, 3), "weight": round(self.weight, 4)}


@dataclass(frozen=True, slots=True)
class Consensus:
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    model_count: int
    max_deviation: float  # the largest absolute gap between a sub-model's home probability and the consensus
    dispersion: float  # weighted standard deviation of the home probability across sub-models
    agreement: float  # 1 - dispersion / 0.25, in [0, 1]: 1 when every sub-model agrees exactly
    sub_models: tuple[SubModelEstimate, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"home_win_prob": round(self.home_win_prob, 4), "away_win_prob": round(self.away_win_prob, 4),
                "draw_prob": round(self.draw_prob, 4), "model_count": self.model_count,
                "max_deviation": round(self.max_deviation, 4), "dispersion": round(self.dispersion, 4),
                "agreement": round(self.agreement, 4)}


MIN_SUB_MODELS = 3


def blend(estimates: Sequence[SubModelEstimate], weights: dict[str, float] | None = None) -> Consensus:
    """Weighted linear opinion pool. ``weights`` (pillar 1's calibrated weights, by sub-model name) override each
    estimate's own weight; a weight of 0 benches a sub-model. At least three must vote."""
    pool = [e.normalised() for e in estimates]
    w = [max(0.0, float((weights or {}).get(e.name, e.weight))) for e in pool]
    voting = [(e, wi) for e, wi in zip(pool, w, strict=True) if wi > 0]
    if len(voting) < MIN_SUB_MODELS:
        raise ValueError(f"consensus needs at least {MIN_SUB_MODELS} voting sub-models, got {len(voting)}")
    total_w = sum(wi for _, wi in voting)
    home = sum(e.home_win_prob * wi for e, wi in voting) / total_w
    away = sum(e.away_win_prob * wi for e, wi in voting) / total_w
    draw = sum(e.draw_prob * wi for e, wi in voting) / total_w
    var = sum(wi * (e.home_win_prob - home) ** 2 for e, wi in voting) / total_w
    dispersion = math.sqrt(var)
    max_dev = max(abs(e.home_win_prob - home) for e, _ in voting)
    agreement = clamp(1.0 - dispersion / 0.25, 0.0, 1.0)
    return Consensus(home, away, draw, len(voting), max_dev, dispersion, agreement, tuple(pool))


def weighted_mean(values: Sequence[tuple[float, float]]) -> float:
    total = sum(w for _, w in values)
    return sum(v * w for v, w in values) / total if total > 0 else 0.0
