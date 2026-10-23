"""TODAR MAL's daily market health and its "when to bet" intimation (Group 115, points b.10 and b.12). Developed for Amit
Ashok Kumar Patnaik.

``measure`` reads what today's market is, from the live board and the Wire only:

* the slate: fixtures kicking off for the rest of the local day (SENTINEL_TIMEZONE) priced at Parimatch or 1xBet, and
  how many are in a top-tier league (the league registry's tier for the fixture's feed key; unknown leagues are named);
* the juice: the desk books' average Match Odds overround over the markets they quote in full (``market_margins``);
* rotation and absences: fixtures with rotation news (the Wire's ROTATION event) or a side missing at least
  ``MARKET_HEALTH_ABSENCES_PER_SIDE`` players OUT or SUSPENDED; unmeasured when the Wire has read nothing recently;
* the anchors: legs the synthesizer could use today (``street_smart_synthesizer.anchors`` at either desk book).

``street_smart.score_day`` turns that into the 0-100 score, its tier and the advice; ``evaluate`` keeps one snapshot
per day (re-evaluated on each read). ``intimate`` runs once a day at MARKET_HEALTH_HOUR:MARKET_HEALTH_MINUTE (Celery beat,
or the API's Sentinel watchdog without a worker): a REST_DAY warning or a PRIME_HARVEST day goes to the phone through
the Sentinel (AlertKind.MARKET_HEALTH, the routing matrix's HYPE row: Telegram and SMS); a SELECTIVE day sends nothing.
A per-day key sends it once, and that morning's 08:00 hype then stands down, so the phone never hears "no betting
today" beside "secure the bag". What each channel delivered is read from the Sentinel's delivery log.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain import street_smart as ss
from app.domain.manual_parlay.cognitive_parlay_rater import market_margins
from app.domain.oracle.markets import MarketKind
from app.domain.oracle.parlay_engine import LegCandidate
from app.models.market_health import DailyMarketHealthSnapshot, MarketAdvisoryTier
from app.models.sentinel import ChannelName, DeliveryStatus, SentinelDelivery, Severity
from app.models.sports_config import LeagueRegistry
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment
from app.services import street_smart_synthesizer as synth
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert
from app.services.sentinel_hype import CATCH_UP_HOURS, local_day, market_health_sent_key

logger = logging.getLogger("betdoc.market_health")

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
DESK = tuple(synth.DESK_BOOKS)  # parimatch, 1xbet
SYNTH_PATH = "/oracle?tab=builder&mode=synthesizer"


class MarketHealthUnavailable(Exception):
    """The live board cannot be read: no day is scored without the market."""


@dataclass(slots=True)
class Measured:
    day: date
    inputs: ss.HealthInputs


def thresholds(settings: Settings) -> ss.HealthThresholds:
    return ss.HealthThresholds(settings.MARKET_HEALTH_REST_BELOW, settings.MARKET_HEALTH_PRIME_FROM, settings.MARKET_HEALTH_MARGIN_ELEVATED,
                               settings.MARKET_HEALTH_MARGIN_SEVERE)


def _margins(today: Sequence[LegCandidate], settings: Settings, now: datetime) -> list[float]:
    """Each desk book's Match Odds overround on each fixture it quotes in full (a two-way sport over home and away)."""
    by_fixture: dict[str, list[LegCandidate]] = defaultdict(list)
    for leg in today:
        if leg.market.kind is MarketKind.MATCH_ODDS:
            by_fixture[leg.fixture_id].append(leg)
    out: list[float] = []
    for legs in by_fixture.values():
        quotes = {leg.selection: {b: q.odds for b, q in leg.quotes.items() if b in DESK and q.age(now) <= settings.ASHOKA_MAX_QUOTE_AGE_SECONDS} for leg in legs}
        three_way = any(leg.selection == "DRAW" for leg in legs)  # some book prices the draw: a three-way market
        selections = [s for s in legs[0].market.selections if s != "DRAW" or three_way]
        out.extend(market_margins(selections, quotes)["by_book"].values())
    return out


async def _tiers(session: AsyncSession, sport_keys: set[str]) -> dict[str, int]:
    if not sport_keys:
        return {}
    rows = (await session.execute(select(LeagueRegistry.odds_api_sport_key, func.min(LeagueRegistry.tier))
                                  .where(LeagueRegistry.odds_api_sport_key.in_(sport_keys), LeagueRegistry.is_active.is_(True))
                                  .group_by(LeagueRegistry.odds_api_sport_key))).all()
    return {key: int(tier) for key, tier in rows if key}


async def _rotation(session: AsyncSession, settings: Settings, fixtures: set[str], now: datetime) -> int | None:
    """Today's fixtures with rotation news or a depleted squad; None when the Wire has read nothing in the lookback."""
    since = now - timedelta(hours=settings.MARKET_HEALTH_WIRE_LOOKBACK_HOURS)
    articles = (await session.execute(select(NewsArticleSentiment.events, NewsArticleSentiment.fixtures)
                                      .where(NewsArticleSentiment.ingested_at >= since))).all()
    squads = (await session.execute(select(func.count()).select_from(InjuryRosterReport).where(InjuryRosterReport.updated_at >= since))).scalar_one()
    if not articles and not squads:
        return None
    risky: set[str] = set()
    for events, mentioned in articles:
        if "ROTATION" in (events or []):
            risky.update(m.get("fixture_id") for m in mentioned or [] if isinstance(m, dict) and m.get("fixture_id") in fixtures)
    if fixtures:
        depleted = (await session.execute(
            select(InjuryRosterReport.fixture_id).where(InjuryRosterReport.fixture_id.in_(fixtures), InjuryRosterReport.is_active.is_(True),
                                                        InjuryRosterReport.status.in_(("OUT", "SUSPENDED")))
            .group_by(InjuryRosterReport.fixture_id, InjuryRosterReport.side).having(func.count() >= settings.MARKET_HEALTH_ABSENCES_PER_SIDE))).scalars().all()
        risky.update(depleted)
    return len(risky)


def day_anchors(today: Sequence[LegCandidate], settings: Settings, now: datetime, until: datetime) -> list[ss.PricedLeg]:
    """The anchors at either desk book (the better-paying one per leg), one per fixture, safest first."""
    best: dict[str, ss.PricedLeg] = {}
    for book in DESK:
        for leg in synth.anchors(today, book, settings, now, until=until):
            held = best.get(leg.leg_id)
            if held is None or leg.ev > held.ev:
                best[leg.leg_id] = leg
    return ss.one_per_fixture(best.values())


async def measure(session: AsyncSession, redis: Redis | None, settings: Settings, now: datetime) -> Measured:
    day, _, end = local_day(now, settings.SENTINEL_TIMEZONE)
    try:
        candidates = await synth.board(redis, settings, now)
    except synth.SynthesisError as exc:
        raise MarketHealthUnavailable(exc.message) from exc
    today = [leg for leg in candidates if leg.kickoff is not None and now < leg.kickoff < end]
    priced = [leg for leg in today if any(b in leg.quotes for b in DESK)]
    fixtures = {leg.fixture_id for leg in priced}
    sport_of = {leg.fixture_id: leg.sport_key for leg in priced}
    tiers = await _tiers(session, {k for k in sport_of.values() if k})
    known = [fx for fx, key in sport_of.items() if key in tiers]
    top = sum(1 for fx in known if tiers[sport_of[fx]] <= settings.MARKET_HEALTH_TOP_TIER) if known else None  # type: ignore[index]
    margins = _margins(priced, settings, now)
    inputs = ss.HealthInputs(
        fixtures=len(fixtures), top_tier_fixtures=top, margin=(sum(margins) / len(margins)) if margins else None, margins_measured=len(margins),
        rotation=await _rotation(session, settings, fixtures, now), anchors=day_anchors(priced, settings, now, end), min_anchor_legs=settings.SYNTH_MIN_LEGS,
    )
    return Measured(day, inputs)


async def evaluate(session: AsyncSession, redis: Redis | None, settings: Settings, now: datetime) -> tuple[DailyMarketHealthSnapshot, ss.HealthCall]:
    """Today's snapshot, measured and scored now (created on the day's first evaluation). The caller commits."""
    measured = await measure(session, redis, settings, now)
    call = ss.score_day(measured.inputs, thresholds(settings))
    inputs = measured.inputs
    values = {
        "health_score": call.score, "advisory_tier": call.tier, "advisory_message": call.message, "deductions": call.deductions, "unmeasured": call.unmeasured,
        "active_fixtures_count": inputs.fixtures, "top_tier_fixtures_count": inputs.top_tier_fixtures,
        "average_bookmaker_margin": None if inputs.margin is None else round(inputs.margin, 6), "rotation_risks_count": inputs.rotation,
        "anchors_count": len(inputs.anchors), "top_anchors": [leg.as_dict() for leg in inputs.anchors[:5]], "updated_at": now,
    }
    snapshot = await _today(session, measured.day)
    if snapshot is None:
        try:
            async with session.begin_nested():
                snapshot = DailyMarketHealthSnapshot(id=uuid.uuid4(), snapshot_date=measured.day, created_at=now, **values)
                session.add(snapshot)
        except IntegrityError:  # another process created the day's row first
            snapshot = await _today(session, measured.day)
    if snapshot is not None:
        for key, value in values.items():
            setattr(snapshot, key, value)
    await session.flush()
    return snapshot, call  # type: ignore[return-value]


async def _today(session: AsyncSession, day: date) -> DailyMarketHealthSnapshot | None:
    return (await session.execute(select(DailyMarketHealthSnapshot).where(DailyMarketHealthSnapshot.snapshot_date == day))).scalar_one_or_none()


async def delivered(session: AsyncSession, alert_id: uuid.UUID | None) -> dict[str, bool]:
    """What the day's intimation actually reached, from the Sentinel's delivery log."""
    if alert_id is None:
        return {"telegram": False, "sms": False}
    channels = set((await session.execute(select(SentinelDelivery.channel).where(SentinelDelivery.alert_id == alert_id,
                                                                                SentinelDelivery.status == DeliveryStatus.SENT.value))).scalars())
    return {"telegram": ChannelName.TELEGRAM.value in channels, "sms": ChannelName.TWILIO.value in channels}


def view(snapshot: DailyMarketHealthSnapshot, reached: dict[str, bool]) -> dict[str, Any]:
    margin = snapshot.average_bookmaker_margin
    return {
        "date": snapshot.snapshot_date.isoformat(), "health_score": snapshot.health_score, "advisory_tier": snapshot.advisory_tier,
        "advisory_message": snapshot.advisory_message, "should_bet": snapshot.advisory_tier != MarketAdvisoryTier.REST_DAY.value,
        "deductions": snapshot.deductions or [], "unmeasured": snapshot.unmeasured or [], "active_fixtures_count": snapshot.active_fixtures_count,
        "top_tier_fixtures_count": snapshot.top_tier_fixtures_count, "average_bookmaker_margin_pct": None if margin is None else round(margin * 100, 2),
        "rotation_risks_count": snapshot.rotation_risks_count, "anchors_count": snapshot.anchors_count, "top_anchors": snapshot.top_anchors or [],
        "dispatched_at": None if snapshot.dispatched_at is None else snapshot.dispatched_at.isoformat(), "dispatched_telegram": reached["telegram"],
        "dispatched_sms": reached["sms"], "updated_at": snapshot.updated_at.isoformat(), "developer_name": DEVELOPER_CREDIT,
    }


def is_due(now: datetime, settings: Settings) -> bool:
    """Between MARKET_HEALTH_HOUR:MARKET_HEALTH_MINUTE and CATCH_UP_HOURS after it, SENTINEL_TIMEZONE."""
    local = now.astimezone(ZoneInfo(settings.SENTINEL_TIMEZONE))
    at = local.replace(hour=settings.MARKET_HEALTH_HOUR, minute=settings.MARKET_HEALTH_MINUTE, second=0, microsecond=0)
    return at <= local < at + timedelta(hours=CATCH_UP_HOURS)


def alert_body(call: ss.HealthCall, snapshot: DailyMarketHealthSnapshot, settings: Settings) -> str:
    from app.services.pocket_twin import app_link  # noqa: PLC0415 - the Pocket Twin's formatting, loaded on use

    margin = snapshot.average_bookmaker_margin
    lines = [call.body,
             f"Today: {snapshot.active_fixtures_count} fixtures priced at Parimatch/1xBet · margin "
             f"{'not measured' if margin is None else f'{margin:.1%}'} · rotation/absence risk "
             f"{'not measured' if snapshot.rotation_risks_count is None else snapshot.rotation_risks_count} · {snapshot.anchors_count} anchors"]
    link = app_link(settings, SYNTH_PATH)
    if link and call.tier == MarketAdvisoryTier.PRIME_HARVEST.value:
        lines.append(link)
    lines.append(f"Developed for {DEVELOPER_CREDIT}")
    return "\n".join(lines)


async def intimate(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *,
                   dry_run: bool = False) -> dict[str, Any]:
    """Score today and, on a REST_DAY or a PRIME_HARVEST day, page the phone once (a dry run sends and keeps nothing)."""
    async with sessions() as session:
        snapshot, call = await evaluate(session, redis, settings, now)
        result: dict[str, Any] = {"date": snapshot.snapshot_date.isoformat(), "tier": call.tier, "score": call.score, "title": call.title,
                                  "body": alert_body(call, snapshot, settings), "sent": False, "dry_run": dry_run}
        if dry_run:
            await session.rollback()
            return result
        await session.commit()
        if call.tier == MarketAdvisoryTier.SELECTIVE.value:
            result["reason"] = "a selective day sends nothing to the phone"
            return result
        if snapshot.alert_id is not None:
            result["reason"] = "today's intimation already went out"
            return result
        key = market_health_sent_key(SentinelKeys(settings), snapshot.snapshot_date)
        try:
            if redis is None or not await redis.set(key, call.tier, nx=True, ex=2 * 86_400):
                result["reason"] = "today's intimation already went out"
                return result
        except (RedisError, OSError):
            result["reason"] = "Redis did not answer: nothing sent"
            return result
        alert = SentinelAlert(
            kind=AlertKind.MARKET_HEALTH, severity=Severity.WARNING if call.tier == MarketAdvisoryTier.REST_DAY.value else Severity.INFO,
            title=call.title[:200], body=result["body"][:4000], source="market_health", dedupe_key=f"market-health:{snapshot.snapshot_date.isoformat()}",
            detail={"tier": call.tier, "score": call.score, "deductions": call.deductions, "unmeasured": call.unmeasured,
                    "anchors": [leg["label"] for leg in snapshot.top_anchors or []]}, occurred_at=now,
        )
        if await emit_alert(redis, settings, alert):
            snapshot.alert_id, snapshot.dispatched_at = alert.id, now
            await session.commit()
            result.update(sent=True, alert_id=str(alert.id))
        else:
            try:
                await redis.delete(key)  # the bus did not take it: the next run tries again
            except (RedisError, OSError):
                pass
            result["reason"] = "the Sentinel bus did not take the alert"
    logger.info("market health %s: %s %s/100 (%s)", result["date"], call.tier, call.score, "sent" if result["sent"] else result.get("reason", "not sent"))
    return result


__all__ = ["MarketHealthUnavailable", "day_anchors", "delivered", "evaluate", "intimate", "is_due", "measure", "thresholds", "view"]
