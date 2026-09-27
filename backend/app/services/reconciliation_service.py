import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BetLedger
from app.schemas.admin import ResolveBetRequest
from app.services.execution_service import (
    EXCHANGE_TIMEOUT_SECONDS,
    PENDING_NETWORK,
    UNKNOWN,
)

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 60
SWEEP_BATCH_SIZE = 1000

# A bet still inside its Phase 2 network call must never be swept.
# Its row is not locked during Phase 2, so the time gap is the only guard.
assert STALE_AFTER_SECONDS > EXCHANGE_TIMEOUT_SECONDS * 3, (
    "STALE_AFTER_SECONDS must comfortably exceed EXCHANGE_TIMEOUT_SECONDS"
)


async def sweep_stale_bets(db: AsyncSession) -> int:
    """Move stranded PENDING_NETWORK bets to UNKNOWN.

    Entirely database-side: no IDs are loaded into Python. Rows are claimed with
    FOR UPDATE SKIP LOCKED so concurrent sweepers split the work instead of
    blocking each other. Each batch is its own short transaction, which keeps
    lock counts, transaction size, and WAL bursts bounded on very large backlogs.
    Exposure stays reserved because UNKNOWN counts as open exposure.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
    total = 0

    while True:
        stale_ids = (
            select(BetLedger.id)
            .where(
                BetLedger.status == PENDING_NETWORK,
                BetLedger.placed_at < cutoff,
            )
            .order_by(BetLedger.placed_at)
            .limit(SWEEP_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )

        stmt = (
            update(BetLedger)
            .where(BetLedger.id.in_(stale_ids))
            # Re-check in the outer UPDATE. This is harmless, and it guards
            # against a row that changed state between subquery and update.
            .where(BetLedger.status == PENDING_NETWORK)
            .values(status=UNKNOWN)
            .execution_options(synchronize_session=False)
        )

        result = await db.execute(stmt)
        await db.commit()

        swept = result.rowcount or 0
        total += swept
        if swept < SWEEP_BATCH_SIZE:
            break

    if total:
        logger.warning("Reconciliation sweep moved %d stale bets to UNKNOWN", total)
    return total


async def resolve_unknown_bet(
    db: AsyncSession,
    bet_id: UUID,
    req: ResolveBetRequest,
    admin_id: UUID,
) -> BetLedger:
    result = await db.execute(
        select(BetLedger).where(BetLedger.id == bet_id).with_for_update()
    )
    bet = result.scalar_one_or_none()
    if bet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bet not found")

    if bet.status != UNKNOWN:
        await db.rollback()  # release lock
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only UNKNOWN bets can be resolved"
        )

    previous_status = bet.status
    bet.status = req.status
    if req.exchange_bet_id is not None:
        bet.exchange_bet_id = req.exchange_bet_id
    if req.status == "REJECTED":
        bet.resolved_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(bet)

    logger.critical(
        "ADMIN_AUDIT bet_resolved admin_id=%s bet_id=%s from_status=%s to_status=%s "
        "exchange_bet_id=%s",
        admin_id,
        bet.id,
        previous_status,
        bet.status,
        bet.exchange_bet_id,
    )
    return bet
