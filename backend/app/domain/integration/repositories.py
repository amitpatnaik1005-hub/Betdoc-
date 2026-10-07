"""Stateless async repositories (SQLAlchemy 2.0 ``select()`` only; session is always injected per call)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.integration.models import (
    ApiCredential,
    BetRecord,
    BetStatus,
    EventResearch,
    PredictionRecord,
    SubsystemConstraint,
    utcnow,
)
from app.domain.integration.schemas import ArenaMetrics, LabMetrics, OracleMetrics, VaultMetrics

ORACLE_SUBSYSTEM = "oracle"
ARENA_SUBSYSTEM = "arena"
ORACLE_AUTOMATION_FLAG = "automated_predictions_enabled"
ARENA_LIVE_BETTING_FLAG = "live_betting_enabled"


class ConstraintRepository:
    async def is_enabled(self, session: AsyncSession, subsystem: str, flag: str) -> bool:
        row = await session.get(SubsystemConstraint, (subsystem, flag))
        return True if row is None else row.enabled

    async def set_flag(
        self, session: AsyncSession, *, subsystem: str, flag: str, enabled: bool, reason: str | None
    ) -> SubsystemConstraint:
        row = await session.get(SubsystemConstraint, (subsystem, flag))
        if row is None:
            row = SubsystemConstraint(subsystem=subsystem, flag=flag, enabled=enabled, reason=reason)
            session.add(row)
        else:
            row.enabled, row.reason, row.updated_at = enabled, reason, utcnow()
        await session.flush()
        return row


class VaultRepository:
    async def add_credential(self, session: AsyncSession, *, provider: str, encrypted_key: str) -> ApiCredential:
        row = ApiCredential(provider=provider, encrypted_key=encrypted_key)
        session.add(row)
        await session.flush()
        return row

    async def get_credential(self, session: AsyncSession, credential_id: int) -> ApiCredential | None:
        return await session.get(ApiCredential, credential_id)

    async def metrics(self, session: AsyncSession) -> VaultMetrics:
        stmt = select(func.count(ApiCredential.id), func.count(distinct(ApiCredential.provider)))
        total, providers = (await session.execute(stmt)).one()
        return VaultMetrics(credentials_stored=int(total), providers=int(providers))


class OracleRepository:
    async def get_research(self, session: AsyncSession, event_id: str) -> EventResearch | None:
        return await session.get(EventResearch, event_id)

    async def get_prediction(self, session: AsyncSession, prediction_id: int) -> PredictionRecord | None:
        return await session.get(PredictionRecord, prediction_id)

    async def add_prediction(self, session: AsyncSession, record: PredictionRecord) -> PredictionRecord:
        session.add(record)
        await session.flush()
        await session.refresh(record)
        return record

    async def metrics(self, session: AsyncSession, *, automated_enabled: bool) -> OracleMetrics:
        stmt = select(
            func.count(PredictionRecord.id),
            func.coalesce(func.sum(case((PredictionRecord.approved.is_(True), 1), else_=0)), 0),
            func.max(PredictionRecord.created_at),
        )
        total, approved, last_at = (await session.execute(stmt)).one()
        last: datetime | None = last_at
        return OracleMetrics(
            predictions_total=int(total),
            approved_predictions=int(approved),
            automated_predictions_enabled=automated_enabled,
            last_prediction_at=last,
        )


class ArenaRepository:
    async def add_bet(self, session: AsyncSession, bet: BetRecord) -> BetRecord:
        session.add(bet)
        await session.flush()
        await session.refresh(bet)
        return bet

    async def get_bet(self, session: AsyncSession, bet_id: int) -> BetRecord | None:
        return await session.get(BetRecord, bet_id)

    async def realized_pnl(self, session: AsyncSession) -> float:
        value = (await session.execute(select(func.coalesce(func.sum(BetRecord.pnl), 0.0)))).scalar_one()
        return float(value)

    async def open_exposure(self, session: AsyncSession) -> float:
        stmt = select(func.coalesce(func.sum(BetRecord.stake), 0.0)).where(BetRecord.status == BetStatus.OPEN)
        return float((await session.execute(stmt)).scalar_one())

    async def metrics(self, session: AsyncSession, *, live_enabled: bool) -> ArenaMetrics:
        is_open = BetRecord.status == BetStatus.OPEN
        stmt = select(
            func.coalesce(func.sum(case((is_open, 1), else_=0)), 0),
            func.coalesce(func.sum(case((is_open, 0), else_=1)), 0),
            func.coalesce(func.sum(case((is_open, BetRecord.stake), else_=0.0)), 0.0),
            func.coalesce(func.sum(BetRecord.pnl), 0.0),
        )
        open_bets, settled, exposure, pnl = (await session.execute(stmt)).one()
        return ArenaMetrics(
            open_bets=int(open_bets),
            settled_bets=int(settled),
            open_exposure=float(exposure),
            realized_pnl=float(pnl),
            live_betting_enabled=live_enabled,
        )


class LabRepository:
    async def metrics(self, session: AsyncSession) -> LabMetrics:
        stmt = (
            select(
                func.count(BetRecord.id),
                func.coalesce(func.sum(case((BetRecord.status == BetStatus.WON, 1), else_=0)), 0),
                func.avg(PredictionRecord.edge),
            )
            .join(PredictionRecord, PredictionRecord.id == BetRecord.prediction_id)
            .where(BetRecord.status.in_([BetStatus.WON, BetStatus.LOST]))
        )
        settled, wins, avg_edge = (await session.execute(stmt)).one()
        n = int(settled)
        return LabMetrics(
            settled_bets=n,
            hit_rate=(int(wins) / n) if n else None,
            average_edge=float(avg_edge) if avg_edge is not None else None,
        )
