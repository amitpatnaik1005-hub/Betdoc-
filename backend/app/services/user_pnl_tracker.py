"""The user's own P&L: placed bets, automatic settlement, the scorecard and the betting twin (Group 69).

Recording. "I placed this bet" stores the slip as the user placed it: the bookmaker, the stake, the odds
they actually got (which may differ from Ashoka's), every leg with its canonical market.

Settlement, automatic and idempotent (``settle_pending``). A leg settles from its fixture's final score
(``fixture_scores``: The Odds API's scores feed or an administrator), matched by Ashoka's fixture id or,
for a hand-typed leg, by team names and kickoff date; failing that, a 1X2 leg settles from the CFO's
recorded market result. Abandoned fixtures void their legs; postponed ones once the books' 48 hours have
passed. Every leg result comes from ``markets.settle_selection``: quarter-line handicaps and totals give
half wins and half losses. A straight multiple settles as soon as one leg loses, otherwise when all legs
are in; it pays the stake times the product of the legs' payout factors, scaled to the odds the book
actually gave (``placed_odds``). A system bet pays each line at its unit stake once every leg is in.

The scorecard: today's, this week's (Monday start), this month's and all-time P&L in ``ORACLE_TIMEZONE``,
by settlement time, with staked, returned, win rate, ROI and the current streak, and an optional estimate
of tax on net winnings (``ORACLE_TDS_RATE``). Cached in Redis per user and version; any record or
settlement bumps the version.

The twin: the user's results split by structure, number of legs, league, market, bookmaker and odds
band, with strengths (ROI >= +5% over at least five bets) and leaks (ROI <= -10%).
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.oracle.markets import LegResult, parse_market, payout_factor, settle_selection
from app.domain.oracle.parlay_engine import SYSTEMS, SlipKind, lines_of
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, PlacedStructure, ScoreStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import SYSTEM_STRUCTURES, PlaceBetRequest, ScoreIn

logger = logging.getLogger("betdoc.ashoka.pnl")

PAISA = Decimal("0.01")
ZERO = Decimal(0)
POSTPONED_GRACE = timedelta(hours=48)
STRAIGHT = {PlacedStructure.SINGLE, PlacedStructure.DOUBLE, PlacedStructure.TREBLE, PlacedStructure.ACCUMULATOR}
ODDS_BANDS = ((1.0, 1.5, "1.01-1.50"), (1.5, 2.0, "1.50-2.00"), (2.0, 3.0, "2.00-3.00"), (3.0, 5.0, "3.00-5.00"), (5.0, math.inf, "5.00+"))


def _money(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_UP)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _name(value: str) -> str:
    return " ".join("".join(ch for ch in value.casefold() if ch.isalnum() or ch.isspace()).split())


def manual_fixture_id(home: str, away: str, kickoff: datetime | None) -> str:
    """A hand-typed leg's fixture id: the teams and the kickoff date, so the same match shares it."""
    day = _aware(kickoff).date().isoformat() if kickoff else "undated"
    return f"manual:{_name(home).replace(' ', '-')}:{_name(away).replace(' ', '-')}:{day}"[:128]


# ================================================================ recording
async def record_bet(session: AsyncSession, user_id: uuid.UUID, body: PlaceBetRequest, now: datetime) -> UserPlacedBet:
    structure = PlacedStructure(body.structure)
    unit = None
    if structure in SYSTEM_STRUCTURES:
        n_lines = len(lines_of(SlipKind(structure.value), len(body.legs)))
        unit = _money(body.stake_inr / n_lines)
    bet = UserPlacedBet(
        id=uuid.uuid4(), user_id=user_id, slip_id=body.slip_id, source=body.source, bookmaker=body.bookmaker.value,
        bookmaker_name=body.bookmaker_name, structure=structure.value, stake_inr=body.stake_inr, unit_stake_inr=unit,
        placed_odds=body.placed_odds, placed_at=_aware(body.placed_at) or now, status=PlacedStatus.PENDING.value, notes=body.notes,
    )
    session.add(bet)
    for position, leg in enumerate(body.legs):
        session.add(
            UserPlacedLeg(
                id=uuid.uuid4(), bet_id=bet.id, position=position, fixture_id=leg.fixture_id or manual_fixture_id(leg.home, leg.away, leg.kickoff),
                home=leg.home, away=leg.away, sport_key=leg.sport_key, league=leg.league, kickoff=_aware(leg.kickoff), market=leg.market,
                selection=leg.selection, odds=leg.odds, fair_probability=leg.fair_probability, result=PlacedStatus.PENDING.value,
            )
        )
    await session.flush()
    return bet


async def record_score(session: AsyncSession, body: ScoreIn, *, source: str, by: uuid.UUID | None, now: datetime) -> FixtureScore:
    fixture_id = body.fixture_id or manual_fixture_id(body.home, body.away, body.kickoff)
    row = await session.get(FixtureScore, fixture_id)
    if row is None:
        row = FixtureScore(fixture_id=fixture_id)
        session.add(row)
    row.home, row.away, row.sport_key, row.kickoff = body.home, body.away, body.sport_key, _aware(body.kickoff)
    row.home_goals, row.away_goals, row.status = body.home_goals, body.away_goals, body.status.value
    row.source, row.recorded_by, row.recorded_at = source, by, now
    await session.flush()
    return row


# ================================================================ settling
def leg_result_from_score(leg: UserPlacedLeg, score: FixtureScore, now: datetime) -> LegResult | None:
    """None: not yet (a postponement inside the books' grace period)."""
    if score.status == ScoreStatus.ABANDONED.value:
        return LegResult.VOID
    if score.status == ScoreStatus.POSTPONED.value:
        kickoff = _aware(score.kickoff or leg.kickoff)
        return LegResult.VOID if kickoff is not None and now - kickoff >= POSTPONED_GRACE else None
    ref = parse_market(leg.market)
    if ref is None or score.home_goals is None or score.away_goals is None:
        return None
    home_goals, away_goals = score.home_goals, score.away_goals
    if _name(score.home) == _name(leg.away) and _name(score.away) == _name(leg.home):  # the score was recorded the other way round
        home_goals, away_goals = away_goals, home_goals
    return settle_selection(ref, leg.selection, home_goals, away_goals)


def bet_outcome(bet: UserPlacedBet, legs: Sequence[UserPlacedLeg]) -> tuple[PlacedStatus, Decimal] | None:
    """(status, return) once the bet can be settled, else None."""
    structure = PlacedStructure(bet.structure)
    results = [LegResult(leg.result) if leg.result != PlacedStatus.PENDING.value else None for leg in legs]
    if structure in STRAIGHT:
        if any(r is LegResult.LOST for r in results):
            return PlacedStatus.LOST, ZERO
        if any(r is None for r in results):
            return None
        factor = math.prod(payout_factor(r, float(leg.odds)) for r, leg in zip(results, legs, strict=True))  # type: ignore[arg-type]
        nominal = math.prod(float(leg.odds) for leg in legs)
        scale = float(bet.placed_odds) / nominal if bet.placed_odds is not None and nominal > 0 else 1.0
        payout = _money(Decimal(str(float(bet.stake_inr) * factor * scale)))
        if structure is PlacedStructure.SINGLE:
            return PlacedStatus(results[0].value), payout  # type: ignore[union-attr]
    else:
        if any(r is None for r in results):
            return None
        unit = float(bet.unit_stake_inr or 0)
        lines = lines_of(SlipKind(structure.value), len(legs))
        payout = _money(Decimal(str(sum(unit * math.prod(payout_factor(results[j], float(legs[j].odds)) for j in line) for line in lines))))  # type: ignore[arg-type]
    if all(r is LegResult.VOID for r in results):
        return PlacedStatus.VOID, _money(bet.stake_inr)
    if payout <= 0:
        return PlacedStatus.LOST, ZERO
    if payout > bet.stake_inr:
        return PlacedStatus.WON, payout
    if payout == bet.stake_inr:
        return PlacedStatus.VOID, payout
    return PlacedStatus.HALF_LOST, payout


@dataclass(slots=True)
class SettleReport:
    legs: int = 0
    bets: int = 0
    users: set[uuid.UUID] = field(default_factory=set)


async def _scores_for(session: AsyncSession, legs: Sequence[UserPlacedLeg]) -> dict[uuid.UUID, FixtureScore]:
    by_id = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(FixtureScore.fixture_id.in_({leg.fixture_id for leg in legs})))).scalars()}
    out: dict[uuid.UUID, FixtureScore] = {}
    unmatched = [leg for leg in legs if leg.fixture_id not in by_id]
    if unmatched:
        days = [d for leg in unmatched if (d := _aware(leg.kickoff)) is not None]
        query = select(FixtureScore)
        if days:
            query = query.where(or_(FixtureScore.kickoff.is_(None), FixtureScore.kickoff.between(min(days) - timedelta(days=1), max(days) + timedelta(days=1))))
        candidates = list((await session.execute(query.limit(5000))).scalars())
        for leg in unmatched:
            teams = {_name(leg.home), _name(leg.away)}
            for score in candidates:
                if {_name(score.home), _name(score.away)} != teams:
                    continue
                k1, k2 = _aware(leg.kickoff), _aware(score.kickoff)
                if k1 is None or k2 is None or abs((k1 - k2).total_seconds()) <= 86_400:
                    out[leg.id] = score
                    break
    for leg in legs:
        if leg.fixture_id in by_id:
            out[leg.id] = by_id[leg.fixture_id]
    return out


async def _market_results(session: AsyncSession, legs: Sequence[UserPlacedLeg]) -> dict[uuid.UUID, LegResult]:
    """1X2 legs the CFO's market results settle (no score needed)."""
    from app.models.cfo_vault import MarketResult  # noqa: PLC0415 - the CFO models are heavy

    one_x_two = [leg for leg in legs if leg.market == "Match Odds"]
    if not one_x_two:
        return {}
    rows = (await session.execute(select(MarketResult).where(MarketResult.fixture_id.in_({leg.fixture_id for leg in one_x_two}), MarketResult.market == "Match Odds"))).scalars()
    found = {row.fixture_id: row for row in rows}
    out: dict[uuid.UUID, LegResult] = {}
    for leg in one_x_two:
        row = found.get(leg.fixture_id)
        if row is None:
            continue
        if row.is_void:
            out[leg.id] = LegResult.VOID
        elif row.winning_selection:
            out[leg.id] = LegResult.WON if row.winning_selection == leg.selection else LegResult.LOST
    return out


async def settle_pending(session_factory: async_sessionmaker[AsyncSession], now: datetime, *, user_id: uuid.UUID | None = None) -> SettleReport:
    """Settle every leg a score or a market result now decides, then every bet that is complete."""
    report = SettleReport()
    async with session_factory() as session:
        query = select(UserPlacedLeg).join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id).where(
            UserPlacedBet.status == PlacedStatus.PENDING.value, UserPlacedLeg.result == PlacedStatus.PENDING.value
        )
        if user_id is not None:
            query = query.where(UserPlacedBet.user_id == user_id)
        pending = list((await session.execute(query)).scalars())
        if not pending:
            return report
        scores = await _scores_for(session, pending)
        results = await _market_results(session, [leg for leg in pending if leg.id not in scores])
        touched: set[uuid.UUID] = set()
        for leg in pending:
            score = scores.get(leg.id)
            result = leg_result_from_score(leg, score, now) if score is not None else results.get(leg.id)
            if result is None:
                continue
            leg.result, leg.settled_at = result.value, now
            if score is not None:
                leg.home_goals, leg.away_goals = score.home_goals, score.away_goals
            report.legs += 1
            touched.add(leg.bet_id)
        for bet_id in touched:
            bet = await session.get(UserPlacedBet, bet_id)
            if bet is None or bet.status != PlacedStatus.PENDING.value:
                continue
            legs = list((await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet_id).order_by(UserPlacedLeg.position))).scalars())
            outcome = bet_outcome(bet, legs)
            if outcome is None:
                continue
            status, payout = outcome
            bet.status, bet.return_inr, bet.pnl_inr, bet.settled_at = status.value, payout, _money(payout - bet.stake_inr), now
            report.bets += 1
            report.users.add(bet.user_id)
        await session.commit()
    return report


async def record_cashout(session: AsyncSession, bet: UserPlacedBet, amount: Decimal, now: datetime) -> UserPlacedBet:
    if bet.status != PlacedStatus.PENDING.value:
        raise ValueError("only a pending bet can be cashed out")
    bet.status, bet.return_inr, bet.pnl_inr, bet.settled_at = PlacedStatus.CASHED_OUT.value, _money(amount), _money(amount - bet.stake_inr), now
    return bet


# ================================================================ the scorecard
@dataclass(slots=True)
class PeriodStats:
    pnl: Decimal = ZERO
    staked: Decimal = ZERO
    returned: Decimal = ZERO
    bets: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0

    def add(self, bet: UserPlacedBet) -> None:
        self.bets += 1
        self.staked += bet.stake_inr
        self.returned += bet.return_inr or ZERO
        self.pnl += bet.pnl_inr or ZERO
        status = bet.status
        if status in (PlacedStatus.WON.value, PlacedStatus.HALF_WON.value) or (status == PlacedStatus.CASHED_OUT.value and (bet.pnl_inr or ZERO) > 0):
            self.won += 1
        elif status == PlacedStatus.VOID.value:
            self.void += 1
        else:
            self.lost += 1

    def as_dict(self, tax_rate: float | None = None) -> dict[str, Any]:
        decided = self.won + self.lost
        out: dict[str, Any] = {
            "pnl_inr": str(_money(self.pnl)), "staked_inr": str(_money(self.staked)), "returned_inr": str(_money(self.returned)),
            "bets": self.bets, "won": self.won, "lost": self.lost, "void": self.void,
            "win_rate": None if decided == 0 else round(self.won / decided, 4),
            "roi": None if self.staked == 0 else float((self.pnl / self.staked).quantize(Decimal("0.0001"))),
        }
        if tax_rate is not None:
            tax = _money(self.pnl * Decimal(str(tax_rate))) if self.pnl > 0 else ZERO
            out["tax_inr"] = str(tax)
            out["net_after_tax_inr"] = str(_money(self.pnl - tax))
        return out


def period_starts(now: datetime, tz: str) -> dict[str, datetime]:
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    today = datetime.combine(local.date(), time.min, tzinfo=zone)
    return {
        "today": today.astimezone(UTC),
        "week": (today - timedelta(days=local.weekday())).astimezone(UTC),
        "month": today.replace(day=1).astimezone(UTC),
    }


def streak(settled: Sequence[UserPlacedBet]) -> dict[str, Any]:
    """The current run of wins or losses, newest first (voids and break-even cashouts skip)."""
    kind, count = None, 0
    for bet in sorted(settled, key=lambda b: _aware(b.settled_at) or datetime.min.replace(tzinfo=UTC), reverse=True):
        pnl = bet.pnl_inr or ZERO
        if pnl == 0:
            continue
        this = "W" if pnl > 0 else "L"
        if kind is None:
            kind = this
        if this != kind:
            break
        count += 1
    return {"kind": kind, "count": count, "label": f"{kind}{count}" if kind else "—"}


def scorecard(bets: Iterable[UserPlacedBet], now: datetime, tz: str, tax_rate: float | None = None) -> dict[str, Any]:
    starts = period_starts(now, tz)
    periods = {name: PeriodStats() for name in ("today", "week", "month", "all_time")}
    pending = PeriodStats()
    settled: list[UserPlacedBet] = []
    for bet in bets:
        if bet.status == PlacedStatus.PENDING.value:
            pending.bets += 1
            pending.staked += bet.stake_inr
            continue
        settled.append(bet)
        at = _aware(bet.settled_at) or now
        periods["all_time"].add(bet)
        for name, start in starts.items():
            if at >= start:
                periods[name].add(bet)
    return {
        "generated_at": now.isoformat(),
        "timezone": tz,
        "periods": {name: stats.as_dict(tax_rate) for name, stats in periods.items()},
        "pending": {"bets": pending.bets, "staked_inr": str(_money(pending.staked))},
        "streak": streak(settled),
        "tax_rate": tax_rate,
    }


# ================================================================ the betting twin
def _band(odds: float) -> str:
    return next(label for low, high, label in ODDS_BANDS if low <= odds < high)


def twin_profile(bets: Sequence[UserPlacedBet], legs_by_bet: dict[uuid.UUID, list[UserPlacedLeg]], *, min_bets: int = 5) -> dict[str, Any]:
    """Where the user wins and where they leak, from settled bets only."""
    settled = [b for b in bets if b.status not in (PlacedStatus.PENDING.value,)]
    segments: dict[str, dict[str, PeriodStats]] = defaultdict(lambda: defaultdict(PeriodStats))
    odds_seen: list[float] = []
    for bet in settled:
        legs = legs_by_bet.get(bet.id, [])
        price = float(bet.placed_odds) if bet.placed_odds is not None else math.prod(float(leg.odds) for leg in legs) if legs else 0.0
        if price:
            odds_seen.append(price)
            segments["odds_band"][_band(price)].add(bet)
        segments["structure"][bet.structure].add(bet)
        segments["legs"][f"{len(legs)}-leg" if len(legs) != 1 else "single"].add(bet)
        segments["bookmaker"][bet.bookmaker_name or bet.bookmaker].add(bet)
        for league in sorted({leg.league or leg.sport_key or "unknown" for leg in legs}):
            segments["league"][league].add(bet)
        for market in sorted({(parse_market(leg.market).kind.value if parse_market(leg.market) else leg.market) for leg in legs}):
            segments["market"][market].add(bet)
    table = {axis: {name: stats.as_dict() for name, stats in rows.items()} for axis, rows in segments.items()}
    strengths, leaks = [], []
    for axis, rows in table.items():
        for name, row in rows.items():
            if row["bets"] < min_bets or row["roi"] is None:
                continue
            item = {"axis": axis, "segment": name, "bets": row["bets"], "roi": row["roi"], "pnl_inr": row["pnl_inr"], "win_rate": row["win_rate"]}
            if row["roi"] >= 0.05:
                strengths.append(item)
            elif row["roi"] <= -0.10:
                leaks.append(item)
    stakes = [float(b.stake_inr) for b in settled]
    return {
        "settled_bets": len(settled),
        "segments": table,
        "strengths": sorted(strengths, key=lambda r: -r["roi"]),
        "leaks": sorted(leaks, key=lambda r: r["roi"]),
        "average_odds": round(sum(odds_seen) / len(odds_seen), 3) if odds_seen else None,
        "average_stake_inr": str(_money(sum(stakes) / len(stakes))) if stakes else None,
        "min_bets": min_bets,
    }


# ================================================================ cache
def _cache_version_key(user_id: uuid.UUID) -> str:
    return f"oracle:pnl:version:{user_id}"


async def bump(redis: Redis | None, user_ids: Iterable[uuid.UUID]) -> None:
    if redis is None:
        return
    try:
        pipe = redis.pipeline(transaction=False)
        for user_id in set(user_ids):
            pipe.incr(_cache_version_key(user_id))
        await pipe.execute()
    except (RedisError, OSError):
        pass


async def cached(redis: Redis | None, user_id: uuid.UUID, name: str) -> tuple[str | None, dict[str, Any] | None]:
    """(the cache key for this user's current version, the cached value if any)."""
    if redis is None:
        return None, None
    try:
        version = await redis.get(_cache_version_key(user_id)) or "0"
        key = f"oracle:pnl:{user_id}:{version}:{name}"
        raw = await redis.get(key)
        return key, (json.loads(raw) if raw else None)
    except (RedisError, OSError, ValueError):
        return None, None


async def store(redis: Redis | None, key: str | None, value: dict[str, Any], ttl: int = 300) -> None:
    if redis is None or key is None:
        return
    try:
        await redis.set(key, json.dumps(value, default=str), ex=ttl)
    except (RedisError, OSError):
        pass


async def user_bets(session: AsyncSession, user_id: uuid.UUID) -> tuple[list[UserPlacedBet], dict[uuid.UUID, list[UserPlacedLeg]]]:
    bets = list((await session.execute(select(UserPlacedBet).where(UserPlacedBet.user_id == user_id).order_by(UserPlacedBet.placed_at.desc()))).scalars())
    legs: dict[uuid.UUID, list[UserPlacedLeg]] = defaultdict(list)
    if bets:
        for leg in (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id.in_([b.id for b in bets])).order_by(UserPlacedLeg.position))).scalars():
            legs[leg.bet_id].append(leg)
    return bets, legs


def system_lines(structure: str) -> int | None:
    kind = SlipKind(structure) if structure in SlipKind.__members__ else None
    return None if kind is None or kind not in SYSTEMS else len(lines_of(kind, SYSTEMS[kind][0]))


def settings_tax(settings: Settings, enabled: bool, rate: float | None) -> float | None:
    if not enabled:
        return None
    return settings.ORACLE_TDS_RATE if rate is None else rate
