"""Multi-Sport Support: per-sport dynamic model configuration."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import true

from app.models import Base

SPORT_NAME_MAX_LENGTH = 32
CONFIG_JSON_MAX_LENGTH = 2048


class SportConfigModel(Base):
    __tablename__ = "sport_configs"
    __table_args__ = (
        CheckConstraint("length(sport_name) > 0", name="ck_sport_configs_sport_name_not_empty"),
        CheckConstraint("length(config_json) >= 2", name="ck_sport_configs_config_json_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_name: Mapped[str] = mapped_column(String(SPORT_NAME_MAX_LENGTH), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    config_json: Mapped[str] = mapped_column(String(CONFIG_JSON_MAX_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
