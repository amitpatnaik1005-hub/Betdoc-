"""The Lab's quant toolkit (PANINI): the log of every dutching, matched-betting, market-making and cointegration run.

Group 96 retired arbitrage and the Phantom section. ``phantom_arbitrage_opportunities`` stays in the schema history
but is no longer mapped (``alembic/env.py`` keeps autogenerate from proposing to drop it); this table keeps its name.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base


class CalcType(StrEnum):
    DUTCHING = "DUTCHING"
    MATCHED_BETTING = "MATCHED_BETTING"
    AVELLANEDA = "AVELLANEDA"
    COINTEGRATION = "COINTEGRATION"


def _in_clause(column: str, enum_cls: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


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
