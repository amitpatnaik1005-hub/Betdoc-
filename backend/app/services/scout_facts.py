"""The live book the Scout Oracle briefs from (Group 100; shared with the Pocket Twin's /ask in Group 109).
Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dashboard import build_dashboard_summary
from app.domain.oracle_scout.manager import ScoutFacts

logger = logging.getLogger("betdoc.ashoka")


async def facts_for(user_id: UUID | None, db: AsyncSession) -> ScoutFacts | None:
    if user_id is None:
        return None
    try:
        summary = await build_dashboard_summary(user_id, db, 0.0)
    except Exception:  # noqa: BLE001 - a briefing without numbers beats no briefing
        logger.warning("ASHOKA: live book unavailable for %s", user_id, exc_info=True)
        await db.rollback()
        return None
    return ScoutFacts(
        bankroll=float(summary.total_bankroll),
        exposure=float(summary.current_exposure),
        daily_pnl=float(summary.daily_pnl),
        open_positions=int(summary.active_bets_count),
        win_rate_pct=float(summary.win_rate_pct),
        stop_loss_status=str(summary.stop_loss_status),
    )
