"""Omni normalisation: canonical ids, true probabilities, and quorum consensus across providers.

Two halves:

* **Canonical normalisation** (the ingestion fleet): raw provider JSON -> ``MarketTick``. External
  team names map to canonical BetDoc UUIDs through the predefined alias dictionary
  (``app/data/canonical_aliases.json``), every price format becomes a vig-free "true probability",
  and each source's view of a fixture lands on the same canonical match id, so sources can be
  compared and merged.
* **Quorum consensus**: recency/confidence-weighted agreement across providers with variance
  quarantine. Used inline when the live board merges sources (``merge_board_tick``) and on a
  schedule (``omni.run_scheduled_quorum``) to audit and quarantine disagreement.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base_adapter import StandardizedEvent
from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.core.config import Settings
from app.models.omni_vault import OmniQuarantineLog
from app.schemas.aryabhata import BookQuote, MarketQuote
from app.schemas.market import MarketTick, QuorumState
from app.schemas.odds import NormalizedMatchOdds

logger = logging.getLogger(__name__)


class QuorumError(ValueError):
    """Events cannot form a quorum (empty, stale, mixed entities/types)."""


class QuorumVarianceException(Exception):
    def __init__(self, topic: str, variance: float, threshold: float, events: Sequence[StandardizedEvent]) -> None:
        super().__init__(f"Quorum variance {variance:.4%} exceeds {threshold:.4%} for topic '{topic}'.")
        self.topic = topic
        self.variance = variance
        self.threshold = threshold
        self.events = list(events)


@dataclass(frozen=True, slots=True)
class QuorumPolicy:
    variance_threshold: float
    half_life_seconds: float
    max_age_seconds: float
    zero_tolerance: float

    @classmethod
    def from_settings(cls, settings: Settings) -> QuorumPolicy:
        return cls(
            variance_threshold=settings.omni_quorum_variance_threshold,
            half_life_seconds=settings.omni_quorum_half_life_seconds,
            max_age_seconds=settings.omni_quorum_max_age_seconds,
            zero_tolerance=settings.omni_quorum_zero_tolerance,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights, strict=True))
    half = sum(weights) / 2.0
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


IQR_FENCE = 1.5  # Tukey's fences
MIN_IQR_SAMPLE = 4  # quartiles of fewer points say nothing about outliers


def iqr_inliers(values: Sequence[float], tolerance: float) -> list[bool]:
    """Tukey-fence mask (True = keep) for four or more values.

    A value is an outlier only if it is outside ``[Q1 - 1.5 IQR, Q3 + 1.5 IQR]`` AND further than
    ``tolerance`` (relative) from the median, so near-identical sources (IQR ~ 0) never eject a
    neighbour over rounding noise.
    """
    if len(values) < MIN_IQR_SAMPLE:
        return [True] * len(values)
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    spread = q3 - q1
    low, high = q1 - IQR_FENCE * spread, q3 + IQR_FENCE * spread
    median = statistics.median(values)
    slack = tolerance * abs(median)
    return [low <= v <= high or abs(v - median) <= slack for v in values]


@dataclass(frozen=True, slots=True)
class QuorumResult:
    event: StandardizedEvent
    rejected: tuple[StandardizedEvent, ...] = ()  # outliers dropped before consensus


class QuorumConsensusEngine:
    r"""Weight :math:`w_i = c_i \cdot 2^{-\Delta t_i / h}` (confidence x recency half-life).

    With four or more numeric votes, values outside Tukey's IQR fences are rejected first
    (Byzantine tolerance: one stale or broken provider cannot fail the whole fixture), never
    leaving fewer than two voters. Variance is then the maximum relative deviation from the
    weighted median, :math:`\max_i |x_i - \tilde x| / |\tilde x|` (absolute when
    :math:`|\tilde x| \le \epsilon`). Above the threshold, ``QuorumVarianceException`` is raised.
    Otherwise the consensus value is the weighted mean, stamped with the freshest source timestamp.
    """

    def __init__(self, policy: QuorumPolicy, clock: Callable[[], datetime] = _utcnow) -> None:
        self._policy = policy
        self._clock = clock

    @property
    def policy(self) -> QuorumPolicy:
        return self._policy

    def resolve_conflict(self, topic: str, events: list[StandardizedEvent]) -> StandardizedEvent:
        return self.resolve_detailed(topic, events).event

    def resolve_detailed(self, topic: str, events: list[StandardizedEvent]) -> QuorumResult:
        if not events:
            raise QuorumError(f"No events supplied for topic '{topic}'.")
        if any(e.source_timestamp is None for e in events):
            raise QuorumError("Every event must carry source_timestamp (Double-Timestamp Law).")
        if len({e.entity_id for e in events}) != 1:
            raise QuorumError(f"Topic '{topic}' mixes entity_ids.")

        now = self._clock()
        fresh = [e for e in events if (now - e.source_timestamp).total_seconds() <= self._policy.max_age_seconds]  # type: ignore[operator]
        if not fresh:
            raise QuorumError(f"All events for '{topic}' are older than {self._policy.max_age_seconds}s.")

        rejected: list[StandardizedEvent] = []
        if all(isinstance(e.normalized_value, float) for e in fresh):
            keep = iqr_inliers([float(e.normalized_value) for e in fresh], self._policy.variance_threshold)  # type: ignore[arg-type]
            if sum(keep) >= 2 and not all(keep):
                rejected = [e for e, ok in zip(fresh, keep, strict=True) if not ok]
                fresh = [e for e, ok in zip(fresh, keep, strict=True) if ok]

        weights = [self._weight(e, now) for e in fresh]
        if sum(weights) <= 0.0:
            weights = [1.0] * len(fresh)
        winner = max(zip(fresh, weights, strict=True), key=lambda pair: (pair[1], pair[0].source_timestamp))[0]
        freshest_ts = max(e.source_timestamp for e in fresh)  # type: ignore[type-var]

        if all(isinstance(e.normalized_value, float) for e in fresh):
            values = [float(e.normalized_value) for e in fresh]  # type: ignore[arg-type]
            variance = self._variance(values, weights)
            if variance > self._policy.variance_threshold:
                raise QuorumVarianceException(topic, variance, self._policy.variance_threshold, fresh)
            consensus: float | dict[str, float | int | str | bool | None] = sum(
                v * w for v, w in zip(values, weights, strict=True)
            ) / sum(weights)
        elif all(isinstance(e.normalized_value, dict) for e in fresh):
            dicts = [e.normalized_value for e in fresh if isinstance(e.normalized_value, dict)]
            shared = set.intersection(*(set(d) for d in dicts))
            numeric_keys = [k for k in shared if all(isinstance(d[k], float) and not isinstance(d[k], bool) for d in dicts)]
            merged = dict(winner.normalized_value) if isinstance(winner.normalized_value, dict) else {}
            worst = 0.0
            for key in numeric_keys:
                values = [float(d[key]) for d in dicts]  # type: ignore[arg-type]
                worst = max(worst, self._variance(values, weights))
                merged[key] = sum(v * w for v, w in zip(values, weights, strict=True)) / sum(weights)
            if worst > self._policy.variance_threshold:
                raise QuorumVarianceException(topic, worst, self._policy.variance_threshold, fresh)
            variance, consensus = worst, merged
        else:
            raise QuorumError(f"Topic '{topic}' mixes numeric and structured values.")

        mean_confidence = sum(e.confidence_score * w for e, w in zip(fresh, weights, strict=True)) / sum(weights)
        event = winner.model_copy(
            update={
                "normalized_value": consensus,
                "confidence_score": max(0.0, min(1.0, mean_confidence * (1.0 - variance))),
                "source_timestamp": freshest_ts,
                "provider_id": None,  # consensus, not a single provider
            }
        )
        return QuorumResult(event, tuple(rejected))

    def _weight(self, event: StandardizedEvent, now: datetime) -> float:
        age = max((now - event.source_timestamp).total_seconds(), 0.0)  # type: ignore[operator]
        return event.confidence_score * math.pow(0.5, age / self._policy.half_life_seconds)

    def _variance(self, values: Sequence[float], weights: Sequence[float]) -> float:
        if len(values) < 2:
            return 0.0
        center = _weighted_median(values, weights)
        deviation = max(abs(v - center) for v in values)
        if abs(center) <= self._policy.zero_tolerance:
            return deviation
        return deviation / abs(center)


class QuorumService:
    """Runs the engine and routes variance failures to ``OmniQuarantineLog``."""

    def __init__(self, engine: QuorumConsensusEngine) -> None:
        self._engine = engine

    async def resolve(self, session: AsyncSession, topic: str, events: list[StandardizedEvent]) -> StandardizedEvent | None:
        result = await self.resolve_detailed(session, topic, events)
        return result.event if result is not None else None

    async def resolve_detailed(self, session: AsyncSession, topic: str, events: list[StandardizedEvent]) -> QuorumResult | None:
        """Consensus (with any rejected outliers logged for audit), or None after quarantining the topic."""
        try:
            result = self._engine.resolve_detailed(topic, events)
        except QuorumVarianceException as exc:
            logger.warning("Quarantining topic=%s variance=%.4f threshold=%.4f", topic, exc.variance, exc.threshold)
            await self._quarantine(session, topic, "variance_exceeded", exc.variance, exc.threshold, exc.events)
            return None
        except QuorumError as exc:
            logger.warning("Quorum rejected topic=%s: %s", topic, exc)
            await self._quarantine(session, topic, "invalid_quorum", None, None, events)
            return None
        if result.rejected:
            logger.info("Quorum topic=%s rejected %d outlier(s); consensus from the rest", topic, len(result.rejected))
            await self._quarantine(session, topic, "outlier_rejected", None, self._engine.policy.variance_threshold, result.rejected)
        return result

    async def _quarantine(
        self,
        session: AsyncSession,
        topic: str,
        reason: str,
        variance: float | None,
        threshold: float | None,
        events: Sequence[StandardizedEvent],
    ) -> None:
        session.add(
            OmniQuarantineLog(
                topic=topic,
                reason=reason,
                variance=variance,
                threshold=threshold,
                events=[e.model_dump(mode="json") for e in events],
            )
        )
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            logger.exception("Failed to persist quarantine record for topic=%s", topic)
            raise


# =============================================================================================
# Canonical normalisation (ingestion fleet)
# =============================================================================================
ALIAS_DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "data" / "canonical_aliases.json"
# Club-name affixes providers add or drop ("Arsenal FC" / "Arsenal", "AFC Bournemouth" / "Bournemouth")
_AFFIXES = frozenset({"fc", "afc", "cf", "sc", "ac", "the"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_VERSUS = re.compile(r"\s+(?:vs?\.?|@)\s+", re.IGNORECASE)

HOME, DRAW, AWAY = "HOME", "DRAW", "AWAY"
OddsFormat = Literal["decimal", "american", "fractional", "probability"]


def normalize_name(name: str) -> str:
    """Comparison form of a name: accents stripped, case-folded, ``&`` -> ``and``, punctuation dropped."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold().replace("&", " and ")
    return " ".join(_NON_ALNUM.sub(" ", folded).split())


def _core_name(normalized: str) -> str:
    tokens = [token for token in normalized.split() if token not in _AFFIXES]
    return " ".join(tokens) or normalized


@dataclass(frozen=True, slots=True)
class CanonicalRef:
    id: UUID
    kind: str
    sport: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SportSpec:
    key: str
    name: str
    polymarket: str | None
    has_draw: bool


@dataclass(frozen=True, slots=True)
class ResolvedName:
    id: UUID
    name: str
    canonical: bool  # False: not in the dictionary; ``id`` is provisional (stable, but not a DB row)
    raw: str


class AliasDictionary:
    """External spelling -> canonical entity, per sport. Never guesses: an alias that two entities
    of the same sport share resolves to neither."""

    def __init__(self, namespace: UUID, sports: Mapping[str, SportSpec], entities: Sequence[CanonicalRef]) -> None:
        self.namespace = namespace
        self.sports: dict[str, SportSpec] = dict(sports)
        self.entities: tuple[CanonicalRef, ...] = tuple(entities)
        self._by_polymarket = {spec.polymarket: spec for spec in self.sports.values() if spec.polymarket}
        self._exact: dict[tuple[str, str], CanonicalRef | None] = {}
        self._core: dict[tuple[str, str], CanonicalRef | None] = {}
        for ref in self.entities:
            for spelling in (ref.name, *ref.aliases):
                normalized = normalize_name(spelling)
                self._claim(self._exact, (ref.sport, normalized), ref)
                self._claim(self._core, (ref.sport, _core_name(normalized)), ref)

    @staticmethod
    def _claim(index: dict[tuple[str, str], CanonicalRef | None], slot: tuple[str, str], ref: CanonicalRef) -> None:
        if slot not in index:
            index[slot] = ref
        elif index[slot] is not None and index[slot] != ref:
            index[slot] = None  # ambiguous

    @classmethod
    def from_file(cls, path: Path) -> AliasDictionary:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        sports = {
            key: SportSpec(key=key, name=spec["name"], polymarket=spec.get("polymarket"), has_draw=bool(spec.get("has_draw")))
            for key, spec in doc["sports"].items()
        }
        entities = [
            CanonicalRef(
                id=UUID(row["id"]), kind=row["kind"], sport=row["sport"], name=row["name"], aliases=tuple(row.get("aliases", ()))
            )
            for row in doc["entities"]
        ]
        return cls(UUID(doc["namespace"]), sports, entities)

    def lookup(self, sport: str, raw: str) -> CanonicalRef | None:
        normalized = normalize_name(raw)
        return self._exact.get((sport, normalized)) or self._core.get((sport, _core_name(normalized)))

    def resolve(self, sport: str, raw: str) -> ResolvedName:
        ref = self.lookup(sport, raw)
        if ref is not None:
            return ResolvedName(ref.id, ref.name, True, raw)
        provisional = uuid5(self.namespace, f"{sport}:unmapped:{_core_name(normalize_name(raw))}")
        return ResolvedName(provisional, raw.strip()[:128] or "Unknown", False, raw)

    def sport_for_polymarket(self, league: str) -> SportSpec | None:
        return self._by_polymarket.get(league.lower())

    def match_id(self, sport: str, home: UUID, away: UUID, kickoff: datetime) -> str:
        """The same fixture gets the same id from every source: sport, both canonical sides, UTC date."""
        day = kickoff.astimezone(UTC).date().isoformat()
        return str(uuid5(self.namespace, f"{sport}:match:{home}:{away}:{day}"))


@lru_cache(maxsize=1)
def default_alias_dictionary() -> AliasDictionary:
    return AliasDictionary.from_file(ALIAS_DICTIONARY_PATH)


# ---------------------------------------------------------------- price -> probability
def implied_probability(price: float | str, fmt: OddsFormat = "decimal") -> float:
    """Vig-inclusive probability implied by one quoted price. Raises ValueError for impossible prices."""
    try:
        if fmt == "fractional":
            numerator, _, denominator = str(price).partition("/")
            probability = 1.0 / (1.0 + float(numerator) / float(denominator or 1))
        else:
            value = float(price)
            if fmt == "decimal":
                probability = 1.0 / value if value > 1.0 else math.nan
            elif fmt == "american":
                if -100.0 < value < 100.0:
                    probability = math.nan  # American prices are <= -100 or >= +100
                else:
                    probability = 100.0 / (value + 100.0) if value > 0 else -value / (-value + 100.0)
            else:
                probability = value
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Unparseable {fmt} price {price!r}") from exc
    if not math.isfinite(probability) or not 0.0 < probability < 1.0:
        raise ValueError(f"{fmt} price {price!r} implies no valid probability")
    return probability


DevigMethod = Literal["shin", "multiplicative"]


def remove_vig(implied: Sequence[float]) -> list[float]:
    """Proportional (multiplicative) de-vig: scale a complete book so it sums to exactly 1."""
    total = math.fsum(implied)
    if not implied or not math.isfinite(total) or total <= 0.0:
        raise ValueError("Cannot de-vig an empty or invalid book")
    return [p / total for p in implied]


def shin_devig(implied: Sequence[float], tolerance: float = 1e-12, max_iterations: int = 200) -> list[float]:
    r"""Shin (1993): fair probabilities when a share :math:`z` of turnover is insider money.

    :math:`p_i(z) = \frac{\sqrt{z^2 + 4(1-z) q_i^2 / B} - z}{2(1-z)}` with :math:`B = \sum q_i`.
    :math:`\sum p_i(0) = \sqrt{B} > 1` and the sum falls as :math:`z` grows, so bisection finds the
    unique :math:`z` with :math:`\sum p_i = 1`. Unlike the multiplicative method it shaves more
    margin off longshots than favourites (the favourite-longshot bias). It needs prices only.
    Raises ValueError when its preconditions fail; ``devig`` then falls back.
    """
    if len(implied) < 2 or not all(math.isfinite(q) and 0.0 < q < 1.0 for q in implied):
        raise ValueError("Shin needs a complete book of valid implied probabilities")
    booksum = math.fsum(implied)
    if booksum <= 1.0:
        raise ValueError("Shin needs an overround (book sum > 1)")

    def fair(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * q * q / booksum) - z) / (2.0 * (1.0 - z)) for q in implied]

    low, high = 0.0, 0.999
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        if math.fsum(fair(mid)) > 1.0:
            low = mid
        else:
            high = mid
        if high - low < tolerance:
            break
    probabilities = fair((low + high) / 2.0)
    total = math.fsum(probabilities)
    if not math.isfinite(total) or abs(total - 1.0) > 1e-6:
        raise ValueError("Shin did not converge")
    return [p / total for p in probabilities]


def devig(implied: Sequence[float], method: DevigMethod = "shin") -> tuple[list[float], DevigMethod]:
    """Fair probabilities and the method that produced them. Shin degrades to multiplicative
    (never raises for a usable book): no overround, an arbitrage book, or non-convergence."""
    if method == "shin":
        try:
            return shin_devig(implied), "shin"
        except ValueError:
            pass
    return remove_vig(implied), "multiplicative"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", text):  # Polymarket's "2026-10-10 11:30:00+00"
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _number(value: object, default: float = 0.0) -> float:
    """Optional numeric metadata (liquidity, spread): absent or garbled degrades to ``default``."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0.0 else default


def _probability(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


# ---------------------------------------------------------------- normaliser
@dataclass(slots=True)
class NormalizationReport:
    source_id: str
    ticks: list[MarketTick] = field(default_factory=list)
    quotes: list[MarketQuote] = field(default_factory=list)  # every book's prices, for the Aryabhata engine
    events_seen: int = 0
    events_normalized: int = 0
    malformed: int = 0  # events that raised while parsing: isolated, the rest of the batch still lands
    unmapped: set[str] = field(default_factory=set)  # external names missing from the alias dictionary
    devig_methods: Counter[str] = field(default_factory=Counter)  # books de-vigged per method

    @property
    def coverage(self) -> float | None:
        return self.events_normalized / self.events_seen if self.events_seen else None


# A provider changing its data structure surfaces as these while parsing one event
_EVENT_ERRORS = (TypeError, ValueError, KeyError, AttributeError, IndexError)


@dataclass(frozen=True, slots=True)
class _Quote:
    fair_input: float  # the source's probability for the outcome, before de-vig
    price: float  # best decimal price on offer


@dataclass(frozen=True, slots=True)
class RawFixture:
    """One fixture as a bookmaker-style source quotes it, before canonical mapping.

    ``books`` holds one ``{HOME|DRAW|AWAY: decimal price}`` mapping per bookmaker (a single-book
    source has one); ``bookmakers`` names them in the same order (empty or "" = the source itself).
    Produced by the Odds API parser and by every config-driven provider.
    """

    sport: str
    event_id: str
    home: str
    away: str
    kickoff: datetime
    books: tuple[Mapping[str, float], ...]
    observed_at: datetime | None = None
    suspended: bool = False
    bookmakers: tuple[str, ...] = ()


FixtureMapper = Callable[[SourcePayload], Sequence[RawFixture | None]]


def same_side(aliases: AliasDictionary, sport: str, a: str, b: str) -> bool:
    """Do two spellings name the same team? Exact, affix-insensitive, or the same canonical entity."""
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb or _core_name(na) == _core_name(nb):
        return True
    ra, rb = aliases.lookup(sport, a), aliases.lookup(sport, b)
    return ra is not None and ra == rb


class OmniNormalizer:
    """Raw fleet JSON -> canonical ``MarketTick``s, one per selection of each fixture.

    Malformed events are isolated: a provider that changes one field breaks that event, not the
    batch (``report.malformed`` counts them; a batch where nothing parses is schema drift).
    """

    def __init__(self, aliases: AliasDictionary | None = None, devig_method: DevigMethod = "shin") -> None:
        self._aliases = aliases or default_alias_dictionary()
        self._devig = devig_method

    @property
    def aliases(self) -> AliasDictionary:
        return self._aliases

    def normalize(self, batch: IngestionBatch, mapper: FixtureMapper | None = None, devig_method: DevigMethod | None = None) -> NormalizationReport:
        """``mapper`` turns a payload into fixtures for config-driven providers; built-ins need none."""
        report = NormalizationReport(batch.source_id)
        method = devig_method or self._devig
        if mapper is not None:
            for payload in batch.payloads:
                try:
                    fixtures = mapper(payload)
                except _EVENT_ERRORS:
                    report.malformed += 1
                    continue
                for fixture in fixtures:
                    if fixture is None:  # the mapper could not parse this event
                        report.events_seen += 1
                        report.malformed += 1
                        continue
                    self.emit_fixture(report, fixture, batch.fetched_at, method)
            return report

        handlers: dict[str, Callable[[SourcePayload, IngestionBatch, NormalizationReport, DevigMethod], None]] = {
            "odds_api": self._odds_api,
            "polymarket": self._polymarket,
        }
        handler = handlers.get(batch.source_id)
        if handler is None:
            raise ValueError(f"No normaliser for source '{batch.source_id}'")
        for payload in batch.payloads:
            handler(payload, batch, report, method)
        return report

    def emit_fixture(self, report: NormalizationReport, fixture: RawFixture, fetched_at: datetime, method: DevigMethod) -> None:
        """De-vig every book, take the median fair probability and the best price per selection."""
        report.events_seen += 1
        try:
            self._emit_books(report, fixture, fetched_at, method)
        except _EVENT_ERRORS:
            report.malformed += 1

    def _emit_books(self, report: NormalizationReport, fixture: RawFixture, fetched_at: datetime, method: DevigMethod) -> None:
        books = [b for b in fixture.books if len(b) >= 2]
        if not books:
            return
        shape = Counter(frozenset(b) for b in books).most_common(1)[0][0]  # 2-way or 3-way: what most books quote
        labels = [label for label in (HOME, DRAW, AWAY) if label in shape]
        if HOME not in shape or AWAY not in shape:
            return
        fair_by_label: dict[str, list[float]] = defaultdict(list)
        best_price: dict[str, float] = {}
        priced_books = 0
        for book in books:
            if frozenset(book) != shape:
                continue
            try:
                fair, used = devig([implied_probability(book[label]) for label in labels], method)
            except ValueError:
                continue
            priced_books += 1
            report.devig_methods[used] += 1
            for label, probability in zip(labels, fair, strict=True):
                fair_by_label[label].append(probability)
                best_price[label] = max(best_price.get(label, 0.0), float(book[label]))
        if priced_books == 0:
            return
        # Median of each book's de-vigged probability: one stale or off-market book can't drag it
        quotes = {label: _Quote(statistics.median(fair_by_label[label]), best_price[label]) for label in labels}
        self._emit(
            report,
            sport=fixture.sport,
            home=self._side(fixture.sport, fixture.home, report),
            away=self._side(fixture.sport, fixture.away, report),
            kickoff=fixture.kickoff.astimezone(UTC),
            quotes=quotes,
            suspended=fixture.suspended,
            # More books agreeing on the median -> a firmer number
            confidence=min(0.95, 0.55 + 0.04 * priced_books),
            observed_at=(fixture.observed_at or fetched_at).astimezone(UTC),
            source_event_id=fixture.event_id,
            fetched_at=fetched_at,
            books=[(self._bookmaker(fixture, index, report.source_id), book) for index, book in enumerate(fixture.books) if len(book) >= 2],
        )

    # ---------------------------------------------------------------- shared
    @staticmethod
    def _bookmaker(fixture: RawFixture, index: int, source_id: str) -> str:
        named = fixture.bookmakers[index].strip() if index < len(fixture.bookmakers) else ""
        if named:
            return named[:64]
        return source_id if len(fixture.books) == 1 else f"{source_id}_{index + 1}"

    def _side(self, sport: str, raw: str, report: NormalizationReport) -> ResolvedName:
        resolved = self._aliases.resolve(sport, raw)
        if not resolved.canonical:
            report.unmapped.add(f"{sport}: {raw.strip()}")
        return resolved

    def _emit(
        self,
        report: NormalizationReport,
        *,
        sport: str,
        home: ResolvedName,
        away: ResolvedName,
        kickoff: datetime,
        quotes: Mapping[str, _Quote],
        suspended: bool,
        confidence: float,
        observed_at: datetime,
        source_event_id: str,
        fetched_at: datetime,
        books: Sequence[tuple[str, Mapping[str, float]]] = (),
    ) -> None:
        labels = [label for label in (HOME, DRAW, AWAY) if label in quotes]
        fair = remove_vig([quotes[label].fair_input for label in labels])
        match_id = self._aliases.match_id(sport, home.id, away.id, kickoff)
        for label, probability in zip(labels, fair, strict=True):
            report.ticks.append(
                MarketTick(
                    match_id=match_id,
                    home_team=home.name,
                    away_team=away.name,
                    market_type="Match Odds",
                    selection=label,
                    odds=Decimal(str(round(quotes[label].price, 4))),
                    true_probability=Decimal(str(round(probability, 6))),
                    is_suspended=suspended,
                    sport_key=sport,
                    home_team_id=home.id,
                    away_team_id=away.id,
                    commence_time=kickoff,
                    source=report.source_id,
                    sources=(report.source_id,),
                    source_event_id=source_event_id[:128],
                    confidence=round(min(max(confidence, 0.0), 1.0), 4),
                    observed_at=observed_at,
                    quorum="single_source",
                )
            )
        self._quote(report, sport, home, away, kickoff, match_id, fetched_at, observed_at, suspended, books)
        report.events_normalized += 1

    @staticmethod
    def _quote(
        report: NormalizationReport,
        sport: str,
        home: ResolvedName,
        away: ResolvedName,
        kickoff: datetime,
        match_id: str,
        fetched_at: datetime,
        observed_at: datetime,
        suspended: bool,
        books: Sequence[tuple[str, Mapping[str, float]]],
    ) -> None:
        """The same fixture as one Aryabhata frame: every book's own prices, not just the best."""
        quoted: list[BookQuote] = []
        for bookmaker, prices in books:
            clean = {label: Decimal(str(price)) for label, price in prices.items() if math.isfinite(price) and price > 1.0}
            if len(clean) == len(prices) >= 2:
                quoted.append(BookQuote(bookmaker_id=bookmaker, prices=clean, observed_at=observed_at, is_suspended=suspended))
        if quoted:
            report.quotes.append(
                MarketQuote(
                    match_id=match_id,
                    market_type="Match Odds",
                    home_team=home.name,
                    away_team=away.name,
                    sport_key=sport,
                    commence_time=kickoff,
                    source=report.source_id,
                    fetched_at=fetched_at.astimezone(UTC),
                    books=tuple(quoted),
                )
            )

    # ---------------------------------------------------------------- The Odds API
    def _odds_api(self, payload: SourcePayload, batch: IngestionBatch, report: NormalizationReport, method: DevigMethod) -> None:
        for raw_event in payload.data if isinstance(payload.data, list) else []:
            try:
                event = NormalizedMatchOdds.model_validate(raw_event)
            except ValidationError:
                report.events_seen += 1
                report.malformed += 1
                continue
            books: list[dict[str, float]] = []
            names: list[str] = []
            latest: datetime | None = None
            for bookmaker in event.bookmakers:
                market = next((m for m in bookmaker.markets if m.key == "h2h"), None)
                if market is None:
                    continue
                priced: dict[str, float] = {}
                for outcome in market.selections:
                    label = (
                        HOME if outcome.name == event.home_team
                        else AWAY if outcome.name == event.away_team
                        else DRAW if outcome.name.casefold() == "draw"
                        else None
                    )
                    if label is None or not math.isfinite(outcome.price) or outcome.price <= 1.0:
                        priced = {}
                        break
                    priced[label] = outcome.price
                if len(priced) < 2:
                    continue
                books.append(priced)
                names.append(bookmaker.key)
                stamp = market.last_update or bookmaker.last_update
                if latest is None or stamp > latest:
                    latest = stamp
            fixture = RawFixture(
                sport=event.sport_key or payload.key,
                event_id=event.id,
                home=event.home_team,
                away=event.away_team,
                kickoff=event.commence_time,
                books=tuple(books),
                observed_at=latest,
                bookmakers=tuple(names),
            )
            self.emit_fixture(report, fixture, batch.fetched_at, method)
            try:
                self._side_markets(report, event, fixture.sport, batch.fetched_at)
            except _EVENT_ERRORS:
                report.malformed += 1

    def _side_markets(self, report: NormalizationReport, event: NormalizedMatchOdds, sport: str, fetched_at: datetime) -> None:
        """Totals, Asian handicaps and both-teams-to-score, as Aryabhata frames under Ashoka's canonical
        market names (``"Totals 2.5"``, ``"Asian Handicap -0.5"``: the home line, ``"BTTS"``).

        One frame per market and line, every book's prices kept. Only complete two-way prices count: a
        book quoting one side of a line, or a handicap whose sides do not mirror, is left out."""
        grouped: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
        seen: dict[str, datetime] = {}
        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                for market_type, prices in self._two_way(market.key, market.selections, event.home_team, event.away_team):
                    grouped[market_type][bookmaker.key] = prices
                    stamp = market.last_update or bookmaker.last_update
                    if market_type not in seen or stamp > seen[market_type]:
                        seen[market_type] = stamp
        if not grouped:
            return
        home, away = self._side(sport, event.home_team, report), self._side(sport, event.away_team, report)
        kickoff = event.commence_time.astimezone(UTC)
        match_id = self._aliases.match_id(sport, home.id, away.id, kickoff)
        for market_type, books in grouped.items():
            observed = (seen.get(market_type) or fetched_at).astimezone(UTC)
            quoted = tuple(
                BookQuote(bookmaker_id=key[:64], prices={label: Decimal(str(price)) for label, price in prices.items()}, observed_at=observed)
                for key, prices in books.items()
            )
            report.quotes.append(
                MarketQuote(
                    match_id=match_id, market_type=market_type, home_team=home.name, away_team=away.name, sport_key=sport,
                    commence_time=kickoff, source=report.source_id, fetched_at=fetched_at.astimezone(UTC), books=quoted,
                )
            )

    @staticmethod
    def _two_way(key: str, outcomes: Sequence[Any], home_team: str, away_team: str) -> list[tuple[str, dict[str, float]]]:
        """(canonical market type, prices) for one book's totals / spreads / btts market."""
        def valid(price: float) -> bool:
            return math.isfinite(price) and price > 1.0

        if key == "btts":
            prices = {o.name.strip().upper(): float(o.price) for o in outcomes if o.name.strip().upper() in ("YES", "NO") and valid(float(o.price))}
            return [("BTTS", prices)] if len(prices) == 2 else []
        if key == "totals":
            lines: dict[float, dict[str, float]] = defaultdict(dict)
            for o in outcomes:
                side = o.name.strip().upper()
                if side in ("OVER", "UNDER") and o.point is not None and valid(float(o.price)):
                    lines[float(o.point)][side] = float(o.price)
            return [(f"Totals {point:g}", prices) for point, prices in lines.items() if len(prices) == 2 and point >= 0 and abs(point * 4 - round(point * 4)) < 1e-9]
        if key == "spreads":
            home = next((o for o in outcomes if o.name == home_team and o.point is not None), None)
            away = next((o for o in outcomes if o.name == away_team and o.point is not None), None)
            if home is None or away is None or abs(float(home.point) + float(away.point)) > 1e-9 or abs(float(home.point) * 4 - round(float(home.point) * 4)) > 1e-9:
                return []
            if not (valid(float(home.price)) and valid(float(away.price))):
                return []
            return [(f"Asian Handicap {float(home.point):+g}", {"HOME": float(home.price), "AWAY": float(away.price)})]
        return []

    # ---------------------------------------------------------------- Polymarket
    def _polymarket(self, payload: SourcePayload, batch: IngestionBatch, report: NormalizationReport, method: DevigMethod) -> None:  # noqa: ARG002 - exchange mids are already fair-ish: multiplicative only
        data = payload.data if isinstance(payload.data, dict) else {}
        league = str(data.get("league") or payload.key)
        spec = self._aliases.sport_for_polymarket(league)
        sport = spec.key if spec else f"polymarket_{league}"
        home_first = data.get("ordering", "home") != "away"

        events: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for market in data.get("markets") or []:
            if not isinstance(market, dict) or not market.get("gameStartTime"):
                continue  # futures and season-series markets have no game time
            parents = market.get("events") or []
            event = parents[0] if parents and isinstance(parents[0], dict) else None
            if event is None or event.get("id") is None:
                continue
            events.setdefault(str(event["id"]), (event, []))[1].append(market)

        for event_id, (event, markets) in events.items():
            report.events_seen += 1
            try:
                self._polymarket_event(report, batch, sport, spec, home_first, event_id, event, markets)
            except _EVENT_ERRORS:
                report.malformed += 1

    def _polymarket_event(
        self,
        report: NormalizationReport,
        batch: IngestionBatch,
        sport: str,
        spec: SportSpec | None,
        home_first: bool,
        event_id: str,
        event: dict[str, Any],
        markets: list[dict[str, Any]],
    ) -> None:
        sides = _VERSUS.split(str(event.get("title") or ""), maxsplit=1)
        kickoff = _parse_time(markets[0].get("gameStartTime")) or _parse_time(event.get("startTime"))
        if len(sides) != 2 or kickoff is None:
            return
        # Polymarket titles list the home side first for some leagues and the away side for others
        first, second = sides[0].strip(), sides[1].strip()
        home_raw, away_raw = (first, second) if home_first else (second, first)
        quotes = self._polymarket_quotes(sport, markets, home_raw, away_raw)
        if HOME not in quotes or AWAY not in quotes:
            return
        if spec is not None and spec.has_draw != (DRAW in quotes):
            return  # incomplete book: a three-way sport quoted without the draw (or vice versa)
        liquidity = min((_number(m.get("liquidityNum")) for m in markets), default=0.0)
        spread = max((_number(m.get("spread")) for m in markets), default=0.0)
        stamps = [stamp for m in markets if (stamp := _parse_time(m.get("updatedAt")))]
        report.devig_methods["multiplicative"] += 1  # exchange mids: no bookmaker margin to model
        self._emit(
            report,
            sport=sport,
            home=self._side(sport, home_raw, report),
            away=self._side(sport, away_raw, report),
            kickoff=kickoff,
            quotes=quotes,
            suspended=any(m.get("closed") or not m.get("acceptingOrders", True) for m in markets),
            # Deep, tight books are worth more than thin ones; missing depth data just lowers trust
            confidence=min(0.95, max(0.2, 0.35 + 0.1 * math.log10(1.0 + liquidity) - 2.0 * spread)),
            observed_at=max(stamps) if stamps else batch.fetched_at,
            source_event_id=event_id,
            fetched_at=batch.fetched_at,
            books=[(report.source_id, {label: quote.price for label, quote in quotes.items()})],
        )

    def _same_side(self, sport: str, a: str, b: str) -> bool:
        return same_side(self._aliases, sport, a, b)

    def _polymarket_quotes(self, sport: str, markets: list[dict[str, Any]], home_raw: str, away_raw: str) -> dict[str, _Quote]:
        quotes: dict[str, _Quote] = {}
        for market in markets:
            try:
                outcomes = [str(o) for o in json.loads(market.get("outcomes") or "[]")]
                prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
            except (TypeError, ValueError):
                continue
            if len(outcomes) != 2:
                continue
            if [o.casefold() for o in outcomes] == ["yes", "no"]:
                # Soccer: one Yes/No market per outcome; groupItemTitle names the outcome
                title = str(market.get("groupItemTitle") or "")
                label = (
                    DRAW if title.casefold().startswith("draw")
                    else HOME if self._same_side(sport, title, home_raw)
                    else AWAY if self._same_side(sport, title, away_raw)
                    else None
                )
                quote = _token_quote(market, prices, 0)
                if label is not None and quote is not None:
                    quotes[label] = quote
            else:
                # Two-way sports: one market whose two outcomes are the two sides
                for index, outcome in enumerate(outcomes):
                    side = (
                        HOME if self._same_side(sport, outcome, home_raw)
                        else AWAY if self._same_side(sport, outcome, away_raw)
                        else None
                    )
                    quote = _token_quote(market, prices, index)
                    if side is not None and quote is not None:
                        quotes.setdefault(side, quote)
        return quotes


def _token_quote(market: Mapping[str, Any], prices: Sequence[float], index: int) -> _Quote | None:
    """Mid and buy price for outcome ``index``. bestBid/bestAsk quote outcome 0; outcome 1 is its complement."""
    bid, ask = _probability(market.get("bestBid")), _probability(market.get("bestAsk"))
    if index == 1 and bid is not None and ask is not None:
        bid, ask = 1.0 - ask, 1.0 - bid
    if bid is not None and ask is not None and bid <= ask and ask - bid <= 0.10:
        mid: float | None = (bid + ask) / 2.0
    else:
        mid = _probability(prices[index]) if index < len(prices) else None
    if mid is None or not 0.0 < mid < 1.0:
        return None
    buy = ask if ask is not None and 0.0 < ask < 1.0 else mid
    return _Quote(fair_input=mid, price=1.0 / buy)


# ---------------------------------------------------------------- cross-source board merge
def source_provider_id(source: str) -> UUID:
    """Stable provider id for a fleet source, so quorum events carry one vote per source."""
    return uuid5(NAMESPACE_URL, f"betdoc:fleet:{source}")


def tick_to_event(tick: MarketTick, now: datetime) -> StandardizedEvent:
    return StandardizedEvent(
        entity_id=tick.board_key,
        event_type="market.true_probability",
        normalized_value=float(tick.true_probability),
        confidence_score=tick.confidence if tick.confidence is not None else 0.5,
        source_timestamp=tick.observed_at or now,
        provider_id=source_provider_id(tick.source or "unknown"),
    )


def merge_board_tick(source_ticks: Sequence[MarketTick], engine: QuorumConsensusEngine, now: datetime | None = None) -> MarketTick:
    """One board cell from every source's latest view of it.

    ``odds`` is the best tradable price on offer (what a bettor can actually get). ``true_probability``
    is the quorum consensus when two or more fresh sources agree, and is flagged ``quarantined``
    (with the confidence-weighted median used) when they disagree beyond the variance threshold.
    """
    if not source_ticks:
        raise ValueError("merge_board_tick needs at least one tick")
    now = now or _utcnow()
    newest = max(source_ticks, key=lambda t: t.observed_at or now)
    max_age = engine.policy.max_age_seconds
    fresh = [t for t in source_ticks if (now - (t.observed_at or now)).total_seconds() <= max_age] or [newest]
    if len(fresh) == 1:
        only = fresh[0]
        return only.model_copy(update={"sources": (only.source,) if only.source else (), "quorum": "single_source"})

    tradable = [t for t in fresh if not t.is_suspended] or fresh
    best = max(tradable, key=lambda t: t.odds)
    events = [tick_to_event(t, now) for t in fresh]
    state: QuorumState
    try:
        consensus = engine.resolve_conflict(best.board_key, events)
        probability, state, confidence = float(consensus.normalized_value), "consensus", consensus.confidence_score  # type: ignore[arg-type]
    except QuorumVarianceException:
        values = [float(e.normalized_value) for e in events]  # type: ignore[arg-type]
        probability = _weighted_median(values, [e.confidence_score for e in events])
        state, confidence = "quarantined", min(e.confidence_score for e in events) / 2.0
    except QuorumError:
        return newest.model_copy(update={"sources": (newest.source,) if newest.source else (), "quorum": "single_source"})

    return best.model_copy(
        update={
            "true_probability": Decimal(str(round(probability, 6))),
            "quorum": state,
            "sources": tuple(sorted({t.source for t in fresh if t.source})),
            "confidence": round(confidence, 4),
            "observed_at": max(t.observed_at or now for t in fresh),
            "is_suspended": all(t.is_suspended for t in fresh),
        }
    )
