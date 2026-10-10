"""The model recalibration engine's mathematics (Group 74): skill, lifecycle and pillar 1's weights.

Input: every settled prediction in the window (``model_prediction_feedback``, Group 73), each with its age.

1. **Brier scores** per model: the plain mean over the window and over the short window, and the
   half-life decayed mean ``sum_i w_i (p_i - o_i)^2 / sum_i w_i`` with ``w_i = exp(-ln 2 / T_half x age_i)``.
2. **Brier skill** against the benchmark (the sharp close, de-vigged): ``BSS = 1 - BS_m / BS_bench`` over
   the legs *both* priced. Models price different legs (an audit's models only the twin's slips), so
   comparing each model's own mean against the benchmark's overall mean would compare different matches;
   the paired form compares like with like. It counts once ``MIN_SAMPLES`` legs are paired.
3. **Murphy's decomposition** ``BS = REL - RES + UNC`` over probability bins, with ``UNC`` the variance of
   the outcomes (``o(1 - o)`` when outcomes are 0/1; half results make it ``mean(o^2) - mean(o)^2``). With
   forecasts that vary inside a bin the identity is approximate; the residual is reported.
4. **Lifecycle**, first match wins (a verdict other than ACTIVE needs ``MIN_SAMPLES`` predictions; a newer
   model is ACTIVE with a shrunk weight):
   BENCHED: short-window Brier >= ``AUTO_BENCH_BRIER`` (over ``MIN_SAMPLES`` recent predictions) or, over
   ``BENCH_CLV_MIN_SAMPLES``, CLV < ``BENCH_CLV``; PROBATION: BSS < ``PROBATION_BSS`` or CLV < ``PROBATION_CLV``;
   ALPHA_BOOSTED: BSS >= ``PROMOTION_BSS`` and CLV >= ``PROMOTION_CLV``; else ACTIVE.
5. **Weights**: ``S_m = -BS_m(decayed) / tau + beta x CLV_m`` over the models not benched;
   ``w~_m = softmax(S)_m x K`` over the established models (the mean is 1: pillar 1 gives an unknown model
   1). A model short of history is scored against them (capped at the highest band) and shrunk to the
   neutral 1: ``w = N/(N + N0) w~ + N0/(N + N0)``; an established one is clamped into its state's band.
   Benched: 0, and pillar 1 takes its veto away. An administrator's pin wins over all.

CLV here is the closing-line value of the legs a model priced (the price the bet took against the sharp
close): a property of the bets the model was asked about, shared by every model that priced the same leg.
It separates models only through the legs they priced; Brier skill is what tells them apart on a leg.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.model_calibration import LIFECYCLE_RANK, ModelLifecycleStatus

Status = ModelLifecycleStatus


@dataclass(frozen=True, slots=True)
class Observation:
    model: str
    leg_id: str
    predicted: float
    outcome: float
    brier: float
    clv_pct: float | None
    age_days: float


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    benchmark: str
    references: frozenset[str]  # predictors measured but never weighted (the benchmark among them)
    short_window_days: float
    half_life_days: float
    temperature: float
    clv_weight: float
    min_samples: int
    prior_strength: float
    auto_bench_brier: float
    bench_clv: float
    bench_clv_min_samples: int
    promotion_bss: float
    promotion_clv: float
    probation_bss: float
    probation_clv: float
    bands: Mapping[str, tuple[float, float]]
    bins: int

    @classmethod
    def from_settings(cls, settings: Any, references: Iterable[str]) -> CalibrationPolicy:
        return cls(
            benchmark=settings.TWIN_RECALIBRATION_BENCHMARK_MODEL, references=frozenset({*references, settings.TWIN_RECALIBRATION_BENCHMARK_MODEL}),
            short_window_days=settings.TWIN_RECALIBRATION_SHORT_WINDOW_DAYS, half_life_days=settings.TWIN_RECALIBRATION_HALF_LIFE_DAYS,
            temperature=settings.TWIN_RECALIBRATION_TEMPERATURE, clv_weight=settings.TWIN_RECALIBRATION_CLV_WEIGHT, min_samples=settings.TWIN_RECALIBRATION_MIN_SAMPLES,
            prior_strength=settings.TWIN_RECALIBRATION_PRIOR_STRENGTH, auto_bench_brier=settings.TWIN_RECALIBRATION_AUTO_BENCH_BRIER,
            bench_clv=settings.TWIN_RECALIBRATION_BENCH_CLV, bench_clv_min_samples=settings.TWIN_RECALIBRATION_BENCH_CLV_MIN_SAMPLES,
            promotion_bss=settings.TWIN_RECALIBRATION_PROMOTION_BSS, promotion_clv=settings.TWIN_RECALIBRATION_PROMOTION_CLV,
            probation_bss=settings.TWIN_RECALIBRATION_PROBATION_BSS, probation_clv=settings.TWIN_RECALIBRATION_PROBATION_CLV,
            bands={k: (float(lo), float(hi)) for k, (lo, hi) in settings.TWIN_RECALIBRATION_WEIGHT_BANDS.items()}, bins=settings.FEEDBACK_CALIBRATION_BINS,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark, "short_window_days": self.short_window_days, "half_life_days": self.half_life_days, "temperature": self.temperature,
            "clv_weight": self.clv_weight, "min_samples": self.min_samples, "prior_strength": self.prior_strength, "auto_bench_brier": self.auto_bench_brier,
            "bench_clv": self.bench_clv, "bench_clv_min_samples": self.bench_clv_min_samples, "promotion_bss": self.promotion_bss, "promotion_clv": self.promotion_clv,
            "probation_bss": self.probation_bss, "probation_clv": self.probation_clv, "bands": {k: list(v) for k, v in self.bands.items()},
        }


# ================================================================ scores
def decay_weight(age_days: float, half_life_days: float) -> float:
    return math.exp(-math.log(2.0) / half_life_days * max(age_days, 0.0))


def decayed_brier(rows: Sequence[Observation], half_life_days: float) -> float | None:
    weights = [decay_weight(r.age_days, half_life_days) for r in rows]
    total = sum(weights)
    return None if total <= 0 else sum(w * r.brier for w, r in zip(weights, rows, strict=True)) / total


@dataclass(frozen=True, slots=True)
class Murphy:
    reliability: float
    resolution: float
    uncertainty: float
    brier: float
    residual: float  # brier - (reliability - resolution + uncertainty): zero when forecasts are constant within each bin


def murphy_decomposition(pairs: Sequence[tuple[float, float]], bins: int) -> Murphy | None:
    if not pairs:
        return None
    n = len(pairs)
    base = sum(o for _, o in pairs) / n
    groups: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, o in pairs:
        groups[min(int(min(max(p, 0.0), 1.0) * bins), bins - 1)].append((p, o))
    rel = res = 0.0
    for members in groups.values():
        k = len(members)
        p_bar = sum(p for p, _ in members) / k
        o_bar = sum(o for _, o in members) / k
        rel += k / n * (p_bar - o_bar) ** 2
        res += k / n * (o_bar - base) ** 2
    unc = sum(o * o for _, o in pairs) / n - base * base
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    return Murphy(rel, res, unc, brier, brier - (rel - res + unc))


# ================================================================ verdicts
@dataclass(slots=True)
class ModelVerdict:
    model: str
    samples: int
    samples_short: int
    paired: int
    brier_short: float | None
    brier_long: float | None
    brier_decayed: float | None
    bss: float | None  # over the paired legs (None: the benchmark priced none of them)
    bss_counts: bool  # enough paired legs for the BSS to move the lifecycle
    clv_pct: float | None
    murphy: Murphy | None
    status: Status
    reason: str
    previous_weight: float | None
    previous_status: Status | None
    score: float | None = None  # the softmax logit
    weight: float = 1.0
    pinned: bool = False

    def snapshot(self) -> dict[str, Any]:
        m = self.murphy
        return {
            "samples": self.samples, "samples_short": self.samples_short, "paired": self.paired, "bss_counts": self.bss_counts, "score": _r(self.score),
            "brier_short": _r(self.brier_short), "brier_long": _r(self.brier_long), "brier_decayed": _r(self.brier_decayed), "bss": _r(self.bss), "clv_pct": _r(self.clv_pct),
            "murphy": None if m is None else {"reliability": _r(m.reliability), "resolution": _r(m.resolution), "uncertainty": _r(m.uncertainty), "brier": _r(m.brier), "residual": _r(m.residual)},
            "pinned": self.pinned,
        }


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


@dataclass(slots=True)
class CalibrationResult:
    verdicts: list[ModelVerdict]
    benchmark_brier: float | None
    weights: dict[str, float]
    promoted: int
    demoted: int
    references: dict[str, int] = field(default_factory=dict)  # reference predictor -> its sample count


def classify(v: ModelVerdict, policy: CalibrationPolicy) -> tuple[Status, str]:
    if v.samples < policy.min_samples:
        return Status.ACTIVE, f"{v.samples} settled prediction(s), under {policy.min_samples}: active, its weight shrunk toward 1"
    if v.samples_short >= policy.min_samples and v.brier_short is not None and v.brier_short >= policy.auto_bench_brier:
        return Status.BENCHED, f"{policy.short_window_days:g}-day Brier {v.brier_short:.4f} is at or above {policy.auto_bench_brier:g} (no better than a coin flip): benched, no vote and no veto"
    if v.samples >= policy.bench_clv_min_samples and v.clv_pct is not None and v.clv_pct < policy.bench_clv:
        return Status.BENCHED, f"CLV {v.clv_pct:+.2f}% over {v.samples} predictions is under {policy.bench_clv:+g}%: benched, no vote and no veto"
    bss = v.bss if v.bss_counts else None
    weak = []
    if bss is not None and bss < policy.probation_bss:
        weak.append(f"Brier skill {bss:+.3f} against {policy.benchmark} is under {policy.probation_bss:+g}")
    if v.clv_pct is not None and v.clv_pct < policy.probation_clv:
        weak.append(f"CLV {v.clv_pct:+.2f}% is under {policy.probation_clv:+g}%")
    if weak:
        return Status.PROBATION, "probation: " + "; ".join(weak)
    if bss is not None and bss >= policy.promotion_bss and v.clv_pct is not None and v.clv_pct >= policy.promotion_clv:
        return Status.ALPHA_BOOSTED, f"alpha boost: Brier skill {bss:+.3f} against {policy.benchmark} and CLV {v.clv_pct:+.2f}% clear {policy.promotion_bss:+g} and {policy.promotion_clv:+g}%"
    detail = f"Brier skill {bss:+.3f}" if bss is not None else f"Brier skill not yet measurable ({v.paired} leg(s) paired with {policy.benchmark})"
    return Status.ACTIVE, f"active: {detail}" + ("" if v.clv_pct is None else f", CLV {v.clv_pct:+.2f}%")


def evaluate(observations: Iterable[Observation], policy: CalibrationPolicy, *, previous_weights: Mapping[str, float] | None = None,
             previous_status: Mapping[str, str] | None = None, pins: Mapping[str, float] | None = None) -> CalibrationResult:
    previous_weights, previous_status, pins = previous_weights or {}, previous_status or {}, pins or {}
    by_model: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        by_model[o.model].append(o)
    bench = {o.leg_id: o.brier for o in by_model.get(policy.benchmark, [])}
    benchmark_brier = _mean(bench.values())
    verdicts: list[ModelVerdict] = []
    for model in sorted(by_model):
        if model in policy.references:
            continue
        rows = by_model[model]
        short = [r for r in rows if r.age_days <= policy.short_window_days]
        paired = [(r.brier, bench[r.leg_id]) for r in rows if r.leg_id in bench]
        bss = None
        if paired:
            bench_mean = sum(b for _, b in paired) / len(paired)
            bss = None if bench_mean <= 0 else 1.0 - (sum(m for m, _ in paired) / len(paired)) / bench_mean
        clvs = [r.clv_pct for r in rows if r.clv_pct is not None]
        prev = previous_status.get(model)
        v = ModelVerdict(
            model=model, samples=len(rows), samples_short=len(short), paired=len(paired), brier_short=_mean(r.brier for r in short), brier_long=_mean(r.brier for r in rows),
            brier_decayed=decayed_brier(rows, policy.half_life_days), bss=bss, bss_counts=bss is not None and len(paired) >= policy.min_samples,
            clv_pct=_mean(clvs), murphy=murphy_decomposition([(r.predicted, r.outcome) for r in rows], policy.bins), status=Status.ACTIVE, reason="",
            previous_weight=previous_weights.get(model), previous_status=Status(prev) if prev in Status.__members__ else None,
        )
        v.status, v.reason = classify(v, policy)
        verdicts.append(v)

    voting = [v for v in verdicts if v.status is not Status.BENCHED]
    for v in voting:
        v.score = -(v.brier_decayed or 0.0) / policy.temperature + policy.clv_weight * (v.clv_pct or 0.0)
    # The softmax is normalised over the established models (MIN_SAMPLES or more); with no established
    # model, over every voting one. A newcomer is scored against that pool without entering its
    # denominator, capped at the highest band, then shrunk: two lucky predictions can neither take a
    # weight nor, through the normalisation, push the established models down.
    established = [v for v in voting if v.samples >= policy.min_samples] or voting
    if voting:
        top = max(v.score for v in established)  # type: ignore[type-var]
        total = sum(math.exp(v.score - top) for v in established)  # type: ignore[operator]
        ceiling = max((hi for _, hi in policy.bands.values()), default=math.inf)
        for v in voting:
            soft = min(math.exp(min(v.score - top, 700.0)) / total * len(established), ceiling)  # type: ignore[operator]
            if v.samples < policy.min_samples:
                lam = v.samples / (v.samples + policy.prior_strength)
                v.weight = lam * soft + (1.0 - lam)
            else:
                lo, hi = policy.bands.get(v.status.value, (0.0, math.inf))
                v.weight = min(max(soft, lo), hi)
    for v in verdicts:
        if v.status is Status.BENCHED:
            v.weight = 0.0
        if v.model in pins:
            v.weight, v.pinned = float(pins[v.model]), True
            v.reason += f"; pinned by an administrator at {pins[v.model]:g}"
        v.weight = round(v.weight, 6)
    promoted = demoted = 0
    for v in verdicts:
        before = LIFECYCLE_RANK[v.previous_status or Status.ACTIVE]
        after = LIFECYCLE_RANK[v.status]
        promoted += after > before
        demoted += after < before
    weights = {v.model: v.weight for v in verdicts}
    for model, pinned in pins.items():  # a pin on a model with no settled predictions yet still holds
        weights.setdefault(model, round(float(pinned), 6))
    return CalibrationResult(
        verdicts, benchmark_brier, weights, promoted, demoted,
        references={m: len(by_model[m]) for m in sorted(by_model) if m in policy.references},
    )
