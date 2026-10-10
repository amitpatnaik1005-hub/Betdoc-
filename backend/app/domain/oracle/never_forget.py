"""The Never-Forget shield's mathematics (Group 75): a leg's situation, and how much it resembles a lost one.

A **situation** is what the fortress knew about a leg when it vetted it, each feature scaled to [0, 1]:

    rain      min(1, mm/h / scale)            wind       min(1, km/h / scale)
    fatigue   clamp((full - rest h) / span)   the backed side's rest (a leg backing no side: the more tired side)
    cards     min(1, cards a game / scale)    penalties  min(1, penalties per 90 / scale)
    steam     1 if the sharp line moved against the selection, else 0
    odds      clamp((odds - 1) / scale)       model_ev, sharp_edge   clamp(value / scale)
    public    the measured share of tickets on the selection

Indoors, rain and wind are 0. A feature without fresh evidence is absent, never imputed: an imputed
"normal" value would make every unknown situation look like every memorised one.

Two situations compare over the features both carry, weighted by ``w_k`` (renormalised over them):

    D_w^2 = sum w_k (x_k - m_k)^2 / sum w_k        similarity = exp(-gamma D_w^2)

and only when those shared features hold at least ``min_coverage`` of the lesson's own weight; below it
the comparison is inconclusive (pillar 15 is then UNVERIFIED for an ACTIVE lesson, never a pass).
A similarity at or above the threshold is the same trap: an ACTIVE lesson vetoes, an EXPERIMENTAL one is
reported as a shadow match. With ``scope = "shape"`` a lesson guards only the same market kind and the
same side of it (a lost Over says nothing about an Under in the same rain).

A lesson is only as useful as it is specific: ``specificity`` counts how many of the legs the fortress saw
recently it would have matched. One that matches a large share of them describes ordinary betting, not a
trap, and is kept EXPERIMENTAL until an administrator promotes it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.oracle.markets import MarketKind, MarketRef

FEATURES = ("rain", "wind", "fatigue", "cards", "penalties", "steam", "odds", "model_ev", "sharp_edge", "public")
SCALED = frozenset({"rain", "wind", "cards", "penalties", "odds", "model_ev", "sharp_edge"})
SIDE_MARKETS = (MarketKind.MATCH_ODDS, MarketKind.ASIAN_HANDICAP, MarketKind.DRAW_NO_BET)

ACTIVE = "ACTIVE"
EXPERIMENTAL = "EXPERIMENTAL"
ARCHIVED = "ARCHIVED"


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def bet_shape(market: MarketRef, selection: str) -> str:
    """The kind of bet a lesson guards: the market kind and the side of it (backing either team is one shape)."""
    if market.kind in SIDE_MARKETS and selection in ("HOME", "AWAY"):
        return f"{market.kind.value}:SIDE"
    if market.kind is MarketKind.DOUBLE_CHANCE and selection in ("1X", "X2"):
        return f"{market.kind.value}:SIDE_OR_DRAW"
    return f"{market.kind.value}:{selection}"


# ================================================================ policy
@dataclass(frozen=True, slots=True)
class NeverForgetPolicy:
    enabled: bool
    threshold: float
    gamma: float
    weights: Mapping[str, float]
    scales: Mapping[str, float]
    full_rest_hours: float
    fatigue_span_hours: float
    min_coverage: float
    scope: str  # "shape" | "any"

    @classmethod
    def from_settings(cls, settings: Any) -> NeverForgetPolicy:
        unknown = sorted((set(settings.NEVER_FORGET_FEATURE_WEIGHTS) | set(settings.NEVER_FORGET_FEATURE_SCALES)) - set(FEATURES))
        if unknown:
            raise ValueError(f"NEVER_FORGET_FEATURE_WEIGHTS / _SCALES name unknown features: {', '.join(unknown)} (known: {', '.join(FEATURES)})")
        return cls(
            enabled=bool(settings.NEVER_FORGET_ENABLED), threshold=float(settings.NEVER_FORGET_SIMILARITY_THRESHOLD), gamma=float(settings.NEVER_FORGET_GAMMA),
            weights={k: float(v) for k, v in settings.NEVER_FORGET_FEATURE_WEIGHTS.items() if v > 0},
            scales={k: float(v) for k, v in settings.NEVER_FORGET_FEATURE_SCALES.items()},
            full_rest_hours=float(settings.NEVER_FORGET_FULL_REST_HOURS), fatigue_span_hours=float(settings.NEVER_FORGET_FATIGUE_SPAN_HOURS),
            min_coverage=float(settings.NEVER_FORGET_MIN_COVERAGE), scope=str(settings.NEVER_FORGET_MATCH_SCOPE),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "gamma": self.gamma, "weights": dict(self.weights), "scales": dict(self.scales), "full_rest_hours": self.full_rest_hours,
                "fatigue_span_hours": self.fatigue_span_hours, "min_coverage": self.min_coverage, "scope": self.scope}


# ================================================================ the situation
@dataclass(frozen=True, slots=True)
class Situation:
    raw: dict[str, float]  # the evidence as read (mm/h, km/h, rest hours, odds ...)
    vector: dict[str, float]  # each feature scaled to [0, 1]; absent: no fresh evidence

    def as_dict(self) -> dict[str, Any]:
        return {"raw": {k: round(v, 4) for k, v in self.raw.items()}, "vector": {k: round(v, 6) for k, v in self.vector.items()}}


def situation(
    policy: NeverForgetPolicy, *, odds: float, indoor: bool | None = None, rain_mmh: float | None = None, wind_kmh: float | None = None,
    rest_hours: float | None = None, cards_per_game: float | None = None, penalties_per_90: float | None = None, steam_against: bool | None = None,
    model_ev: float | None = None, sharp_edge: float | None = None, public_share: float | None = None,
) -> Situation:
    raw: dict[str, float] = {"odds": float(odds)}
    if indoor:
        raw["rain"], raw["wind"] = 0.0, 0.0
    else:
        if rain_mmh is not None:
            raw["rain"] = float(rain_mmh)
        if wind_kmh is not None:
            raw["wind"] = float(wind_kmh)
    for name, value in (("fatigue", rest_hours), ("cards", cards_per_game), ("penalties", penalties_per_90), ("model_ev", model_ev),
                        ("sharp_edge", sharp_edge), ("public", public_share)):
        if value is not None:
            raw[name] = float(value)
    if steam_against is not None:
        raw["steam"] = 1.0 if steam_against else 0.0
    vector: dict[str, float] = {}
    for name, value in raw.items():
        if name == "fatigue":
            vector[name] = _clamp((policy.full_rest_hours - value) / policy.fatigue_span_hours)
        elif name in ("steam", "public"):
            vector[name] = _clamp(value)
        elif name in SCALED:
            scale = policy.scales.get(name)
            if scale is None:
                continue  # no scale configured: recorded raw, never compared
            vector[name] = _clamp((value - 1.0) / scale if name == "odds" else value / scale)
    return Situation(raw, vector)


# ================================================================ comparison
@dataclass(frozen=True, slots=True)
class Comparison:
    similarity: float | None  # None: inconclusive (too little shared evidence)
    coverage: float  # the share of the lesson's feature weight both situations carry
    missing: tuple[str, ...]  # weighted features the lesson has and the candidate lacks


def own_coverage(vector: Mapping[str, float], policy: NeverForgetPolicy) -> float:
    """The share of all feature weight a situation carries (a lesson under ``min_coverage`` cannot be recognised)."""
    total = sum(policy.weights.values())
    return sum(w for k, w in policy.weights.items() if k in vector) / total if total > 0 else 0.0


def compare(candidate: Mapping[str, float], lesson: Mapping[str, float], policy: NeverForgetPolicy) -> Comparison:
    known = {k: w for k, w in policy.weights.items() if k in lesson}
    total = sum(known.values())
    if total <= 0:
        return Comparison(None, 0.0, ())
    shared = {k: w for k, w in known.items() if k in candidate}
    coverage = sum(shared.values()) / total
    missing = tuple(sorted(k for k in known if k not in candidate))
    if coverage < policy.min_coverage - 1e-12:
        return Comparison(None, coverage, missing)
    d2 = sum(w * (candidate[k] - lesson[k]) ** 2 for k, w in shared.items()) / sum(shared.values())
    return Comparison(math.exp(-policy.gamma * d2), coverage, missing)


@dataclass(frozen=True, slots=True)
class Lesson:
    """A memorised lost leg as pillar 15 reads it."""

    rule_id: str
    rule_code: str
    mistake_id: str
    status: str  # ACTIVE vetoes; EXPERIMENTAL is a shadow match
    shape: str
    vector: Mapping[str, float]
    lesson: str


@dataclass(frozen=True, slots=True)
class Match:
    lesson: Lesson
    similarity: float
    coverage: float

    def as_dict(self) -> dict[str, Any]:
        return {"rule_id": self.lesson.rule_id, "rule_code": self.lesson.rule_code, "mistake_id": self.lesson.mistake_id, "status": self.lesson.status,
                "similarity": round(self.similarity, 4), "coverage": round(self.coverage, 4)}


@dataclass(slots=True)
class LegScan:
    vetoes: list[Match] = field(default_factory=list)  # ACTIVE lessons at or above the threshold
    shadow: list[Match] = field(default_factory=list)  # EXPERIMENTAL lessons at or above it
    inconclusive: list[tuple[Lesson, tuple[str, ...]]] = field(default_factory=list)  # ACTIVE lessons it could not be compared with
    closest: Match | None = None
    compared: int = 0


def applies(lesson: Lesson, shape: str, policy: NeverForgetPolicy) -> bool:
    return policy.scope == "any" or lesson.shape == shape


def scan(shape: str, vector: Mapping[str, float], lessons: Iterable[Lesson], policy: NeverForgetPolicy) -> LegScan:
    out = LegScan()
    for lesson in lessons:
        if lesson.status not in (ACTIVE, EXPERIMENTAL) or not applies(lesson, shape, policy):
            continue
        c = compare(vector, lesson.vector, policy)
        if c.similarity is None:
            if lesson.status == ACTIVE:
                out.inconclusive.append((lesson, c.missing))
            continue
        out.compared += 1
        match = Match(lesson, c.similarity, c.coverage)
        if out.closest is None or match.similarity > out.closest.similarity:
            out.closest = match
        if c.similarity >= policy.threshold - 1e-12:
            (out.vetoes if lesson.status == ACTIVE else out.shadow).append(match)
    out.vetoes.sort(key=lambda m: -m.similarity)
    out.shadow.sort(key=lambda m: -m.similarity)
    return out


# ================================================================ specificity
@dataclass(frozen=True, slots=True)
class SeenLeg:
    fixture_id: str
    shape: str
    vector: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Specificity:
    comparable: int  # recent legs of the same shape it could be compared with
    matched: int  # of which it would have vetoed

    @property
    def rate(self) -> float | None:
        return self.matched / self.comparable if self.comparable else None


def specificity(vector: Mapping[str, float], shape: str, fixture_id: str, seen: Iterable[SeenLeg], policy: NeverForgetPolicy) -> Specificity:
    """How many recently vetted legs (other fixtures, the lesson's shape) the lesson would have matched."""
    probe = Lesson("", "", "", ACTIVE, shape, vector, "")
    comparable = matched = 0
    unique: dict[tuple[str, str], Mapping[str, float]] = {}
    for leg in seen:
        if leg.fixture_id != fixture_id and applies(probe, leg.shape, policy):
            unique.setdefault((leg.fixture_id, leg.shape), leg.vector)  # one fixture vetted ten times is one situation
    for other in unique.values():
        c = compare(other, vector, policy)
        if c.similarity is None:
            continue
        comparable += 1
        matched += c.similarity >= policy.threshold - 1e-12
    return Specificity(comparable, matched)


# ================================================================ the lesson in words
_UNITS = {"rain": ("rain", "{:g} mm/h"), "wind": ("wind", "{:g} km/h"), "fatigue": ("rest", "{:g}h"), "cards": ("referee", "{:g} cards a game"),
          "penalties": ("", "{:g} penalties per 90"), "odds": ("odds", "{:g}"), "model_ev": ("model EV", "{:+.1%}"), "sharp_edge": ("edge over the sharp price", "{:+.1%}"),
          "public": ("public", "{:.0%} of tickets")}


def describe(raw: Mapping[str, float]) -> str:
    parts = []
    for name in FEATURES:
        if name not in raw:
            continue
        if name == "steam":
            parts.append("sharp steam against it" if raw[name] >= 0.5 else "no adverse steam")
            continue
        label, fmt = _UNITS[name]
        parts.append(f"{label} {fmt.format(raw[name])}".strip())
    return ", ".join(parts)


def lesson_text(label: str, shape: str, raw: Mapping[str, float], root_cause: str, explanation: str) -> str:
    """The lesson as the journal shows it: what was backed, in what situation, and what the post-mortem found. Every
    number in it is evidence the fortress recorded; nothing is inferred."""
    known = describe(raw) or "no situational evidence recorded"
    return (f"Lost {label} ({shape.replace(':', ' ').lower()}) with {known}. Post-mortem: {root_cause.replace('_', ' ').lower()}: {explanation} "
            f"Never back this shape of bet in this situation again.")[:2000]


def missing_features(vector: Mapping[str, float], policy: NeverForgetPolicy) -> list[str]:
    return [k for k in policy.weights if k not in vector]


def leg_vector_from(metrics: Mapping[str, Any], leg_id: str) -> tuple[str, Situation] | None:
    """(shape, situation) of one leg from a pillar 15 audit record."""
    for row in metrics.get("legs") or []:
        if row.get("leg") == leg_id and isinstance(row.get("vector"), dict):
            return str(row.get("shape", "")), Situation({k: float(v) for k, v in (row.get("raw") or {}).items()}, {k: float(v) for k, v in row["vector"].items()})
    return None


def seen_legs(pillars: Sequence[Mapping[str, Any]]) -> list[SeenLeg]:
    """Every leg's situation in one audit's pillar 15 record."""
    for p in pillars:
        if p.get("number") == 15:
            return [SeenLeg(str(row.get("leg", "")).split("|", 1)[0], str(row.get("shape", "")), {k: float(v) for k, v in (row.get("vector") or {}).items()})
                    for row in (p.get("metrics") or {}).get("legs") or [] if isinstance(row.get("vector"), dict)]
    return []
