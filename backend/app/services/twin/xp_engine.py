"""Experience (FA-2, Group 75): XP for discipline, wins, lessons and the losses the shield turned away.

Every award is a ``user_xp_audit_logs`` row keyed by what earned it (``source_ref``), unique per profile and
kind, inserted ``ON CONFLICT DO NOTHING`` under the profile's row lock: the vetting run, the feedback sweep
and a retry can all try to pay one award and it is paid once. The rank is the highest of ``XP_TIERS`` the
total reaches; ranks describe progress and unlock nothing (no execution right depends on XP).

    SLIP_VETTED        XP_AWARD_SLIP_VETTED        a slip cleared every enforced pillar (once per slip)
    BET_WON            XP_AWARD_BET_WON            a placed bet settled WON
    LOSS_PREVENTED     XP_AWARD_LOSS_PREVENTED     a leg pillar 15 vetoed went on to lose (measured, once the score is in)
    MISTAKE_MEMORIZED  XP_AWARD_MISTAKE_MEMORIZED  a lost leg became a lesson
    STREAK_BONUS       XP_AWARD_STREAK_BONUS       every XP_STREAK_DAYS consecutive days (ORACLE_TIMEZONE) with a fortress
                                                   run and no bet placed without a vetted audit
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.digital_twin import TwinVettingAudit
from app.models.never_forget import UserXPProfile, XPActionType, XPAuditLog
from app.models.user_bets_ledger import UserPlacedBet

logger = logging.getLogger("betdoc.xp")

COUNTERS = {
    XPActionType.SLIP_VETTED: "slips_vetted_count", XPActionType.BET_WON: "bets_won_count", XPActionType.LOSS_PREVENTED: "losses_prevented_count",
    XPActionType.MISTAKE_MEMORIZED: "mistakes_learned_count", XPActionType.STREAK_BONUS: "streak_bonuses_count",
}


def insert_ignore(session: AsyncSession, model: Any) -> Any:
    """INSERT ... ON CONFLICT DO NOTHING on PostgreSQL and SQLite."""
    return (postgresql.insert(model) if session.get_bind().dialect.name == "postgresql" else sqlite.insert(model)).on_conflict_do_nothing()


def amount_for(action: XPActionType, settings: Settings) -> int:
    return int(getattr(settings, f"XP_AWARD_{action.value}"))


# ================================================================ ranks
@dataclass(frozen=True, slots=True)
class Tier:
    level: int
    rank: str
    floor: int  # the XP that reaches it
    next_floor: int | None  # the XP that reaches the next; None: the top rank
    next_rank: str | None

    def progress(self, xp: int) -> float:
        if self.next_floor is None:
            return 100.0
        return round(min(100.0, max(0.0, (xp - self.floor) / (self.next_floor - self.floor) * 100.0)), 1)


def tier_for(xp: int, settings: Settings) -> Tier:
    tiers = list(settings.XP_TIERS.items())
    level = max(i for i, (_, floor) in enumerate(tiers) if xp >= floor)
    rank, floor = tiers[level]
    nxt = tiers[level + 1] if level + 1 < len(tiers) else None
    return Tier(level + 1, rank, floor, None if nxt is None else nxt[1], None if nxt is None else nxt[0])


# ================================================================ awards
@dataclass(frozen=True, slots=True)
class Award:
    user_id: uuid.UUID
    action: XPActionType
    source_ref: str  # what earned it; one award per (user, action, ref)
    description: str
    metadata: dict[str, Any] | None = None


async def profile_for(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime, *, lock: bool = False) -> UserXPProfile:
    first = next(iter(settings.XP_TIERS))
    await session.execute(insert_ignore(session, UserXPProfile).values(
        id=uuid.uuid4(), user_id=user_id, total_xp=0, level=1, rank_title=first, slips_vetted_count=0, bets_won_count=0, losses_prevented_count=0,
        mistakes_learned_count=0, streak_bonuses_count=0, created_at=now, updated_at=now,
    ))
    query = select(UserXPProfile).where(UserXPProfile.user_id == user_id).execution_options(populate_existing=True)
    return (await session.execute(query.with_for_update() if lock else query)).scalar_one()


async def award(session: AsyncSession, settings: Settings, item: Award, now: datetime) -> int:
    """Pay one award inside the caller's transaction; returns the XP paid (0: it was paid before)."""
    amount = amount_for(item.action, settings)
    profile = await profile_for(session, item.user_id, settings, now, lock=True)
    inserted = (await session.execute(insert_ignore(session, XPAuditLog).values(
        id=uuid.uuid4(), profile_id=profile.id, action_type=item.action.value, xp_amount=amount, source_ref=item.source_ref[:160],
        description=item.description[:255], metadata_snapshot=item.metadata or {}, created_at=now,
    ).returning(XPAuditLog.id))).scalar_one_or_none()
    if inserted is None:
        return 0
    total = profile.total_xp + amount
    tier = tier_for(total, settings)
    counter = COUNTERS[item.action]
    await session.execute(update(UserXPProfile).where(UserXPProfile.id == profile.id).values(
        total_xp=total, level=tier.level, rank_title=tier.rank, last_action_at=now, updated_at=now, **{counter: getattr(profile, counter) + 1},
    ))
    if tier.level != profile.level:
        logger.info("xp: user %s reached %s (%d XP)", item.user_id, tier.rank, total)
    return amount


async def award_all(sessions: async_sessionmaker[AsyncSession], settings: Settings, items: list[Award], now: datetime) -> int:
    """Pay several awards, each user's in one transaction (in user order: the profile locks never cross)."""
    paid = 0
    by_user: dict[uuid.UUID, list[Award]] = {}
    for item in items:
        by_user.setdefault(item.user_id, []).append(item)
    for user_id in sorted(by_user, key=str):
        async with sessions() as session:
            for item in by_user[user_id]:
                paid += await award(session, settings, item, now)
            await session.commit()
    return paid


# ================================================================ the streak
def _local(moment: datetime, zone: ZoneInfo) -> date:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone).date()


async def streak_days(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime) -> int:
    """Consecutive days up to today (ORACLE_TIMEZONE) with a fortress run and no bet placed without a vetted audit."""
    zone = ZoneInfo(settings.ORACLE_TIMEZONE)
    today = _local(now, zone)
    since = now - timedelta(days=settings.XP_STREAK_DAYS * 4 + 1)
    runs = {_local(t, zone) for t in (await session.execute(
        select(TwinVettingAudit.created_at).where(TwinVettingAudit.user_id == user_id, TwinVettingAudit.created_at >= since)
    )).scalars()}
    placed = (await session.execute(
        select(UserPlacedBet.placed_at, TwinVettingAudit.is_vetted)
        .join(TwinVettingAudit, TwinVettingAudit.id == UserPlacedBet.vetting_audit_id, isouter=True)
        .where(UserPlacedBet.user_id == user_id, UserPlacedBet.placed_at >= since)
    )).all()
    lapses = {_local(at, zone) for at, vetted in placed if not vetted}
    days, day = 0, today
    while day in runs and day not in lapses:
        days += 1
        day -= timedelta(days=1)
    return days


async def streak_award(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime) -> Award | None:
    days = await streak_days(session, user_id, settings, now)
    if days == 0 or days % settings.XP_STREAK_DAYS:
        return None
    today = _local(now, ZoneInfo(settings.ORACLE_TIMEZONE))
    return Award(user_id, XPActionType.STREAK_BONUS, f"streak:{today.isoformat()}", f"{days} disciplined days in a row: every bet vetted first", {"days": days})


# ================================================================ views
def profile_view(profile: UserXPProfile | None, user_id: uuid.UUID, settings: Settings, fleet_xp: int, credit: str) -> dict[str, Any]:
    xp = 0 if profile is None else profile.total_xp
    tier = tier_for(xp, settings)
    return {
        "user_id": str(user_id), "total_xp": xp, "level": tier.level, "rank_title": tier.rank, "current_level_min_xp": tier.floor,
        "next_level_xp": tier.next_floor, "next_rank": tier.next_rank, "progress_pct": tier.progress(xp),
        "slips_vetted_count": 0 if profile is None else profile.slips_vetted_count, "bets_won_count": 0 if profile is None else profile.bets_won_count,
        "losses_prevented_count": 0 if profile is None else profile.losses_prevented_count,
        "mistakes_learned_count": 0 if profile is None else profile.mistakes_learned_count,
        "streak_bonuses_count": 0 if profile is None else profile.streak_bonuses_count,
        "last_action_at": None if profile is None or profile.last_action_at is None else profile.last_action_at.isoformat(),
        "tiers": [{"level": i + 1, "rank": rank, "min_xp": floor} for i, (rank, floor) in enumerate(settings.XP_TIERS.items())],
        "awards": {a.value: amount_for(a, settings) for a in XPActionType}, "streak_days_required": settings.XP_STREAK_DAYS,
        "fleet_total_xp": fleet_xp, "developer_credit": credit,
    }


async def fleet_xp(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(UserXPProfile.total_xp), 0))) or 0)


def log_view(row: XPAuditLog) -> dict[str, Any]:
    return {"id": str(row.id), "action_type": row.action_type, "xp_amount": row.xp_amount, "source_ref": row.source_ref, "description": row.description,
            "metadata": row.metadata_snapshot, "created_at": row.created_at.isoformat()}
