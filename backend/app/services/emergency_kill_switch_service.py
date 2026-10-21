"""Group 86: the emergency fortress (KAUTILYA, CHANAKYA & SHIVAJI). Developer Credit: Amit Ashok Kumar Patnaik.

One service keeps four places in step: the fortress row (``emergency_kill_switch_states``), the Control Panel
(``system_settings``: bots off and zero daily exposure on a kill), Redis (the master kill switch and one lock key per
book, ``EMERGENCY_BOOK_LOCK_PREFIX:<book>``), and ``bookmaker_configs.is_active``. Every kill, resume, automatic trip
and bookmaker lock appends an audit entry chained to the previous one by SHA-256; the dashboard re-verifies the chain.

* The per-book lock is enforced where real money moves: the real-money gateway refuses a ticket for a locked book
  (``bookmaker_locked``). A global kill locks both books.
* Measurements, never guesses: the reference bankroll is the larger of the real bankrolls' funds (cash + locked
  exposure) and the CFO main accounts' high-water mark; the rolling loss is the net of the CFO ledger's SETTLED P&L
  inside the window (when negative); the streak counts the latest settled losses. A missing bankroll leaves the
  drawdown empty. USD only at TODAR MAL's fresh rate (no refresh triggered here).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.control_panel import emergency_circuit_math as ecm
from app.domain.control_panel.manager import ControlPanelManager
from app.models.bookmakers import BookmakerConfigModel
from app.models.cfo_vault import AuditEvent, AuditLog, BankrollAccount
from app.models.emergency_kill_switch import (
    EMERGENCY_STATE_SINGLETON_ID,
    EmergencyEventType,
    EmergencyKillSwitchAuditModel,
    EmergencyKillSwitchStateModel,
    EmergencyTargetScope,
    LockoutMode,
)
from app.models.real_money import RealBankroll
from app.schemas.control_panel import (
    AutoDrawdownCheckRequest,
    AutoDrawdownCheckResponse,
    BookmakerLockRequest,
    EmergencyAuditRead,
    EmergencyDashboardResponse,
    EmergencyResumeRequest,
    EmergencyStateRead,
    EmergencyTriggerRequest,
)
from app.services.aryabhata_pipeline import publish_risk_limits
from app.services.risk_guard import set_kill_switch
from app.services.sentinel_bus import emit_alert
from app.services.sentinel_watch import kill_switch_alert

logger = logging.getLogger("betdoc.emergency_fortress")

BOOKS = {"PARIMATCH": "parimatch", "1XBET": "1xbet"}  # scope -> canonical book
BOOK_ALIASES = {"parimatch": "parimatch", "1xbet": "1xbet", "onexbet": "1xbet", "one_xbet": "1xbet"}
AUTO = "CHANAKYA_AUTO_CIRCUIT_BREAKER"
STREAK_DEPTH = 25


def book_key(settings: Settings, book: str) -> str:
    return f"{settings.EMERGENCY_BOOK_LOCK_PREFIX}:{book}"


async def bookmaker_locked(redis: Redis | None, settings: Settings, bookmaker: str) -> bool:
    """Is this book under a safety lock? Read by the real-money gateway before every ticket."""
    book = BOOK_ALIASES.get(bookmaker.strip().casefold())
    if book is None or redis is None:
        return False
    try:
        return bool(await redis.exists(book_key(settings, book)))
    except (RedisError, OSError):
        return False


def _stamp(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _entry_hash(prev: str, a: EmergencyKillSwitchAuditModel) -> str:
    return ecm.compute_emergency_audit_hash(prev_hash=prev, event_type=a.event_type, target_scope=a.target_scope, triggered_by=a.triggered_by, reason=a.reason,
                                            drawdown_pct=None if a.drawdown_pct_snapshot is None else float(a.drawdown_pct_snapshot),
                                            protected_capital_inr=a.protected_capital_inr, timestamp_iso=_stamp(a.created_at))


class EmergencyKillSwitchService:
    def __init__(self) -> None:
        self._control = ControlPanelManager()

    # ---------------------------------------------------------------- measurements
    async def _usd_rate(self, db: AsyncSession, redis: Redis | None, settings: Settings, now: datetime) -> Decimal | None:
        from app.services.currency_service import CurrencyService, CurrencyUnavailableError  # noqa: PLC0415

        try:
            return (await CurrencyService(settings, redis).inr_per_unit(db, "USD", now, allow_refresh=False)).inr_per_unit
        except CurrencyUnavailableError:
            return None

    async def measure(self, db: AsyncSession, window_hours: int, now: datetime) -> tuple[Decimal | None, Decimal, int]:
        """(reference bankroll, rolling loss, consecutive losses), all in INR, from the real and CFO ledgers."""
        real = Decimal(str(await db.scalar(select(func.coalesce(func.sum(RealBankroll.cash_balance + RealBankroll.locked_exposure), 0))) or 0))
        cfo = Decimal(str(await db.scalar(select(func.coalesce(func.sum(BankrollAccount.peak_balance), 0)).where(BankrollAccount.bot_id.is_(None))) or 0))
        bankroll = max(real, cfo)
        net = Decimal(str(await db.scalar(select(func.coalesce(func.sum(AuditLog.pnl_inr), 0)).where(
            AuditLog.event == AuditEvent.SETTLED, AuditLog.created_at >= now - timedelta(hours=max(1, window_hours)))) or 0))
        loss = (-net).quantize(ecm.CENT, rounding=ROUND_HALF_UP) if net < 0 else Decimal("0.00")
        streak = 0
        for pnl in (await db.execute(select(AuditLog.pnl_inr).where(AuditLog.event == AuditEvent.SETTLED, AuditLog.pnl_inr.is_not(None))
                                     .order_by(AuditLog.created_at.desc()).limit(STREAK_DEPTH))).scalars():
            if pnl < 0:
                streak += 1
            elif pnl > 0:
                break
        return (bankroll.quantize(ecm.CENT, rounding=ROUND_HALF_UP) if bankroll > 0 else None), loss, streak

    # ---------------------------------------------------------------- state and audit
    async def _state(self, db: AsyncSession, redis: Redis | None, settings: Settings) -> EmergencyKillSwitchStateModel:
        state = await db.get(EmergencyKillSwitchStateModel, EMERGENCY_STATE_SINGLETON_ID, populate_existing=True)
        if state is not None:
            return state
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        bankroll, loss, streak = await self.measure(db, settings.EMERGENCY_AUTO_KILL_WINDOW_HOURS, now)
        state = EmergencyKillSwitchStateModel(
            id=EMERGENCY_STATE_SINGLETON_ID, global_kill_engaged=False, lockout_mode=LockoutMode.ARMED.value, parimatch_locked=False, onexbet_locked=False,
            auto_kill_drawdown_pct=Decimal(f"{settings.EMERGENCY_AUTO_KILL_DRAWDOWN_PCT:.2f}"), auto_kill_window_hours=settings.EMERGENCY_AUTO_KILL_WINDOW_HOURS,
            max_consecutive_losses=settings.EMERGENCY_MAX_CONSECUTIVE_LOSSES, reference_bankroll_inr=bankroll,
            reference_bankroll_usd=ecm.convert_inr_to_usd(bankroll, rate), current_drawdown_inr=loss, current_drawdown_usd=ecm.convert_inr_to_usd(loss, rate),
            current_drawdown_pct=None if bankroll is None else (loss / bankroll * 100).quantize(ecm.CENT, rounding=ROUND_HALF_UP),
            current_consecutive_losses=streak, updated_at=now,
        )
        db.add(state)
        await db.commit()
        await db.refresh(state)
        return state

    async def _audit(self, db: AsyncSession, state: EmergencyKillSwitchStateModel, rate: Decimal | None, *, event: EmergencyEventType,
                     scope: EmergencyTargetScope, by: str, reason: str) -> None:
        last = (await db.execute(select(EmergencyKillSwitchAuditModel).order_by(EmergencyKillSwitchAuditModel.created_at.desc()).limit(1))).scalars().first()
        protected = None if state.reference_bankroll_inr is None else max(Decimal("0.00"), state.reference_bankroll_inr - state.current_drawdown_inr)
        entry = EmergencyKillSwitchAuditModel(
            id=uuid4(), event_type=event.value, target_scope=scope.value, triggered_by=by[:128], reason=reason[:512], drawdown_pct_snapshot=state.current_drawdown_pct,
            protected_capital_inr=protected, protected_capital_usd=ecm.convert_inr_to_usd(protected, rate), prev_hash=ecm.GENESIS_AUDIT_HASH if last is None else last.entry_hash,
            entry_hash="", created_at=datetime.now(UTC),
        )
        entry.entry_hash = _entry_hash(entry.prev_hash, entry)
        db.add(entry)

    async def _sync_books(self, db: AsyncSession, redis: Redis | None, settings: Settings, state: EmergencyKillSwitchStateModel) -> None:
        """The Redis lock keys and ``bookmaker_configs.is_active`` follow the fortress row."""
        for book, locked in (("parimatch", state.parimatch_locked), ("1xbet", state.onexbet_locked)):
            row = (await db.execute(select(BookmakerConfigModel).where(func.lower(BookmakerConfigModel.name) == book))).scalars().first()
            if row is not None:
                row.is_active = not locked
            if redis is not None:
                try:
                    await (redis.set(book_key(settings, book), "1") if locked else redis.delete(book_key(settings, book)))
                except (RedisError, OSError):
                    logger.warning("EMERGENCY: the %s lock key could not reach Redis", book)

    async def _halt(self, db: AsyncSession, redis: Redis | None, settings: Settings, by: str) -> None:
        row = await self._control.emergency_stop(db)
        await set_kill_switch(redis, settings, engaged=True)
        await publish_risk_limits(redis, row, settings)
        await emit_alert(redis, settings, kill_switch_alert(engaged=True, by=by))

    async def dashboard(self, db: AsyncSession, redis: Redis | None, settings: Settings, limit: int = 25) -> EmergencyDashboardResponse:
        from app.services.twin.vetting import developer_credit  # noqa: PLC0415 - pulls in the oracle engine

        state = await self._state(db, redis, settings)
        chain = list((await db.execute(select(EmergencyKillSwitchAuditModel).order_by(EmergencyKillSwitchAuditModel.created_at))).scalars())
        intact, prev = True, ecm.GENESIS_AUDIT_HASH
        for a in chain:
            if a.prev_hash != prev or _entry_hash(prev, a) != a.entry_hash:
                intact = False
                break
            prev = a.entry_hash
        return EmergencyDashboardResponse(state=EmergencyStateRead.model_validate(state), audits=[EmergencyAuditRead.model_validate(a) for a in reversed(chain[-limit:])],
                                          chain_intact=intact, architect=await developer_credit(db))

    # ---------------------------------------------------------------- actions
    async def engage_global_kill(self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: EmergencyTriggerRequest) -> EmergencyDashboardResponse:
        state = await self._state(db, redis, settings)
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        await self._halt(db, redis, settings, payload.triggered_by)
        await db.refresh(state)  # the halt committed: reload before touching the row
        state.global_kill_engaged, state.lockout_mode, state.parimatch_locked, state.onexbet_locked = True, LockoutMode.MANUAL_KILL.value, True, True
        state.last_triggered_by, state.last_trigger_reason = payload.triggered_by, payload.reason
        state.cooldown_expires_at, state.updated_at = now + timedelta(minutes=settings.EMERGENCY_MANUAL_COOLDOWN_MINUTES), now
        await self._sync_books(db, redis, settings, state)
        await self._audit(db, state, rate, event=EmergencyEventType.GLOBAL_KILL_ENGAGED, scope=EmergencyTargetScope.GLOBAL, by=payload.triggered_by, reason=payload.reason)
        await db.commit()
        logger.critical("EMERGENCY: global kill engaged by %s (%s)", payload.triggered_by, payload.reason)
        return await self.dashboard(db, redis, settings)

    async def resume_global_operations(self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: EmergencyResumeRequest) -> EmergencyDashboardResponse:
        state = await self._state(db, redis, settings)
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        row = await self._control.update_settings(db, updates={"bots_enabled": True, "max_daily_exposure": float(payload.restore_daily_exposure_inr)})
        await db.refresh(state)  # the update committed
        state.global_kill_engaged, state.lockout_mode, state.parimatch_locked, state.onexbet_locked = False, LockoutMode.ARMED.value, False, False
        state.current_consecutive_losses, state.cooldown_expires_at, state.updated_at = 0, None, now
        state.last_triggered_by, state.last_trigger_reason = payload.triggered_by, payload.reason
        await self._sync_books(db, redis, settings, state)
        await self._audit(db, state, rate, event=EmergencyEventType.GLOBAL_KILL_RESUMED, scope=EmergencyTargetScope.GLOBAL, by=payload.triggered_by, reason=payload.reason)
        await db.commit()
        await db.refresh(row)  # the commit expired it; the risk limits read it
        await set_kill_switch(redis, settings, engaged=False)
        await publish_risk_limits(redis, row, settings)
        await emit_alert(redis, settings, kill_switch_alert(engaged=False, by=payload.triggered_by))
        logger.warning("EMERGENCY: operations resumed by %s", payload.triggered_by)
        return await self.dashboard(db, redis, settings)

    async def set_bookmaker_lock(self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: BookmakerLockRequest) -> EmergencyDashboardResponse:
        state = await self._state(db, redis, settings)
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        if payload.bookmaker == "PARIMATCH":
            state.parimatch_locked, scope = payload.locked, EmergencyTargetScope.PARIMATCH
        else:
            state.onexbet_locked, scope = payload.locked, EmergencyTargetScope.ONEXBET
        state.last_triggered_by, state.last_trigger_reason, state.updated_at = payload.triggered_by, payload.reason, now
        await self._sync_books(db, redis, settings, state)
        await self._audit(db, state, rate, event=EmergencyEventType.BOOKMAKER_LOCKED if payload.locked else EmergencyEventType.BOOKMAKER_UNLOCKED,
                          scope=scope, by=payload.triggered_by, reason=payload.reason)
        await db.commit()
        return await self.dashboard(db, redis, settings)

    async def evaluate_and_enforce_auto_drawdown(self, db: AsyncSession, redis: Redis | None, settings: Settings,
                                                 payload: AutoDrawdownCheckRequest) -> AutoDrawdownCheckResponse:
        state = await self._state(db, redis, settings)
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        bankroll, loss, streak = await self.measure(db, payload.window_hours, now)
        result = ecm.evaluate_auto_drawdown_breaker(
            reference_bankroll_inr=Decimal(str(payload.reference_bankroll_inr)) if payload.reference_bankroll_inr is not None else bankroll,
            rolling_loss_inr=Decimal(str(payload.rolling_loss_inr)) if payload.rolling_loss_inr is not None else loss,
            threshold_drawdown_pct=payload.threshold_drawdown_pct, window_hours=payload.window_hours,
            consecutive_losses=payload.consecutive_losses if payload.consecutive_losses is not None else streak,
            max_consecutive_losses=payload.max_consecutive_losses, drawdown_cooldown_minutes=settings.EMERGENCY_DRAWDOWN_COOLDOWN_MINUTES,
            streak_cooldown_minutes=settings.EMERGENCY_STREAK_COOLDOWN_MINUTES, usd_inr_rate=rate,
        )
        state.reference_bankroll_inr, state.reference_bankroll_usd = result.reference_bankroll_inr, result.reference_bankroll_usd
        state.current_drawdown_inr, state.current_drawdown_usd = result.rolling_loss_inr, result.rolling_loss_usd
        state.current_drawdown_pct = None if result.drawdown_pct is None else Decimal(f"{result.drawdown_pct:.2f}")
        state.auto_kill_drawdown_pct, state.auto_kill_window_hours = Decimal(f"{result.threshold_drawdown_pct:.2f}"), result.window_hours
        state.current_consecutive_losses, state.max_consecutive_losses, state.updated_at = result.consecutive_losses, result.max_consecutive_losses, now
        if result.should_trip_breaker:
            await self._halt(db, redis, settings, AUTO)
            await db.refresh(state)  # the halt committed the measurements above; reload them
            state.global_kill_engaged, state.lockout_mode, state.parimatch_locked, state.onexbet_locked = True, result.lockout_mode, True, True
            state.last_triggered_by, state.last_trigger_reason = AUTO, result.verdict_summary[:512]
            state.cooldown_expires_at = now + timedelta(minutes=result.recommended_cooldown_minutes)
            await self._sync_books(db, redis, settings, state)
            event = EmergencyEventType.AUTO_DRAWDOWN_TRIPPED if result.lockout_mode == LockoutMode.AUTO_DRAWDOWN_TRIPPED.value else EmergencyEventType.STREAK_LOCKOUT_TRIPPED
            await self._audit(db, state, rate, event=event, scope=EmergencyTargetScope.GLOBAL, by=AUTO, reason=result.verdict_summary)
        await db.commit()
        await db.refresh(state)
        measured = payload.reference_bankroll_inr is None and payload.rolling_loss_inr is None and payload.consecutive_losses is None
        return AutoDrawdownCheckResponse(
            tripped=result.should_trip_breaker, lockout_mode=state.lockout_mode, drawdown_pct=result.drawdown_pct,  # type: ignore[arg-type]
            threshold_drawdown_pct=result.threshold_drawdown_pct, rolling_loss_inr=float(result.rolling_loss_inr),
            rolling_loss_usd=None if result.rolling_loss_usd is None else float(result.rolling_loss_usd),
            reference_bankroll_inr=None if result.reference_bankroll_inr is None else float(result.reference_bankroll_inr),
            reference_bankroll_usd=None if result.reference_bankroll_usd is None else float(result.reference_bankroll_usd),
            consecutive_losses=result.consecutive_losses, max_consecutive_losses=result.max_consecutive_losses, measured=measured,
            verdict_summary=result.verdict_summary, state=EmergencyStateRead.model_validate(state),
        )
