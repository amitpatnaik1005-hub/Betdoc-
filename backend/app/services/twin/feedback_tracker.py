"""The post-execution feedback loop (Group 73): settle, measure, explain, and teach the fortress.

``sweep`` runs every ``FEEDBACK_SWEEP_INTERVAL_SECONDS`` (and from the API):

1. **Settle.** Ashoka's own settlement (``user_pnl_tracker.settle_pending``) decides every leg a final
   score or market result decides, quarter-line Asian handicaps included, with the pending bets locked
   ``ORDER BY id FOR UPDATE SKIP LOCKED``. There is one settler: this loop never settles on its own.
2. **Attribute** every settled bet not yet attributed (``feedback_at IS NULL``), at most
   ``FEEDBACK_BATCH_SIZE`` per sweep, locked the same way, so two sweeps never attribute one bet twice:
   * the **closing line** per leg: the last non-suspended, non-anomalous tick of each selection of the
     leg's market at the first sharp book (``TWIN_SHARP_BOOKS``) that quoted the whole market in the
     ``FEEDBACK_CLOSING_LOOKBACK_HOURS`` before kickoff, from Nalanda's tick lake; Shin de-vigs it;
   * **CLV** per leg and, for a straight bet whose every leg has a close, for the whole slip;
   * **model feedback** per decided leg: every model the twin audit snapshotted when it vetted the slip
     (the names pillar 1 weights), Ashoka's ensemble probability and the sharp close itself, each with
     its Brier score, log loss and (with a whole distribution) ranked probability score;
   * the **root cause** of a loss (``feedback_math.classify``), with its evidence.
3. **Page** the phone through the Sentinel for each bet settled in the last ``FEEDBACK_ALERT_MAX_AGE_HOURS``:
   a win with its profit and CLV, a loss with its root cause and the models' Brier score. Older bets
   (a backlog) are attributed silently; cashouts and voids are not paged.

4. **Escalate** to the recalibration engine (Group 74) when the losses blamed on the models pile up
   (``TWIN_RECALIBRATION_LOSS_TRIGGER_COUNT`` in ``..._HOURS``). The engine is the only publisher of
   pillar 1's weights; this loop only feeds it.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.bookmakers.adapters import canonical_bookmaker
from app.domain.oracle import feedback_math as fm
from app.domain.oracle.fortress import shin_devig
from app.domain.oracle.markets import LegResult, parse_market
from app.models.digital_twin import PulloutReason, TwinInPlayMonitor, TwinVettingAudit
from app.models.feedback import CLOSING_SHARP, ENSEMBLE, REFERENCE_PREDICTORS, ModelPredictionFeedback, RootCauseTag, SettlementRootCauseAudit
from app.models.nalanda_lake import NalandaTick
from app.models.sentinel import Severity
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, PlacedStructure, SettlementSource, UserPlacedBet, UserPlacedLeg
from app.services import user_pnl_tracker as tracker
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin.intel import read_intel, weights_key, weights_meta_key
from app.services.twin.vetting import developer_credit, sharp_books

logger = logging.getLogger("betdoc.feedback")

ODDS_Q = Decimal("0.0001")
PAISA = Decimal("0.01")
STRAIGHT = frozenset({PlacedStructure.SINGLE.value, PlacedStructure.DOUBLE.value, PlacedStructure.TREBLE.value, PlacedStructure.ACCUMULATOR.value})
SILENT = frozenset({PlacedStatus.CASHED_OUT.value, PlacedStatus.VOID.value})


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# ================================================================ closing lines
@dataclass(frozen=True, slots=True)
class ClosingLine:
    book: str
    odds: float  # the selection's raw closing price
    fair_probability: float  # Shin de-vigged over the whole market
    observed_at: datetime
    shin_z: float


def close_from_ticks(ticks: Iterable[NalandaTick], market_key: str, selections: Sequence[str], kickoff: datetime, sharp: Sequence[str]) -> dict[str, ClosingLine]:
    """selection -> its closing line, from one fixture's ticks: the latest price of every selection at the first
    sharp book (in priority order) that quoted the whole market before kickoff."""
    latest: dict[str, dict[str, NalandaTick]] = defaultdict(dict)
    for tick in ticks:
        book = canonical_bookmaker(tick.bookmaker_id)
        seen = _aware(tick.observed_at)
        ref = parse_market(tick.market)
        if book not in sharp or seen is None or seen > kickoff or ref is None or ref.key != market_key or tick.selection not in selections:
            continue
        held = latest[book].get(tick.selection)
        if held is None or seen > _aware(held.observed_at):  # type: ignore[operator]
            latest[book][tick.selection] = tick
    for book in sharp:
        market = latest.get(book, {})
        if all(s in market for s in selections):
            fair, z = shin_devig([float(market[s].odds) for s in selections])
            return {
                s: ClosingLine(book, float(market[s].odds), fair[i], _aware(market[s].observed_at), z)  # type: ignore[arg-type]
                for i, s in enumerate(selections)
            }
    return {}


async def closing_lines(session: AsyncSession, legs: Sequence[UserPlacedLeg], kickoffs: dict[uuid.UUID, datetime], settings: Settings) -> dict[uuid.UUID, ClosingLine]:
    """leg id -> its sharp closing line (legs without a kickoff, or without a complete sharp market, are absent)."""
    sharp = sharp_books(settings)
    lookback = timedelta(hours=settings.FEEDBACK_CLOSING_LOOKBACK_HOURS)
    by_fixture: dict[str, list[UserPlacedLeg]] = defaultdict(list)
    for leg in legs:
        if leg.id in kickoffs:
            by_fixture[leg.fixture_id].append(leg)
    out: dict[uuid.UUID, ClosingLine] = {}
    for fixture_id, rows in by_fixture.items():
        start = min(kickoffs[leg.id] for leg in rows) - lookback
        end = max(kickoffs[leg.id] for leg in rows)
        ticks = (await session.execute(
            select(NalandaTick).where(
                NalandaTick.fixture_id == fixture_id, NalandaTick.observed_at >= start, NalandaTick.observed_at <= end,
                NalandaTick.created_at >= start, NalandaTick.created_at <= end + lookback,  # partition pruning (stored just after observed)
                NalandaTick.is_suspended.is_(False), NalandaTick.is_anomaly.is_(False),
            )
        )).scalars().all()
        for leg in rows:
            ref = parse_market(leg.market)
            if ref is None:
                continue
            line = close_from_ticks(ticks, ref.key, ref.selections, kickoffs[leg.id], sharp).get(leg.selection)
            if line is not None:
                out[leg.id] = line
    return out


# ================================================================ predictions
@dataclass(frozen=True, slots=True)
class Prediction:
    probability: float  # the expected settlement score
    distribution: dict[str, float] | None = None
    basis: str = "win_probability"


def audit_legs(audit: TwinVettingAudit | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    if audit is None:
        return {}
    return {(leg["fixture_id"], leg["market"], leg["selection"]): leg for leg in (audit.slip or {}).get("legs", [])}


def predictions(leg: UserPlacedLeg, audited: dict[str, Any] | None, close: ClosingLine | None) -> dict[str, Prediction]:
    """Every predictor that priced this leg before it was played."""
    out: dict[str, Prediction] = {}
    if audited is not None:
        dists = audited.get("distributions") or {}
        for name, p in (audited.get("models") or {}).items():
            dist = dists.get(name)
            if dist:
                out[name] = Prediction(fm.expected_score(dist), dict(dist), "distribution")
            elif p is not None:
                out[name] = Prediction(float(p))
    if leg.fair_probability is not None:
        out[ENSEMBLE] = Prediction(float(leg.fair_probability))
    if close is not None:
        out[CLOSING_SHARP] = Prediction(close.fair_probability, basis="sharp_close")
    return out


# ================================================================ the sweep
@dataclass(slots=True)
class FeedbackReport:
    settled_bets: int = 0
    settled_legs: int = 0
    attributed_bets: int = 0
    feedback_records: int = 0
    total_pnl_inr: Decimal = Decimal("0.00")
    alerts: int = 0
    root_causes: Counter[str] = field(default_factory=Counter)
    recalibration_run: str | None = None  # the run the losses triggered, if they did

    def as_dict(self) -> dict[str, Any]:
        return {"settled_bets": self.settled_bets, "settled_legs": self.settled_legs, "attributed_bets": self.attributed_bets,
                "feedback_records": self.feedback_records, "total_pnl_inr": str(self.total_pnl_inr.quantize(PAISA)), "alerts": self.alerts,
                "root_causes": dict(self.root_causes),
                "recalibration_run": self.recalibration_run}


def _weather_breach(intel: Any, settings: Settings) -> str | None:
    weather = getattr(intel, "weather", None) if intel is not None else None
    if weather is None or weather.indoor:
        return None
    parts = []
    if weather.wind_kmh is not None and weather.wind_kmh > settings.TWIN_MAX_WIND_KMH:
        parts.append(f"wind {weather.wind_kmh:g} km/h")
    if weather.precipitation_mmh is not None and weather.precipitation_mmh > settings.TWIN_MAX_RAIN_MMH:
        parts.append(f"rain {weather.precipitation_mmh:g} mm/h")
    return ", ".join(parts) or None


def _strict_referee(intel: Any, settings: Settings) -> str | None:
    referee = getattr(intel, "referee", None) if intel is not None else None
    if referee is None or referee.cards_per_game <= settings.TWIN_REFEREE_STRICT_CARDS:
        return None
    return f"{referee.name}, {referee.cards_per_game:g} cards a game"


def slip_clv(bet: UserPlacedBet, legs: Sequence[UserPlacedLeg], closes: dict[uuid.UUID, ClosingLine]) -> tuple[Decimal | None, float | None, float | None]:
    """(closing odds, CLV %, sharp CLV %) of a straight bet whose every leg has a close."""
    if bet.structure not in STRAIGHT or not legs or any(leg.id not in closes for leg in legs):
        return None, None, None
    placed = float(bet.placed_odds) if bet.placed_odds is not None else math.prod(float(leg.odds) for leg in legs)
    closing = math.prod(closes[leg.id].odds for leg in legs)
    fair = math.prod(closes[leg.id].fair_probability for leg in legs)
    return Decimal(str(closing)).quantize(ODDS_Q, rounding=ROUND_HALF_UP), fm.clv_pct(placed, closing), fm.clv_sharp_pct(placed, fair)


async def _kickoffs(session: AsyncSession, legs: Sequence[UserPlacedLeg]) -> dict[uuid.UUID, datetime]:
    scores = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(FixtureScore.fixture_id.in_({leg.fixture_id for leg in legs})))).scalars()} if legs else {}
    out: dict[uuid.UUID, datetime] = {}
    for leg in legs:
        kickoff = _aware(leg.kickoff) or _aware(getattr(scores.get(leg.fixture_id), "kickoff", None))
        if kickoff is not None:
            out[leg.id] = kickoff
    return out


async def sweep(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *, user_id: uuid.UUID | None = None) -> FeedbackReport:
    settled = await tracker.settle_pending(sessions, now, user_id=user_id)
    await tracker.bump(redis, settled.users)
    report = FeedbackReport(settled_bets=settled.bets, settled_legs=settled.legs)
    alerts: list[SentinelAlert] = []
    policy = fm.RcaPolicy(settings.FEEDBACK_RCA_CONFIDENT_PROB, settings.FEEDBACK_RCA_STEAM_CLV_PCT)
    async with sessions() as session:
        query = (
            select(UserPlacedBet)
            .where(UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.feedback_at.is_(None))
            .order_by(UserPlacedBet.id).limit(settings.FEEDBACK_BATCH_SIZE).with_for_update(skip_locked=True)
        )
        if user_id is not None:
            query = query.where(UserPlacedBet.user_id == user_id)
        bets = list((await session.execute(query)).scalars())
        if not bets:
            await session.commit()
            return report
        ids = [b.id for b in bets]
        legs: dict[uuid.UUID, list[UserPlacedLeg]] = defaultdict(list)
        for leg in (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id.in_(ids)).order_by(UserPlacedLeg.bet_id, UserPlacedLeg.position))).scalars():
            legs[leg.bet_id].append(leg)
        all_legs = [leg for rows in legs.values() for leg in rows]
        audit_ids = {b.vetting_audit_id for b in bets if b.vetting_audit_id is not None}
        audits = {a.id: a for a in (await session.execute(select(TwinVettingAudit).where(TwinVettingAudit.id.in_(audit_ids)))).scalars()} if audit_ids else {}
        monitors = {m.bet_id: m for m in (await session.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id.in_(ids)))).scalars()}
        done = {(row[0], row[1]) for row in (await session.execute(select(ModelPredictionFeedback.leg_id, ModelPredictionFeedback.model_name).where(ModelPredictionFeedback.bet_id.in_(ids)))).all()}
        await session.execute(delete(SettlementRootCauseAudit).where(SettlementRootCauseAudit.bet_id.in_(ids)))  # re-attribution replaces the old verdict
        closes = await closing_lines(session, all_legs, await _kickoffs(session, all_legs), settings)
        intel: dict[str, Any] = {}
        if redis is not None:
            try:
                intel = await read_intel(redis, settings, {leg.fixture_id for leg in all_legs})
            except (RedisError, OSError):
                intel = {}
        credit = await developer_credit(session)
        for bet in bets:
            rows = legs.get(bet.id, [])
            audited = audit_legs(audits.get(bet.vetting_audit_id)) if bet.vetting_audit_id else {}
            for leg in rows:
                close = closes.get(leg.id)
                if close is not None:
                    leg.closing_odds = Decimal(str(close.odds)).quantize(ODDS_Q)
                    leg.closing_fair_probability = round(close.fair_probability, 6)
                    leg.closing_book = close.book
            bet.closing_odds, bet.clv_pct, bet.clv_sharp_pct = slip_clv(bet, rows, closes)
            briers: list[float] = []
            for leg in rows:
                if leg.result == PlacedStatus.PENDING.value:
                    continue  # a cashed-out bet's legs stay undecided
                result = LegResult(leg.result)
                y = fm.outcome_score(result)
                if y is None:
                    continue
                close = closes.get(leg.id)
                leg_clv = fm.clv_pct(float(leg.odds), close.odds) if close is not None else None
                for name, pred in predictions(leg, audited.get((leg.fixture_id, leg.market, leg.selection)), close).items():
                    brier = fm.brier_score(pred.probability, y)
                    if name not in REFERENCE_PREDICTORS:
                        briers.append(brier)
                    if (leg.id, name) in done:
                        continue
                    session.add(ModelPredictionFeedback(
                        id=uuid.uuid4(), bet_id=bet.id, leg_id=leg.id, fixture_id=leg.fixture_id, sport_key=leg.sport_key, market=leg.market, selection=leg.selection,
                        model_name=name, predicted_prob=fm.clip01(pred.probability), actual_outcome=y, brier_score=brier,
                        log_loss=fm.log_loss(pred.probability, y, settings.FEEDBACK_LOG_LOSS_EPSILON),
                        rps=None if pred.distribution is None else fm.ranked_probability_score(pred.distribution, result),
                        closing_odds=None if close is None else Decimal(str(close.odds)).quantize(ODDS_Q), clv_pct=_round(leg_clv),
                        details={"basis": pred.basis, "result": result.value, "home": leg.home, "away": leg.away,
                                 "score": None if leg.home_goals is None else f"{leg.home_goals}-{leg.away_goals}",
                                 **({"closing_book": close.book, "shin_z": round(close.shin_z, 6)} if close is not None else {})},
                        created_at=now,
                    ))
                    done.add((leg.id, name))
                    report.feedback_records += 1
            fair = [leg.fair_probability for leg in rows]
            monitor = monitors.get(bet.id)
            collapse = (monitor.initial_win_prob, monitor.current_win_prob) if monitor is not None and monitor.pullout_reason == PulloutReason.PROBABILITY_COLLAPSE.value else None
            evidence = fm.LossEvidence(
                status=bet.status, win_probability=math.prod(fair) if fair and all(p is not None for p in fair) else None,  # type: ignore[misc]
                clv_pct=bet.clv_pct, inplay_collapse=collapse,
                weather_breach=next((b for leg in rows if (b := _weather_breach(intel.get(leg.fixture_id), settings))), None),
                strict_referee=next((r for leg in rows if (r := _strict_referee(intel.get(leg.fixture_id), settings))), None),
                model_brier=fm.mean(briers),
            )
            tag, explanation = fm.classify(evidence, policy)
            bet.root_cause_tag = tag.value
            if tag is not RootCauseTag.NONE:
                session.add(SettlementRootCauseAudit(
                    id=uuid.uuid4(), bet_id=bet.id, root_cause_tag=tag.value, explanation=explanation, model_error_delta=_round(evidence.model_brier, 6),
                    evidence={"win_probability": _round(evidence.win_probability), "clv_pct": _round(evidence.clv_pct, 2), "clv_sharp_pct": _round(bet.clv_sharp_pct, 2),
                              "inplay_collapse": None if collapse is None else [round(collapse[0], 4), round(collapse[1], 4)],
                              "weather": evidence.weather_breach, "referee": evidence.strict_referee},
                    created_at=now,
                ))
                report.root_causes[tag.value] += 1
            bet.feedback_at = now
            report.attributed_bets += 1
            report.total_pnl_inr += bet.pnl_inr or Decimal(0)
            settled_at = _aware(bet.settled_at)
            if bet.status not in SILENT and settled_at is not None and now - settled_at <= timedelta(hours=settings.FEEDBACK_ALERT_MAX_AGE_HOURS):
                alerts.append(settlement_alert(bet, tag, explanation, evidence.model_brier, credit))
        await session.commit()
    for alert in alerts:
        if await emit_alert(redis, settings, alert):
            report.alerts += 1
    if report.root_causes.get(RootCauseTag.MODEL_UNDERESTIMATION.value):
        from app.services.twin import model_calibrator  # noqa: PLC0415 - the engine imports this loop's tables

        triggered = await model_calibrator.maybe_loss_trigger(sessions, redis, settings, now)
        report.recalibration_run = None if triggered is None else str(triggered.id)
    logger.info("feedback sweep: %s", report.as_dict())
    return report


def _rupees(value: Decimal | None, signed: bool = False) -> str:
    amount = (value or Decimal(0)).quantize(PAISA)
    sign = "-" if amount < 0 else "+" if signed and amount > 0 else ""
    return f"{sign}₹{abs(amount):,}"


def settlement_alert(bet: UserPlacedBet, tag: RootCauseTag, explanation: str, model_brier: float | None, credit: str) -> SentinelAlert:
    won = (bet.pnl_inr or Decimal(0)) > 0
    clv = "CLV n/a" if bet.clv_pct is None else f"CLV {bet.clv_pct:+.2f}%" + ("" if bet.clv_sharp_pct is None else f" (vs fair {bet.clv_sharp_pct:+.2f}%)")
    where = f"{bet.bookmaker}" + (f" {bet.booking_code}" if bet.booking_code else "")
    if won:
        title = f"ASHOKA settled {bet.status.replace('_', ' ').lower()}: {_rupees(bet.pnl_inr, True)} · {clv}"
    else:
        title = f"ASHOKA debrief {bet.status.replace('_', ' ').lower()}: {_rupees(bet.pnl_inr, True)} · {tag.value.replace('_', ' ').lower()}"
    lines = [
        f"{where} · stake {_rupees(bet.stake_inr)} · returned {_rupees(bet.return_inr)} · P&L {_rupees(bet.pnl_inr, True)}",
        clv,
        *([] if won else [f"Why: {explanation}"] + ([f"Models' Brier score on it: {model_brier:.3f}"] if model_brier is not None else [])),
        f"Developer: {credit}",
    ]
    return SentinelAlert(
        kind=AlertKind.TWIN_SETTLED, severity=Severity.INFO, source="feedback_loop", title=title[:200], body="\n".join(lines)[:4000],
        dedupe_key=f"twin:settled:{bet.id}",
        detail={"bet_id": str(bet.id), "status": bet.status, "pnl_inr": str(bet.pnl_inr), "clv_pct": _round(bet.clv_pct, 2), "root_cause": tag.value},
    )


# ================================================================ recalibration
async def model_stats(session: AsyncSession, settings: Settings, now: datetime) -> list[dict[str, Any]]:
    """Per predictor over the window: predictions, mean Brier, log loss, RPS and leg CLV."""
    since = now - timedelta(days=settings.FEEDBACK_WINDOW_DAYS)
    rows = (await session.execute(
        select(
            ModelPredictionFeedback.model_name, func.count(ModelPredictionFeedback.id), func.avg(ModelPredictionFeedback.brier_score),
            func.avg(ModelPredictionFeedback.log_loss), func.avg(ModelPredictionFeedback.rps), func.avg(ModelPredictionFeedback.clv_pct),
        ).where(ModelPredictionFeedback.created_at >= since).group_by(ModelPredictionFeedback.model_name).order_by(ModelPredictionFeedback.model_name)
    )).all()
    return [{"model_name": name, "predictions": int(n), "avg_brier": float(b), "avg_log_loss": float(ll), "avg_rps": None if rps is None else float(rps),
             "avg_clv_pct": None if clv is None else float(clv), "reference": name in REFERENCE_PREDICTORS} for name, n, b, ll, rps, clv in rows]


async def published_weights(redis: Redis | None, settings: Settings) -> tuple[dict[str, float], dict[str, Any] | None]:
    if redis is None:
        return {}, None
    try:
        raw = await redis.hgetall(weights_key(settings))
        meta = await redis.get(weights_meta_key(settings))  # the recalibration engine's provenance
    except (RedisError, OSError):
        return {}, None
    weights: dict[str, float] = {}
    for name, value in raw.items():
        try:
            weights[name] = float(value)
        except ValueError:
            continue
    try:
        return weights, (json.loads(meta) if meta else None)
    except ValueError:
        return weights, None


def calibration_report(pairs: Iterable[tuple[float, float]], settings: Settings) -> dict[str, Any]:
    bins, ece = fm.calibration(pairs, settings.FEEDBACK_CALIBRATION_BINS)
    return {"expected_calibration_error": _round(ece, 6), "bins": [
        {"lower": b.lower, "upper": b.upper, "predicted_mean": _round(b.predicted_mean, 6), "observed_mean": _round(b.observed_mean, 6), "count": b.count}
        for b in bins
    ]}


# ================================================================ manual override
async def override(session: AsyncSession, bet: UserPlacedBet, status: PlacedStatus, return_inr: Decimal, notes: str | None, now: datetime) -> UserPlacedBet:
    """An administrator settles a bet by hand. Its attribution re-runs on the next sweep."""
    if status is PlacedStatus.PENDING:
        raise ValueError("an override settles the bet: give its final status")
    bet.status, bet.return_inr = status.value, return_inr.quantize(PAISA)
    bet.pnl_inr = (bet.return_inr - bet.stake_inr).quantize(PAISA)
    bet.settled_at, bet.settlement_source, bet.feedback_at = now, SettlementSource.MANUAL_OVERRIDE.value, None
    if notes:
        bet.notes = notes[:300]
    await session.flush()
    return bet
