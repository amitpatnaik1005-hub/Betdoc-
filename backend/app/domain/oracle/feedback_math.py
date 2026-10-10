"""The feedback loop's mathematics (Group 73): scoring rules, closing-line value, weights, calibration, root causes.

Outcomes are scored on the settlement scale ``y``: won 1, half won 0.75, half lost 0.25, lost 0 (a void
carries no information about a prediction and is never scored).

* Brier score ``(p - y)^2`` and log loss ``-[y ln p + (1 - y) ln(1 - p)]`` with ``p`` clipped to
  ``[eps, 1 - eps]``. ``p`` is the predictor's expected settlement score ``sum_r score(r) P(r)`` when its
  whole outcome distribution is known, else its win chance (identical on a line that cannot push).
* Ranked probability score over the five ordered results (lost < half lost < void < half won < won):
  ``RPS = 1/(K-1) sum_k (F_k - O_k)^2``, the discrete form of CRPS ``int (F(t) - 1{y <= t})^2 dt`` on unit
  spacing, normalised to [0, 1]. On a two-outcome leg it equals the Brier score.
* Closing-line value: ``CLV = placed / closing - 1`` against the sharp book's raw closing price, and
  ``CLV_sharp = placed x pi_fair - 1`` against its Shin de-vigged closing probability (the bet's EV at
  the closing line). A multiple compounds: the products of its legs' prices and probabilities.
* Weights are the recalibration engine's (``app.domain.oracle.calibration``, Group 74).
* Calibration: predictions binned by probability; per bin the mean prediction against the mean outcome;
  the expected calibration error ``ECE = sum_b n_b / N |mean_p_b - mean_y_b|``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.oracle.markets import LegResult
from app.models.feedback import RootCauseTag

OUTCOME_SCORE: dict[LegResult, float] = {LegResult.WON: 1.0, LegResult.HALF_WON: 0.75, LegResult.VOID: 0.5, LegResult.HALF_LOST: 0.25, LegResult.LOST: 0.0}
ORDERED: tuple[LegResult, ...] = (LegResult.LOST, LegResult.HALF_LOST, LegResult.VOID, LegResult.HALF_WON, LegResult.WON)


def outcome_score(result: LegResult) -> float | None:
    """The settlement score of a leg result; None for a void (nothing to score)."""
    return None if result is LegResult.VOID else OUTCOME_SCORE[result]


def clip01(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def brier_score(p: float, y: float) -> float:
    return (clip01(p) - clip01(y)) ** 2


def log_loss(p: float, y: float, eps: float) -> float:
    q = min(max(float(p), eps), 1.0 - eps)
    t = clip01(y)
    return -(t * math.log(q) + (1.0 - t) * math.log(1.0 - q))


def _normalised(dist: Mapping[LegResult | str, float]) -> dict[LegResult, float]:
    raw = {LegResult(k): max(float(v), 0.0) for k, v in dist.items()}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("an outcome distribution needs positive mass")
    return {r: raw.get(r, 0.0) / total for r in ORDERED}


def expected_score(dist: Mapping[LegResult | str, float]) -> float:
    d = _normalised(dist)
    return sum(OUTCOME_SCORE[r] * p for r, p in d.items())


def win_probability(dist: Mapping[LegResult | str, float]) -> float:
    d = _normalised(dist)
    return d[LegResult.WON] + d[LegResult.HALF_WON]


def ranked_probability_score(dist: Mapping[LegResult | str, float], result: LegResult) -> float:
    d = _normalised(dist)
    cumulative = 0.0
    total = 0.0
    hit = ORDERED.index(result)
    for k, r in enumerate(ORDERED[:-1]):
        cumulative += d[r]
        observed = 1.0 if hit <= k else 0.0
        total += (cumulative - observed) ** 2
    return total / (len(ORDERED) - 1)


def clv_pct(placed: float | None, closing: float | None) -> float | None:
    if placed is None or closing is None or placed <= 1.0 or closing <= 1.0:
        return None
    return (placed / closing - 1.0) * 100.0


def clv_sharp_pct(placed: float | None, fair_probability: float | None) -> float | None:
    if placed is None or fair_probability is None or placed <= 1.0 or not 0.0 < fair_probability < 1.0:
        return None
    return (placed * fair_probability - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    predicted_mean: float | None
    observed_mean: float | None
    count: int


def calibration(pairs: Iterable[tuple[float, float]], bins: int) -> tuple[list[CalibrationBin], float | None]:
    """(bins, expected calibration error) for (prediction, outcome) pairs; ECE is None with no pairs."""
    sums = [[0.0, 0.0, 0] for _ in range(bins)]
    n = 0
    for p, y in pairs:
        b = min(int(clip01(p) * bins), bins - 1)
        sums[b][0] += clip01(p)
        sums[b][1] += clip01(y)
        sums[b][2] += 1
        n += 1
    out = [
        CalibrationBin(i / bins, (i + 1) / bins, s / c if c else None, o / c if c else None, int(c))
        for i, (s, o, c) in enumerate(sums)
    ]
    if n == 0:
        return out, None
    ece = sum(b.count / n * abs(b.predicted_mean - b.observed_mean) for b in out if b.count)  # type: ignore[operator]
    return out, ece


# ================================================================ root causes
@dataclass(frozen=True, slots=True)
class LossEvidence:
    status: str  # the bet's settled status
    win_probability: float | None  # the ensemble's chance the bet would win (the product of its legs')
    clv_pct: float | None
    inplay_collapse: tuple[float, float] | None = None  # (win probability at placing, at the pullout) when the watch called a collapse
    weather_breach: str | None = None  # what the weather evidence showed past the fortress's limits
    strict_referee: str | None = None  # the referee and their cards per game, when past the strictness line
    model_brier: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RcaPolicy:
    confident_prob: float
    steam_clv_pct: float


LOSING = frozenset({"LOST", "HALF_LOST"})


def classify(evidence: LossEvidence, policy: RcaPolicy) -> tuple[RootCauseTag, str]:
    """The first cause the evidence supports, in order: an in-play shock, adverse steam, the weather, an
    over-confident model, the referee; else variance."""
    if evidence.status not in LOSING:
        return RootCauseTag.NONE, f"{evidence.status.lower().replace('_', ' ')}: nothing to explain"
    if evidence.inplay_collapse is not None:
        before, after = evidence.inplay_collapse
        return RootCauseTag.INPLAY_SHOCK_RED_CARD, f"an in-play shock (a red card, a penalty or an early goal): the watch saw the win chance fall {before:.0%} -> {after:.0%}"
    if evidence.clv_pct is not None and evidence.clv_pct <= policy.steam_clv_pct:
        return RootCauseTag.STEAM_ADVERSE_SELECTION, f"the sharp line closed {evidence.clv_pct:+.1f}% against the price taken: money moved against the bet before kickoff"
    if evidence.weather_breach is not None:
        return RootCauseTag.WEATHER_ANOMALY, f"the weather was past the fortress's limits ({evidence.weather_breach})"
    if evidence.win_probability is not None and evidence.win_probability >= policy.confident_prob:
        brier = f"; the models' Brier score on it {evidence.model_brier:.3f}" if evidence.model_brier is not None else ""
        return RootCauseTag.MODEL_UNDERESTIMATION, f"the models gave it {evidence.win_probability:.0%} and it lost: they under-rated the risk{brier}"
    if evidence.strict_referee is not None:
        return RootCauseTag.REFEREE_STRICTNESS_BIAS, f"a strict referee ({evidence.strict_referee})"
    chance = f"a {evidence.win_probability:.0%} chance" if evidence.win_probability is not None else "a priced chance"
    clv = f", the price beat the close by {evidence.clv_pct:+.1f}%" if evidence.clv_pct is not None and evidence.clv_pct > 0 else ""
    return RootCauseTag.VARIANCE_BAD_LUCK, f"no other cause found: {chance} that did not come in{clv}"


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
