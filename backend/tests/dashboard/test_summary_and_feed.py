"""Dashboard summary + activity feed treat accepted-but-unsettled bets as open positions."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.domain.dashboard.activity_feed import _fetch_bet_events
from app.domain.dashboard.summary_builder import build_dashboard_summary
from app.models import BetLedger, ExchangeAccount, RiskMandate, User

TABLES = [User.__table__, ExchangeAccount.__table__, RiskMandate.__table__, BetLedger.__table__]


def _sqlite_metadata() -> MetaData:
    """Copies of TABLES minus Postgres-only regex checks (`~`), which SQLite can't compile."""
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for c in [c for c in copy.constraints if isinstance(c, CheckConstraint) and "~" in str(c.sqltext)]:
            copy.constraints.discard(c)
    return md


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_sqlite_metadata().create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def _seed(db: AsyncSession) -> uuid.UUID:
    user = User(username="quant", hashed_password="x")
    db.add(user)
    await db.flush()
    account = ExchangeAccount(user_id=user.id, exchange_name="Pinnacle", api_key_encrypted="k", api_secret_encrypted="s")
    db.add(account)
    await db.flush()

    def bet(status: str, stake: str, payout: str | None = None) -> BetLedger:
        return BetLedger(
            idempotency_key=str(uuid.uuid4()), exchange_account_id=account.id, match_id="evt-ars-che",
            market_type="Match Odds", selection="HOME", odds=Decimal("2.10"), stake=Decimal(stake),
            payout=None if payout is None else Decimal(payout), true_probability=Decimal("0.48"), status=status,
            resolved_at=datetime.now(UTC) if payout is not None else None,
        )

    db.add_all([bet("ACCEPTED", "100"), bet("PENDING_NETWORK", "40"), bet("WON", "50", "105"), bet("REJECTED", "999")])
    await db.commit()
    return user.id


@pytest.mark.asyncio
async def test_summary_counts_accepted_bets_as_open_exposure(session: AsyncSession) -> None:
    user_id = await _seed(session)
    summary = await build_dashboard_summary(user_id, session)
    assert summary.active_bets_count == 2
    assert summary.current_exposure == 140.0
    # Bankroll is configured capital plus realised P&L (the WON bet: +55), never a constant.
    assert summary.total_bankroll == get_settings().starting_bankroll + 55.0
    assert summary.win_rate_pct == 100.0


@pytest.mark.asyncio
async def test_feed_reports_pnl_only_for_settled_bets(session: AsyncSession) -> None:
    user_id = await _seed(session)
    events = {e.metadata["status"]: e for e in await _fetch_bet_events(session, user_id, 10)}
    assert events["ACCEPTED"].metadata["pnl"] is None
    assert "P&L" not in events["ACCEPTED"].message and "HOME @ 2.10" in events["ACCEPTED"].message
    assert events["REJECTED"].metadata["pnl"] is None
    assert events["WON"].metadata["pnl"] == 55.0
