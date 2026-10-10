"""The Never-Forget shield (Group 75): memorise lost legs, guard pillar 15 with them, measure what it turned away.

* ``lessons`` reads the vault for pillar 15 from the database on every fortress run. It is not cached: a cache
  that misses a lesson is the one way a "never forget" guard can forget.
* ``record_vetoes`` keeps one prevention row per rule, leg and user from pillar 15's vetoes (re-vetting a slip
  never counts twice) with the stake the fortress had sized for the slip.
* ``memorize`` (the feedback sweep) turns every lost or half-lost leg of a bet placed from a twin audit into a
  memory and its rule, from the situation pillar 15 recorded when the slip was vetted and the root cause the
  post-mortem found. It works through every such leg not yet memorised, so a sweep that fails is healed by the
  next. A variance loss, a lesson the recorded evidence cannot recognise again, and a lesson that would have
  matched too many of the legs seen recently are kept EXPERIMENTAL (shadow matches only); the rest are ACTIVE.
* ``resolve_vetoes`` settles each vetoed leg against its fixture's final score: the record says how many
  vetoed legs lost (losses avoided) and how many won (winners missed).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.oracle import never_forget as nf
from app.domain.oracle.markets import LegResult
from app.models.digital_twin import TwinVettingAudit
from app.models.feedback import RootCauseTag, SettlementRootCauseAudit
from app.models.never_forget import AshokaMistakeMemory, NeverForgetPreventionAudit, NeverForgetRule, RuleStatus, XPActionType
from app.models.sentinel import Severity
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin import xp_engine
from app.services.twin.vetting import developer_credit
from app.services.user_pnl_tracker import leg_result_from_score

logger = logging.getLogger("betdoc.never_forget")

LOSING = frozenset({PlacedStatus.LOST.value, PlacedStatus.HALF_LOST.value})
LOST_LEGS = frozenset({LegResult.LOST.value, LegResult.HALF_LOST.value})


def pillar_15_metrics(pillars: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return next(((p.get("metrics") or {}) for p in pillars if p.get("number") == 15), None)


def _lesson(rule: NeverForgetRule) -> nf.Lesson:
    vector = (rule.rule_conditions or {}).get("vector") or {}
    return nf.Lesson(str(rule.id), rule.rule_code, str(rule.mistake_id), rule.status, rule.shape, {k: float(v) for k, v in vector.items()}, rule.description)


async def lessons(session: AsyncSession) -> list[nf.Lesson]:
    """Every ACTIVE and EXPERIMENTAL rule, oldest first."""
    rows = (await session.execute(
        select(NeverForgetRule).where(NeverForgetRule.status.in_((RuleStatus.ACTIVE.value, RuleStatus.EXPERIMENTAL.value))).order_by(NeverForgetRule.created_at, NeverForgetRule.id)
    )).scalars()
    return [_lesson(rule) for rule in rows]


# ================================================================ vetoes
async def record_vetoes(sessions: async_sessionmaker[AsyncSession], audit: TwinVettingAudit, now: datetime) -> list[NeverForgetPreventionAudit]:
    """One prevention per rule, leg and user from the audit's pillar 15 vetoes; returns the new ones."""
    metrics = pillar_15_metrics(audit.pillars or [])
    if not metrics:
        return []
    slip_legs = {f"{leg['fixture_id']}|{leg['market']}|{leg['selection']}": leg for leg in (audit.slip or {}).get("legs", [])}
    created: list[NeverForgetPreventionAudit] = []
    async with sessions() as session:
        for row in metrics.get("legs") or []:
            leg = slip_legs.get(row.get("leg", ""))
            if leg is None:
                continue
            for veto in row.get("vetoes") or []:
                rule = await session.get(NeverForgetRule, uuid.UUID(veto["rule_id"]), with_for_update=True)
                if rule is None:
                    continue
                key = f"{rule.id}:{audit.user_id or '-'}:{row['leg']}"[:240]
                price = (leg.get("prices") or {}).get(audit.bookmaker or "") or (row.get("raw") or {}).get("odds")
                prevention = NeverForgetPreventionAudit(
                    id=uuid.uuid4(), rule_id=rule.id, mistake_id=rule.mistake_id, vetting_audit_id=audit.id, user_id=audit.user_id, dedupe_key=key, leg_ref=row["leg"][:220],
                    fixture_id=leg["fixture_id"], home=leg.get("home", ""), away=leg.get("away", ""), sport_key=leg.get("sport_key"),
                    kickoff=datetime.fromisoformat(leg["kickoff"]) if leg.get("kickoff") else None, market=leg["market"], selection=leg["selection"],
                    odds=Decimal(str(price)).quantize(Decimal("0.0001")), similarity_score=min(1.0, max(0.0, float(veto["similarity"]))),
                    stake_withheld_inr=audit.stake_inr if audit.stake_inr and audit.stake_inr > 0 else None,
                    veto_reason=f"{veto['similarity']:.0%} like {rule.rule_code}: {rule.description}"[:4000], created_at=now,
                )
                inserted = (await session.execute(xp_engine.insert_ignore(session, NeverForgetPreventionAudit).values(
                    **{c.key: getattr(prevention, c.key) for c in NeverForgetPreventionAudit.__table__.columns},
                ).returning(NeverForgetPreventionAudit.id))).scalar_one_or_none()
                if inserted is None:
                    continue  # this leg was turned away by this rule for this user before
                rule.times_triggered += 1
                rule.last_triggered_at = now
                created.append(prevention)
        await session.commit()
    return created


async def resolve_vetoes(sessions: async_sessionmaker[AsyncSession], settings: Settings, now: datetime) -> list[NeverForgetPreventionAudit]:
    """How each vetoed leg finished, once its fixture's score is recorded; returns the newly resolved."""
    async with sessions() as session:
        open_rows = list((await session.execute(
            select(NeverForgetPreventionAudit).where(NeverForgetPreventionAudit.outcome.is_(None)).order_by(NeverForgetPreventionAudit.created_at)
            .limit(settings.FEEDBACK_BATCH_SIZE).with_for_update(skip_locked=True)
        )).scalars())
        if not open_rows:
            await session.commit()
            return []
        scores = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(FixtureScore.fixture_id.in_({r.fixture_id for r in open_rows})))).scalars()}
        done: list[NeverForgetPreventionAudit] = []
        for row in open_rows:
            score = scores.get(row.fixture_id)
            result = leg_result_from_score(row, score, now) if score is not None else None  # type: ignore[arg-type] - it reads the leg's fields, which a prevention carries
            if result is None:
                continue
            row.outcome, row.resolved_at = result.value, now
            done.append(row)
        await session.commit()
    return done


# ================================================================ memorising
def rule_code(memory_id: uuid.UUID, now: datetime) -> str:
    return f"NF-{now:%y%m%d}-{memory_id.hex[:6].upper()}"


async def _recent_legs(session: AsyncSession, settings: Settings, now: datetime, exclude: uuid.UUID | None) -> list[nf.SeenLeg]:
    rows = (await session.execute(
        select(TwinVettingAudit.id, TwinVettingAudit.pillars).where(TwinVettingAudit.created_at >= now - timedelta(days=settings.NEVER_FORGET_SPECIFICITY_WINDOW_DAYS))
        .order_by(TwinVettingAudit.created_at.desc()).limit(settings.NEVER_FORGET_SPECIFICITY_MAX_AUDITS)
    )).all()
    return [leg for audit_id, pillars in rows if audit_id != exclude for leg in nf.seen_legs(pillars or [])]


def verdict(policy: nf.NeverForgetPolicy, settings: Settings, cause: str, vector: dict[str, float], spec: nf.Specificity) -> tuple[RuleStatus, str]:
    """ACTIVE or EXPERIMENTAL, and why."""
    coverage = nf.own_coverage(vector, policy)
    if cause in settings.NEVER_FORGET_SHADOW_CAUSES:
        return RuleStatus.EXPERIMENTAL, f"a {cause.replace('_', ' ').lower()} loss: a fairly priced chance, not a trap; kept as a shadow lesson until an administrator promotes it"
    if coverage < policy.min_coverage:
        missing = ", ".join(nf.missing_features(vector, policy))
        return RuleStatus.EXPERIMENTAL, f"the fortress recorded {coverage:.0%} of the situation's weight (no fresh {missing}): too little to recognise it again"
    if spec.comparable >= settings.NEVER_FORGET_SPECIFICITY_MIN_LEGS and spec.rate is not None and spec.rate > settings.NEVER_FORGET_MAX_MATCH_RATE:
        return RuleStatus.EXPERIMENTAL, (f"it would have vetoed {spec.matched} of the {spec.comparable} comparable legs seen in {settings.NEVER_FORGET_SPECIFICITY_WINDOW_DAYS:g} days "
                                         f"({spec.rate:.0%}, over {settings.NEVER_FORGET_MAX_MATCH_RATE:.0%}): ordinary betting, not a trap")
    if spec.comparable < settings.NEVER_FORGET_SPECIFICITY_MIN_LEGS:
        return RuleStatus.ACTIVE, f"only {spec.comparable} comparable legs seen recently ({settings.NEVER_FORGET_SPECIFICITY_MIN_LEGS} judge breadth): guarding conservatively"
    return RuleStatus.ACTIVE, f"specific: it matches {spec.matched} of the {spec.comparable} comparable legs seen in {settings.NEVER_FORGET_SPECIFICITY_WINDOW_DAYS:g} days"


async def memorize(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> list[tuple[AshokaMistakeMemory, NeverForgetRule, uuid.UUID]]:
    """Memorise every lost leg of a twin-audited bet not memorised yet; returns (memory, rule, the bettor) per new lesson."""
    if not settings.NEVER_FORGET_ENABLED:
        return []
    policy = nf.NeverForgetPolicy.from_settings(settings)
    out: list[tuple[AshokaMistakeMemory, NeverForgetRule, uuid.UUID]] = []
    async with sessions() as session:
        due = (await session.execute(
            select(UserPlacedLeg, UserPlacedBet)
            .join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id)
            .join(AshokaMistakeMemory, AshokaMistakeMemory.leg_id == UserPlacedLeg.id, isouter=True)
            .where(
                UserPlacedBet.vetting_audit_id.is_not(None), UserPlacedBet.status.in_(LOSING), UserPlacedBet.feedback_at.is_not(None),
                UserPlacedLeg.result.in_(LOST_LEGS), AshokaMistakeMemory.id.is_(None),
            ).order_by(UserPlacedBet.settled_at, UserPlacedLeg.id).limit(settings.FEEDBACK_BATCH_SIZE)
        )).all()
        if not due:
            return []
        audits = {a.id: a for a in (await session.execute(select(TwinVettingAudit).where(TwinVettingAudit.id.in_({bet.vetting_audit_id for _, bet in due})))).scalars()}
        causes: dict[uuid.UUID, str] = {}
        for row in (await session.execute(
            select(SettlementRootCauseAudit).where(SettlementRootCauseAudit.bet_id.in_({bet.id for _, bet in due})).order_by(SettlementRootCauseAudit.created_at)
        )).scalars():
            causes[row.bet_id] = row.explanation
        credit = await developer_credit(session)
        seen_cache: dict[uuid.UUID | None, list[nf.SeenLeg]] = {}
        for leg, bet in due:
            audit = audits.get(bet.vetting_audit_id)  # type: ignore[arg-type]
            metrics = pillar_15_metrics(audit.pillars or []) if audit is not None else None
            found = nf.leg_vector_from(metrics, f"{leg.fixture_id}|{leg.market}|{leg.selection}") if metrics else None
            if found is None:
                logger.info("never-forget: leg %s of bet %s has no situation recorded (an audit before Group 75): not memorised", leg.id, bet.id)
                continue
            shape, sit = found
            cause = bet.root_cause_tag or RootCauseTag.VARIANCE_BAD_LUCK.value
            if bet.vetting_audit_id not in seen_cache:
                seen_cache[bet.vetting_audit_id] = await _recent_legs(session, settings, now, bet.vetting_audit_id)
            spec = nf.specificity(sit.vector, shape, leg.fixture_id, seen_cache[bet.vetting_audit_id], policy)
            status, reason = verdict(policy, settings, cause, sit.vector, spec)
            label = f"{leg.home} v {leg.away} {leg.market} {leg.selection} @ {leg.odds.normalize():f}"
            text = nf.lesson_text(label, shape, sit.raw, cause, causes.get(bet.id, "no post-mortem explanation recorded."))
            memory_id = uuid.uuid4()
            inserted = (await session.execute(xp_engine.insert_ignore(session, AshokaMistakeMemory).values(
                id=memory_id, bet_id=bet.id, leg_id=leg.id, vetting_audit_id=bet.vetting_audit_id, fixture_id=leg.fixture_id, home=leg.home, away=leg.away,
                sport_key=leg.sport_key, league=leg.league, market=leg.market, selection=leg.selection, placed_odds=leg.odds, leg_result=leg.result, shape=shape,
                loss_root_cause=cause, root_cause_explanation=causes.get(bet.id, ""), situational_fingerprint=sit.vector, situation_raw=sit.raw,
                extracted_lesson=text, developer_credit=credit, created_at=now,
            ).returning(AshokaMistakeMemory.id))).scalar_one_or_none()
            if inserted is None:
                continue  # another sweep memorised it first
            rule = NeverForgetRule(
                id=uuid.uuid4(), mistake_id=memory_id, rule_code=rule_code(memory_id, now), title=f"Never back {shape.replace(':', ' ').lower()} like {leg.home} v {leg.away} again"[:255],
                description=text, shape=shape, rule_conditions={"vector": sit.vector, "threshold": policy.threshold, "gamma": policy.gamma, "weights": dict(policy.weights), "scope": policy.scope},
                action="VETO", status=status.value, status_reason=reason, status_changed_by=None,
                specificity={"comparable": spec.comparable, "matched": spec.matched, "rate": None if spec.rate is None else round(spec.rate, 4)},
                times_triggered=0, created_at=now, updated_at=now,
            )
            session.add(rule)
            out.append((await session.get(AshokaMistakeMemory, memory_id), rule, bet.user_id))  # type: ignore[arg-type]
        await session.commit()
    for memory, rule, _ in out:
        await emit_alert(redis, settings, lesson_alert(memory, rule))
        logger.info("never-forget: %s memorised %s (%s)", rule.rule_code, memory.fixture_id, rule.status)
    return out


def lesson_alert(memory: AshokaMistakeMemory, rule: NeverForgetRule) -> SentinelAlert:
    active = rule.status == RuleStatus.ACTIVE.value
    return SentinelAlert(
        kind=AlertKind.NEVER_FORGET_LESSON, severity=Severity.WARNING if active else Severity.INFO, source="never_forget",
        title=f"Never-Forget {rule.rule_code}: {'guarding' if active else 'shadow lesson'} · {memory.home} v {memory.away} {memory.market} {memory.selection}"[:200],
        body=f"{memory.extracted_lesson}\n{'ACTIVE' if active else 'EXPERIMENTAL'}: {rule.status_reason}\nDeveloper: {memory.developer_credit}"[:4000],
        dedupe_key=f"never_forget:{memory.id}", detail={"mistake_id": str(memory.id), "rule_id": str(rule.id), "rule_code": rule.rule_code, "status": rule.status, "root_cause": memory.loss_root_cause},
    )


async def learn(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, won_bets: Sequence[UserPlacedBet]) -> dict[str, int]:
    """The sweep's Group 75 step: memorise, settle the vetoed legs, and pay the XP they earned."""
    memories = await memorize(sessions, redis, settings, now)
    resolved = await resolve_vetoes(sessions, settings, now)
    awards = [xp_engine.Award(bet.user_id, XPActionType.BET_WON, f"bet:{bet.id}", f"Won: {bet.bookmaker} {bet.structure.lower()} for {bet.pnl_inr} profit", {"bet_id": str(bet.id)})
              for bet in won_bets]
    awards += [xp_engine.Award(user_id, XPActionType.MISTAKE_MEMORIZED, f"memory:{memory.id}", f"Lesson {rule.rule_code} memorised ({memory.loss_root_cause.replace('_', ' ').lower()})",
                               {"mistake_id": str(memory.id), "rule_code": rule.rule_code}) for memory, rule, user_id in memories]
    awards += [xp_engine.Award(row.user_id, XPActionType.LOSS_PREVENTED, f"veto:{row.id}", f"Pillar 15 turned away {row.home} v {row.away} {row.market} {row.selection}: it {row.outcome.replace('_', ' ').lower()}",  # type: ignore[union-attr]
                               {"prevention_id": str(row.id), "outcome": row.outcome}) for row in resolved if row.user_id is not None and row.outcome in LOST_LEGS]
    paid = await xp_engine.award_all(sessions, settings, awards, now) if awards else 0
    return {"memorised": len(memories), "vetoes_resolved": len(resolved), "xp_paid": paid}


# ================================================================ administration
async def set_status(session: AsyncSession, rule: NeverForgetRule, status: RuleStatus, reason: str, admin_id: uuid.UUID, now: datetime) -> NeverForgetRule:
    rule.status, rule.status_reason, rule.status_changed_by, rule.updated_at = status.value, f"{reason} (administrator, {now:%Y-%m-%d %H:%M} UTC)", admin_id, now
    await session.flush()
    return rule


# ================================================================ views
def memory_view(memory: AshokaMistakeMemory, rule: NeverForgetRule | None, record: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(memory.id), "fixture_id": memory.fixture_id, "home": memory.home, "away": memory.away, "sport_key": memory.sport_key, "league": memory.league,
        "market": memory.market, "selection": memory.selection, "placed_odds": str(memory.placed_odds), "leg_result": memory.leg_result, "shape": memory.shape,
        "loss_root_cause": memory.loss_root_cause, "root_cause_explanation": memory.root_cause_explanation, "situation": memory.situation_raw,
        "fingerprint": memory.situational_fingerprint, "extracted_lesson": memory.extracted_lesson, "developer_credit": memory.developer_credit,
        "created_at": memory.created_at.isoformat(), "rule": None if rule is None else rule_view(rule), "record": record or empty_record(),
    }


def rule_view(rule: NeverForgetRule) -> dict[str, Any]:
    return {"id": str(rule.id), "mistake_id": str(rule.mistake_id), "rule_code": rule.rule_code, "title": rule.title, "description": rule.description, "shape": rule.shape,
            "action": rule.action, "status": rule.status, "status_reason": rule.status_reason, "specificity": rule.specificity, "times_triggered": rule.times_triggered,
            "last_triggered_at": None if rule.last_triggered_at is None else rule.last_triggered_at.isoformat(), "created_at": rule.created_at.isoformat()}


def prevention_view(row: NeverForgetPreventionAudit, rule_code_: str | None = None) -> dict[str, Any]:
    return {"id": str(row.id), "rule_id": str(row.rule_id), "rule_code": rule_code_, "mistake_id": str(row.mistake_id), "audit_id": None if row.vetting_audit_id is None else str(row.vetting_audit_id),
            "fixture_id": row.fixture_id, "home": row.home, "away": row.away, "market": row.market, "selection": row.selection, "odds": str(row.odds),
            "similarity_score": row.similarity_score, "stake_withheld_inr": None if row.stake_withheld_inr is None else str(row.stake_withheld_inr),
            "veto_reason": row.veto_reason, "outcome": row.outcome, "resolved_at": None if row.resolved_at is None else row.resolved_at.isoformat(), "created_at": row.created_at.isoformat()}


def empty_record() -> dict[str, Any]:
    return {"vetoes": 0, "resolved": 0, "lost": 0, "won": 0, "void": 0, "stake_withheld_inr": "0.00", "stake_withheld_on_losers_inr": "0.00"}


def record_of(rows: Sequence[NeverForgetPreventionAudit]) -> dict[str, Any]:
    """The measured record of some vetoes. A slip's stake counts once however many of its legs were vetoed."""
    out = empty_record()
    stakes: dict[Any, Decimal] = {}
    losers: dict[Any, Decimal] = {}
    for row in rows:
        out["vetoes"] += 1
        if row.outcome is not None:
            out["resolved"] += 1
            out["lost" if row.outcome in LOST_LEGS else "void" if row.outcome == LegResult.VOID.value else "won"] += 1
        if row.stake_withheld_inr is not None:
            key = row.vetting_audit_id or row.id
            stakes[key] = row.stake_withheld_inr
            if row.outcome in LOST_LEGS:
                losers[key] = row.stake_withheld_inr
    out["stake_withheld_inr"] = str(sum(stakes.values(), Decimal("0.00")).quantize(Decimal("0.01")))
    out["stake_withheld_on_losers_inr"] = str(sum(losers.values(), Decimal("0.00")).quantize(Decimal("0.01")))
    return out


async def records_by_mistake(session: AsyncSession, mistake_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not mistake_ids:
        return {}
    grouped: dict[uuid.UUID, list[NeverForgetPreventionAudit]] = defaultdict(list)
    for row in (await session.execute(select(NeverForgetPreventionAudit).where(NeverForgetPreventionAudit.mistake_id.in_(mistake_ids)))).scalars():
        grouped[row.mistake_id].append(row)
    return {k: record_of(v) for k, v in grouped.items()}
