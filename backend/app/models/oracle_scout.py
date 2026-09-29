"""Scout Oracle (ASHOKA): context-aware sidebar chat history."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base


class OracleScoutHistoryModel(Base):
    __tablename__ = "oracle_scout_history"
    __table_args__ = (
        CheckConstraint("length(user_message) > 0", name="ck_oracle_scout_history_message_not_empty"),
        CheckConstraint("length(oracle_response) > 0", name="ck_oracle_scout_history_response_not_empty"),
        Index("ix_oracle_scout_history_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    page_context: Mapped[str | None] = mapped_column(String(120), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    oracle_response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
