"""The 5-pillar risk guard: every Omni trade passes all five or nothing is reserved.

1. Global kill switch: Redis ``betdoc:kill_switch`` set, or the Control Panel emergency stop
   (``max_daily_exposure <= 0``) -> HTTP 403. Checked first; it short-circuits everything.
2. Daily drawdown: realised P&L over the rolling 24h (``SETTLED`` rows of the audit log). Blocked
   when the net loss exceeds ``daily_drawdown_pct`` of the account's peak equity.
3. Consecutive losses: Redis counter ``betdoc:risk:streak:{user_id}``, maintained by settlement and
   recomputed from the ledger whenever the key is missing. Paused at ``max_loss_streak``.
4. Market exposure: ``SUM(stake_inr)`` of the user's PENDING bets on the fixture plus this stake,
   against ``max_market_exposure_pct`` of equity.
5. Velocity lock: the selection's best price over the last 60s (every board tick lands in a Redis
   history); blocked when its standard deviation exceeds ``velocity_max_cv_pct`` of its mean.

Fail closed: if Redis cannot answer, nothing trades (HTTP 503). The stateful SQL pillars (drawdown,
exposure) run again inside the locked transaction, because a check made before the lock can be
stale by the time the lock is held.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.live_odds import tick_history_key
from app.models.cfo_vault import AuditEvent, AuditLog, BankrollAccount, LedgerStatus, PhantomLedger, RiskGuardSettings
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.services.cfo_ledger import ZERO, CfoError, OrderTicket, loss_streak_from_db, streak_key

HUNDRED = Decimal(100)
TRUTHY = frozenset({"1", "true", "on", "yes"})
DEFAULT_LIMITS = {
    "daily_drawdown_pct": Decimal("10.00"),
    "max_market_exposure_pct": Decimal("10.00"),
    "max_loss_streak": 5,
    "velocity_max_cv_pct": Decimal("3.00"),
    "max_slippage_pct": Decimal("0.50"),
}


class RiskGuardViolation(CfoError):
    status_code = 403


@dataclass(frozen=True, slots=True)
class GuardLimits:
    daily_drawdown_pct: Decimal
    max_market_exposure_pct: Decimal
    max_loss_streak: int
    velocity_max_cv_pct: Decimal
    max_slippage_pct: Decimal = Decimal("0.50")

    @classmethod
    def of(cls, row: RiskGuardSettings | None) -> GuardLimits:
        if row is None:
            return cls(**DEFAULT_LIMITS)  # type: ignore[arg-type]
        return cls(
            Decimal(row.daily_drawdown_pct),
            Decimal(row.max_market_exposure_pct),
            int(row.max_loss_streak),
            Decimal(row.velocity_max_cv_pct),
            Decimal(row.max_slippage_pct if row.max_slippage_pct is not None else DEFAULT_LIMITS["max_slippage_pct"]),
        )


@dataclass(slots=True)
class GuardReport:
    """What each pillar measured, for the audit trail and the betslip."""

    pnl_24h: Decimal = ZERO
    drawdown_limit: Decimal | None = None
    loss_streak: int = 0
    market_exposure: Decimal = ZERO
    market_cap: Decimal | None = None
    velocity_cv_pct: Decimal | None = None
    velocity_points: int = 0

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "pnl_24h": str(self.pnl_24h),
            "drawdown_limit": None if self.drawdown_limit is None else str(self.drawdown_limit),
            "loss_streak": self.loss_streak,
            "market_exposure": str(self.market_exposure),
            "market_cap": None if self.market_cap is None else str(self.market_cap),
            "velocity_cv_pct": None if self.velocity_cv_pct is None else str(self.velocity_cv_pct),
            "velocity_points": self.velocity_points,
        }


async def load_limits(session: AsyncSession, user_id: uuid.UUID) -> GuardLimits:
    return GuardLimits.of(await session.get(RiskGuardSettings, user_id))


def _unavailable() -> RiskGuardViolation:
    return RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "Risk checks cannot run right now (Redis unreachable); nothing was executed", status_code=503)


def coefficient_of_variation(values: list[Decimal]) -> Decimal | None:
    """Sample standard deviation over the mean, in percent. None below two points or a zero mean."""
    if len(values) < 2:
        return None
    n = Decimal(len(values))
    mean = sum(values, ZERO) / n
    if mean <= 0:
        return None
    variance = sum(((v - mean) ** 2 for v in values), ZERO) / (n - 1)
    return variance.sqrt() / mean * HUNDRED


# ---------------------------------------------------------------- measurements (also shown on the betslip)
async def realized_pnl_24h(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> Decimal:
    """Net realised P&L of the rolling 24h, from the audit log's SETTLED rows."""
    pnl = await session.scalar(
        select(func.coalesce(func.sum(AuditLog.pnl_inr), 0)).where(
            AuditLog.user_id == user_id, AuditLog.event == AuditEvent.SETTLED, AuditLog.created_at >= now - timedelta(hours=24)
        )
    )
    return Decimal(str(pnl or 0))


def drawdown_limit(peak: Decimal, limits: GuardLimits) -> Decimal:
    return (peak * limits.daily_drawdown_pct / HUNDRED).quantize(Decimal("0.01"))


async def read_loss_streak(redis: Redis | None, settings: Settings, session: AsyncSession, user_id: uuid.UUID) -> int:
    """The Redis counter; recomputed from the ledger (and written back) when missing or corrupt.
    Raises the 503 violation when Redis cannot be read at all."""
    if redis is None:
        raise _unavailable()
    key = streak_key(settings, user_id)
    try:
        cached = await redis.get(key)
    except (RedisError, OSError) as exc:
        raise _unavailable() from exc
    if cached is not None:
        try:
            return max(int(cached), 0)
        except (TypeError, ValueError):
            pass
    streak = await loss_streak_from_db(session, user_id)
    try:
        await redis.set(key, streak)
    except (RedisError, OSError):
        pass
    return streak


class RiskGuard:
    def __init__(self, redis: Redis | None, settings: Settings, clock: Callable[[], datetime] | None = None) -> None:
        self.redis = redis
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

    # -------------------------------------------------------------- pillar 5 (checked first)
    async def kill_switch(self, session: AsyncSession) -> None:
        if self.redis is None:
            raise _unavailable()
        try:
            flag = await self.redis.get(self.settings.CFO_KILL_SWITCH_KEY)
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        if flag is not None and str(flag).strip().lower() in TRUTHY:
            raise RiskGuardViolation("BLOCKED_BY_KILL_SWITCH", "Trading is halted by the global kill switch")
        controls = await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
        if controls is not None and controls.max_daily_exposure <= 0:
            raise RiskGuardViolation("BLOCKED_BY_KILL_SWITCH", "Trading is halted by the emergency stop")

    # -------------------------------------------------------------- pillar 1
    async def drawdown(self, session: AsyncSession, user_id: uuid.UUID, limits: GuardLimits, account: BankrollAccount | None, report: GuardReport) -> None:
        report.pnl_24h = await realized_pnl_24h(session, user_id, self.clock())
        if account is None or account.peak_balance <= 0:
            return
        report.drawdown_limit = drawdown_limit(account.peak_balance, limits)
        loss = -report.pnl_24h if report.pnl_24h < 0 else ZERO
        if loss > report.drawdown_limit:
            raise RiskGuardViolation(
                "BLOCKED_BY_DRAWDOWN",
                f"24h loss {loss} exceeds {limits.daily_drawdown_pct}% of peak bankroll ({report.drawdown_limit})",
                detail=report.as_dict(),
            )

    # -------------------------------------------------------------- pillar 2
    async def loss_streak(self, session: AsyncSession, user_id: uuid.UUID, limits: GuardLimits, report: GuardReport) -> None:
        streak = await read_loss_streak(self.redis, self.settings, session, user_id)
        report.loss_streak = streak
        if streak >= limits.max_loss_streak:
            raise RiskGuardViolation(
                "BLOCKED_BY_LOSS_STREAK",
                f"{streak} losses in a row: trading pauses at {limits.max_loss_streak}",
                detail=report.as_dict(),
            )

    # -------------------------------------------------------------- pillar 3
    async def market_exposure(
        self, session: AsyncSession, ticket: OrderTicket, limits: GuardLimits, account: BankrollAccount | None, report: GuardReport
    ) -> None:
        open_stake = await session.scalar(
            select(func.coalesce(func.sum(PhantomLedger.stake_inr), 0)).where(
                PhantomLedger.user_id == ticket.user_id,
                PhantomLedger.fixture_id == ticket.fixture_id,
                PhantomLedger.status == LedgerStatus.PENDING,
            )
        )
        report.market_exposure = Decimal(str(open_stake or 0))
        if account is None:
            return  # no account yet: re-checked under the lock once it exists
        report.market_cap = (account.equity * limits.max_market_exposure_pct / HUNDRED).quantize(Decimal("0.01"))
        if report.market_exposure + ticket.stake_inr > report.market_cap:
            raise RiskGuardViolation(
                "BLOCKED_BY_MARKET_EXPOSURE",
                f"{report.market_exposure} already open on this fixture; {ticket.stake_inr} more breaks the {report.market_cap} cap",
                status_code=409,
                detail=report.as_dict(),
            )

    # -------------------------------------------------------------- pillar 4
    async def velocity(self, ticket: OrderTicket, limits: GuardLimits, report: GuardReport) -> None:
        if self.redis is None:
            raise _unavailable()
        now = self.clock().timestamp()
        key = tick_history_key(self.settings, f"{ticket.fixture_id}|{ticket.market}|{ticket.selection}")
        try:
            members: list[str] = await self.redis.zrangebyscore(key, now - self.settings.CFO_VELOCITY_WINDOW_SECONDS, "+inf")
        except (RedisError, OSError) as exc:
            raise _unavailable() from exc
        prices: list[Decimal] = []
        for member in members:
            try:
                price = Decimal(member.split("|")[1])
            except (IndexError, InvalidOperation):
                continue
            if price.is_finite() and price > 1:
                prices.append(price)
        report.velocity_points = len(prices)
        report.velocity_cv_pct = coefficient_of_variation(prices)
        if report.velocity_cv_pct is not None and report.velocity_cv_pct > limits.velocity_max_cv_pct:
            raise RiskGuardViolation(
                "BLOCKED_BY_VELOCITY",
                f"Price swinging {report.velocity_cv_pct.quantize(Decimal('0.01'))}% (std/mean, 60s) > {limits.velocity_max_cv_pct}% limit",
                status_code=409,
                detail=report.as_dict(),
            )

    # -------------------------------------------------------------- composition
    async def check(self, session: AsyncSession, ticket: OrderTicket, account: BankrollAccount | None) -> GuardReport:
        """All five pillars, kill switch first. Raises the first violation."""
        report = GuardReport()
        await self.kill_switch(session)
        limits = await load_limits(session, ticket.user_id)
        await self.drawdown(session, ticket.user_id, limits, account, report)
        await self.loss_streak(session, ticket.user_id, limits, report)
        await self.market_exposure(session, ticket, limits, account, report)
        await self.velocity(ticket, limits, report)
        return report

    async def recheck_locked(self, session: AsyncSession, ticket: OrderTicket, account: BankrollAccount, report: GuardReport) -> GuardReport:
        """The stateful pillars again, now that this transaction holds the bankroll lock."""
        limits = await load_limits(session, ticket.user_id)
        await self.drawdown(session, ticket.user_id, limits, account, report)
        await self.market_exposure(session, ticket, limits, account, report)
        return report


async def set_kill_switch(redis: Redis | None, settings: Settings, engaged: bool) -> bool:
    """The emergency stop and its resume flip the Redis switch every execution checks first."""
    if redis is None:
        return False
    try:
        if engaged:
            await redis.set(settings.CFO_KILL_SWITCH_KEY, "1")
        else:
            await redis.delete(settings.CFO_KILL_SWITCH_KEY)
    except (RedisError, OSError):
        return False
    return True


async def kill_switch_engaged(redis: Redis | None, settings: Settings) -> bool | None:
    """True/False, or None when Redis cannot say."""
    if redis is None:
        return None
    try:
        flag = await redis.get(settings.CFO_KILL_SWITCH_KEY)
    except (RedisError, OSError):
        return None
    return flag is not None and str(flag).strip().lower() in TRUTHY
