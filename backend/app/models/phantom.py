"""PHANTOM (GARUDA): arbitrage opportunities and quantitative calculation logs."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import true

from app.models import Base


class CalcType(StrEnum):
    DUTCHING = "DUTCHING"
    MATCHED_BETTING = "MATCHED_BETTING"
    AVELLANEDA = "AVELLANEDA"
    COINTEGRATION = "COINTEGRATION"


def _in_clause(column: str, enum_cls: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class ArbitrageOpportunityModel(Base):
    __tablename__ = "phantom_arbitrage_opportunities"
    __table_args__ = (
        CheckConstraint(
            "total_implied_probability > 0 AND total_implied_probability < 1.0",
            name="ck_phantom_arb_total_probability_range",
        ),
        CheckConstraint("guaranteed_profit_pct > 0", name="ck_phantom_arb_profit_positive"),
        CheckConstraint("target_total_stake > 0", name="ck_phantom_arb_stake_positive"),
        CheckConstraint("length(stakes_json) >= 2", name="ck_phantom_arb_stakes_json_not_empty"),
        CheckConstraint("length(event_name) > 0", name="ck_phantom_arb_event_not_empty"),
        CheckConstraint("length(market_type) > 0", name="ck_phantom_arb_market_not_empty"),
        Index("ix_phantom_arb_active_created", "is_active", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    market_type: Mapped[str] = mapped_column(String(64), nullable=False)
    total_implied_probability: Mapped[float] = mapped_column(Float, nullable=False)
    guaranteed_profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    target_total_stake: Mapped[float] = mapped_column(Float, nullable=False)
    stakes_json: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PhantomCalculationLogModel(Base):
    __tablename__ = "phantom_calculations"
    __table_args__ = (
        CheckConstraint("length(inputs_json) >= 2", name="ck_phantom_calc_inputs_json_length"),
        CheckConstraint("length(outputs_json) >= 2", name="ck_phantom_calc_outputs_json_length"),
        CheckConstraint(_in_clause("calc_type", CalcType), name="ck_phantom_calc_type"),
        Index("ix_phantom_calculations_type_created", "calc_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    calc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    inputs_json: Mapped[str] = mapped_column(String(2048), nullable=False)
    outputs_json: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
