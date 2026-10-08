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


class QuorumConsensusEngine:
    r"""Weight :math:`w_i = c_i \cdot 2^{-\Delta t_i / h}` (confidence x recency half-life).

    Variance is the maximum relative deviation from the weighted median,
    :math:`\max_i |x_i - \tilde x| / |\tilde x|` (absolute when :math:`|\tilde x| \le \epsilon`).
    Above the threshold, ``QuorumVarianceException`` is raised. Otherwise the consensus value is the
    weighted mean, stamped with the freshest source timestamp.
    """

    def __init__(self, policy: QuorumPolicy, clock: Callable[[], datetime] = _utcnow) -> None:
        self._policy = policy
        self._clock = clock

    @property
    def policy(self) -> QuorumPolicy:
        return self._policy

    def resolve_conflict(self, topic: str, events: list[StandardizedEvent]) -> StandardizedEvent:
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
        return winner.model_copy(
            update={
                "normalized_value": consensus,
                "confidence_score": max(0.0, min(1.0, mean_confidence * (1.0 - variance))),
                "source_timestamp": freshest_ts,
                "provider_id": None,  # consensus, not a single provider
            }
        )

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
        try:
            return self._engine.resolve_conflict(topic, events)
        except QuorumVarianceException as exc:
            logger.warning("Quarantining topic=%s variance=%.4f threshold=%.4f", topic, exc.variance, exc.threshold)
            await self._quarantine(session, topic, "variance_exceeded", exc.variance, exc.threshold, exc.events)
        except QuorumError as exc:
            logger.warning("Quorum rejected topic=%s: %s", topic, exc)
            await self._quarantine(session, topic, "invalid_quorum", None, None, events)
        return None

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


def remove_vig(implied: Sequence[float]) -> list[float]:
    """Proportional de-vig: scale a complete book (every outcome) so it sums to exactly 1."""
    total = math.fsum(implied)
    if not implied or not math.isfinite(total) or total <= 0.0:
        raise ValueError("Cannot de-vig an empty or invalid book")
    return [p / total for p in implied]


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
    events_seen: int = 0
    events_normalized: int = 0
    unmapped: set[str] = field(default_factory=set)  # external names missing from the alias dictionary

    @property
    def coverage(self) -> float | None:
        return self.events_normalized / self.events_seen if self.events_seen else None


@dataclass(frozen=True, slots=True)
class _Quote:
    fair_input: float  # the source's probability for the outcome, before de-vig
    price: float  # best decimal price on offer


class OmniNormalizer:
    """Raw fleet JSON -> canonical ``MarketTick``s, one per selection of each fixture."""

    def __init__(self, aliases: AliasDictionary | None = None) -> None:
        self._aliases = aliases or default_alias_dictionary()

    @property
    def aliases(self) -> AliasDictionary:
        return self._aliases

    def normalize(self, batch: IngestionBatch) -> NormalizationReport:
        handlers: dict[str, Callable[[SourcePayload, IngestionBatch, NormalizationReport], None]] = {
            "odds_api": self._odds_api,
            "polymarket": self._polymarket,
        }
        handler = handlers.get(batch.source_id)
        if handler is None:
            raise ValueError(f"No normaliser for source '{batch.source_id}'")
        report = NormalizationReport(batch.source_id)
        for payload in batch.payloads:
            handler(payload, batch, report)
        return report

    # ---------------------------------------------------------------- shared
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
        report.events_normalized += 1

    # ---------------------------------------------------------------- The Odds API
    def _odds_api(self, payload: SourcePayload, batch: IngestionBatch, report: NormalizationReport) -> None:
        for raw_event in payload.data if isinstance(payload.data, list) else []:
            report.events_seen += 1
            try:
                event = NormalizedMatchOdds.model_validate(raw_event)
            except ValidationError:
                continue
            sport = event.sport_key or payload.key
            per_book: list[dict[str, float]] = []
            shapes: Counter[frozenset[str]] = Counter()
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
                per_book.append(priced)
                shapes[frozenset(priced)] += 1
                stamp = market.last_update or bookmaker.last_update
                if latest is None or stamp > latest:
                    latest = stamp
            if not per_book:
                continue
            shape = shapes.most_common(1)[0][0]  # 2-way or 3-way: whatever most books quote
            labels = [label for label in (HOME, DRAW, AWAY) if label in shape]
            fair_by_label: dict[str, list[float]] = defaultdict(list)
            best_price: dict[str, float] = {}
            books = 0
            for priced in per_book:
                if frozenset(priced) != shape:
                    continue
                try:
                    fair = remove_vig([implied_probability(priced[label]) for label in labels])
                except ValueError:
                    continue
                books += 1
                for label, probability in zip(labels, fair, strict=True):
                    fair_by_label[label].append(probability)
                    best_price[label] = max(best_price.get(label, 0.0), priced[label])
            if books == 0:
                continue
            # Median of each book's de-vigged probability: one stale or off-market book can't drag it
            quotes = {label: _Quote(statistics.median(fair_by_label[label]), best_price[label]) for label in labels}
            self._emit(
                report,
                sport=sport,
                home=self._side(sport, event.home_team, report),
                away=self._side(sport, event.away_team, report),
                kickoff=event.commence_time.astimezone(UTC),
                quotes=quotes,
                suspended=False,
                # More books agreeing on the median -> a firmer number
                confidence=min(0.95, 0.55 + 0.04 * books),
                observed_at=(latest or batch.fetched_at).astimezone(UTC),
                source_event_id=event.id,
            )

    # ---------------------------------------------------------------- Polymarket
    def _polymarket(self, payload: SourcePayload, batch: IngestionBatch, report: NormalizationReport) -> None:
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
            sides = _VERSUS.split(str(event.get("title") or ""), maxsplit=1)
            kickoff = _parse_time(markets[0].get("gameStartTime")) or _parse_time(event.get("startTime"))
            if len(sides) != 2 or kickoff is None:
                continue
            # Polymarket titles list the home side first for some leagues and the away side for others
            first, second = sides[0].strip(), sides[1].strip()
            home_raw, away_raw = (first, second) if home_first else (second, first)
            quotes = self._polymarket_quotes(sport, markets, home_raw, away_raw)
            if HOME not in quotes or AWAY not in quotes:
                continue
            if spec is not None and spec.has_draw != (DRAW in quotes):
                continue  # incomplete book: a three-way sport quoted without the draw (or vice versa)
            liquidity = min((float(m.get("liquidityNum") or 0.0) for m in markets), default=0.0)
            spread = max((float(m.get("spread") or 0.0) for m in markets), default=0.0)
            stamps = [stamp for m in markets if (stamp := _parse_time(m.get("updatedAt")))]
            self._emit(
                report,
                sport=sport,
                home=self._side(sport, home_raw, report),
                away=self._side(sport, away_raw, report),
                kickoff=kickoff,
                quotes=quotes,
                suspended=any(m.get("closed") or not m.get("acceptingOrders", True) for m in markets),
                # Deep, tight books are worth more than thin ones
                confidence=min(0.95, max(0.2, 0.35 + 0.1 * math.log10(1.0 + liquidity) - 2.0 * spread)),
                observed_at=max(stamps) if stamps else batch.fetched_at,
                source_event_id=event_id,
            )

    def _same_side(self, sport: str, a: str, b: str) -> bool:
        na, nb = normalize_name(a), normalize_name(b)
        if na == nb or _core_name(na) == _core_name(nb):
            return True
        ra, rb = self._aliases.lookup(sport, a), self._aliases.lookup(sport, b)
        return ra is not None and ra == rb

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
