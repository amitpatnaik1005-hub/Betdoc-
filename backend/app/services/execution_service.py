import asyncio
import logging
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_api_key
from app.exchanges.base import ExchangeRejectionError
from app.exchanges.factory import get_exchange_adapter
from app.models import BetLedger, ExchangeAccount, RiskMandate
from app.schemas.execution import PlaceBetRequest

logger = logging.getLogger(__name__)

SCALE = Decimal("0.0001")
NUMERIC_16_4_MAX = Decimal("999999999999.9999")

PENDING_NETWORK = "PENDING_NETWORK"
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"

# Statuses that tie up capital. UNKNOWN stays reserved until manually resolved.
EXPOSURE_STATUSES = (PENDING_NETWORK, ACCEPTED, UNKNOWN)

EXCHANGE_TIMEOUT_SECONDS = 10.0


def _sanitize(value: Decimal, field: str) -> Decimal:
    """Quantize to Numeric(16,4) scale and enforce the column's precision bounds."""
    try:
        q = value.quantize(SCALE, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid numeric value for {field}"
        )
    if not q.is_finite() or abs(q) > NUMERIC_16_4_MAX:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} exceeds Numeric(16,4) bounds"
        )
    return q


async def _get_user_bet_by_key(
    db: AsyncSession, user_id: UUID, idempotency_key: str
) -> BetLedger | None:
    # Scoped to the user so a key collision never leaks another user's bet.
    result = await db.execute(
        select(BetLedger)
        .join(ExchangeAccount, ExchangeAccount.id == BetLedger.exchange_account_id)
        .where(
            BetLedger.idempotency_key == idempotency_key,
            ExchangeAccount.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


from datetime import datetime, timezone

async def _finalize(
    db: AsyncSession,
    bet: BetLedger,
    new_status: str,
    exchange_bet_id: str | None = None,
) -> None:
    """Phase 3: persist the terminal state of a bet."""
    bet.status = new_status
    if exchange_bet_id is not None:
        bet.exchange_bet_id = exchange_bet_id
    if new_status == REJECTED:
        bet.resolved_at = datetime.now(timezone.utc)
    
    db.add(bet)
    try:
        await db.commit()
        await db.refresh(bet)
    except SQLAlchemyError:
        # Row stays PENDING_NETWORK, so exposure remains reserved (fail-safe).
        # Log enough to reconcile manually against the exchange.
        logger.critical(
            "Failed to persist bet state: bet_id=%s target_status=%s exchange_bet_id=%s",
            bet.id,
            new_status,
            exchange_bet_id,
        )
        await db.rollback()
        raise


async def execute_bet(db: AsyncSession, user_id: UUID, req: PlaceBetRequest) -> BetLedger:
    # ------------------------------------------------------------------ #
    # Pre-flight (no lock held)
    # ------------------------------------------------------------------ #
    account = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.user_id == user_id,
                ExchangeAccount.exchange_name == req.exchange_name
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Exchange account not found")

    # Decrypt and build the adapter BEFORE reserving exposure. A decryption failure
    # after Phase 1 commits would strand the bet in PENDING_NETWORK.
    try:
        adapter = get_exchange_adapter(
            account.exchange_name,
            decrypt_api_key(account.api_key_encrypted),
            decrypt_api_key(account.api_secret_encrypted),
        )
    except Exception:
        logger.exception("Credential decryption failed for exchange_account_id=%s", account.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Exchange credentials unavailable"
        )

    stake = _sanitize(req.stake, "stake")
    odds = _sanitize(req.odds, "odds")
    true_probability = _sanitize(req.true_probability, "true_probability")

    if stake <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Stake rounds to zero at 4dp scale")
    if odds <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Odds must be greater than 1")

    # ------------------------------------------------------------------ #
    # Phase 1: Lock, validate risk, reserve exposure
    # ------------------------------------------------------------------ #
    mandate_result = await db.execute(
        select(RiskMandate).where(RiskMandate.user_id == user_id).with_for_update()
    )
    mandate = mandate_result.scalar_one_or_none()
    if mandate is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No Risk Mandate")

    from app.domain.risk.stop_loss import StopLossEngine
    stop_loss_engine = StopLossEngine()
    # Use max_daily_exposure as a proxy for bankroll for the trailing stop mechanism
    sl_status = await stop_loss_engine.check(db, user_id, bankroll=float(mandate.max_daily_exposure))
    if sl_status.is_triggered:
        await db.commit()  # Commit flushes the new StopLossEventModel rows
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, 
            f"Stop-loss triggered: {sl_status.trigger_reason}"
        )

    # Idempotent replay: return the existing bet and NEVER re-send it to the exchange.
    existing = await _get_user_bet_by_key(db, user_id, req.idempotency_key)
    if existing is not None:
        await db.commit()  # release lock
        return existing

    if stake > mandate.max_stake_per_bet:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Stake {stake} exceeds max_stake_per_bet {mandate.max_stake_per_bet}",
        )

    exposure_result = await db.execute(
        select(func.coalesce(func.sum(BetLedger.stake), 0))
        .select_from(BetLedger)
        .join(ExchangeAccount, ExchangeAccount.id == BetLedger.exchange_account_id)
        .where(
            ExchangeAccount.user_id == user_id,
            BetLedger.status.in_(EXPOSURE_STATUSES),
        )
    )
    current_exposure = Decimal(str(exposure_result.scalar_one()))

    if current_exposure + stake > mandate.max_daily_exposure:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Exposure limit breached: open={current_exposure}, "
            f"requested={stake}, limit={mandate.max_daily_exposure}",
        )

    bet = BetLedger(
        idempotency_key=req.idempotency_key,
        exchange_account_id=account.id,
        match_id=req.match_id,
        market_type=req.market_type,
        selection=req.selection,
        currency=req.currency,
        odds=odds,
        stake=stake,
        true_probability=true_probability,
        status=PENDING_NETWORK,
    )

    db.add(bet)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await _get_user_bet_by_key(db, user_id, req.idempotency_key)
        if existing is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Bet could not be recorded")
        return existing

    # Commit releases the mandate lock. Exposure is now reserved by the
    # PENDING_NETWORK row, so concurrent requests see it immediately.
    await db.commit()
    await db.refresh(bet)

    # ------------------------------------------------------------------ #
    # Phase 2: Network call (no lock held)
    # ------------------------------------------------------------------ #
    try:
        exchange_bet_id = await asyncio.wait_for(
            adapter.place_bet(
                match_id=bet.match_id,
                selection=bet.selection,
                odds=bet.odds,
                stake=bet.stake,
            ),
            timeout=EXCHANGE_TIMEOUT_SECONDS,
        )

    # ------------------------------------------------------------------ #
    # Phase 3: Reconcile
    # ------------------------------------------------------------------ #
    except ExchangeRejectionError as exc:
        # Definitive refusal: exposure is released.
        await _finalize(db, bet, REJECTED)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    except asyncio.CancelledError:
        # Client disconnect / worker shutdown. CancelledError is a BaseException,
        # so it would bypass `except Exception` and strand the bet. Shield the write.
        logger.warning("Bet placement cancelled mid-flight: bet_id=%s", bet.id)
        await asyncio.shield(_finalize(db, bet, UNKNOWN))
        raise

    except Exception as exc:
        # Timeout or unexpected failure: the bet may or may not be live on the exchange.
        logger.exception("Exchange outcome unknown: bet_id=%s", bet.id)
        await _finalize(db, bet, UNKNOWN)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Exchange API timed out. Status UNKNOWN."
        ) from exc

    await _finalize(db, bet, ACCEPTED, exchange_bet_id)
    return bet
