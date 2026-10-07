import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid, event
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship
from sqlalchemy.types import TypeEngine

from app.models import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class LegendaryBot(StrEnum):
    """The 17 commanders, in handoff order. Must match frontend/src/config/commanders.config.ts."""

    ASHOKA = "ASHOKA"  # Oracle: prediction engine
    KAUTILYA = "KAUTILYA"  # Command Center: master control
    BAJIRAO = "BAJIRAO"  # The Arena: live execution
    VIDUR = "VIDUR"  # The Hive / The Wire: social & news sentiment
    KUMBHA = "KUMBHA"  # The Vault: capital management
    PANINI = "PANINI"  # The Lab: quantitative research
    PRATAP = "PRATAP"  # Core: system architecture
    GARUDA = "GARUDA"  # Phantom: stealth scraping
    TODAR_MAL = "TODAR MAL"  # Archive: data warehouse
    ARYABHATA = "ARYABHATA"  # Math engine: probability distributions
    CHANAKYA = "CHANAKYA"  # Risk management: Kelly criterion
    SHIVAJI = "SHIVAJI"  # Security: VaultCrypto
    DRONA = "DRONA"  # Training: ML ops
    BHEESHMA = "BHEESHMA"  # Rules & compliance: rate limiting
    KARNA = "KARNA"  # Competitive intel: odds shopping
    ARJUNA = "ARJUNA"  # Sniper: high-frequency execution
    DEVRAYA = "DEVRAYA"  # Visualization: UI rendering


class BotStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    SLEEPING = "SLEEPING"
    WORKING = "WORKING"
    DEGRADED = "DEGRADED"
    FATAL = "FATAL"


class TaskStatus(StrEnum):
    BACKLOG = "BACKLOG"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


def _enum_type(enum_cls: type[StrEnum], name: str) -> SAEnum:
    # values_callable persists VALUES ("TODAR MAL"), not member NAMES ("TODAR_MAL").
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
        create_constraint=True,
    )


def _jsonb(*, none_as_null: bool = False) -> TypeEngine[Any]:
    return JSON(none_as_null=none_as_null).with_variant(JSONB(none_as_null=none_as_null), "postgresql")


LEGENDARY_BOT_TYPE = _enum_type(LegendaryBot, "hive_legendary_bot")
BOT_STATUS_TYPE = _enum_type(BotStatus, "hive_bot_status")
TASK_STATUS_TYPE = _enum_type(TaskStatus, "hive_task_status")


class ImmutableLedgerError(RuntimeError):
    """Raised when code attempts to mutate an immutable self-learning ledger row."""


class BotProfileModel(Base):
    __tablename__ = "hive_bot_profiles"
    __table_args__ = (
        CheckConstraint("uptime_seconds >= 0", name="ck_hive_bot_uptime_non_negative"),
        CheckConstraint("tasks_completed >= 0", name="ck_hive_bot_tasks_completed_non_negative"),
        CheckConstraint("error_count >= 0", name="ck_hive_bot_error_count_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_name: Mapped[LegendaryBot] = mapped_column(LEGENDARY_BOT_TYPE, unique=True)
    status: Mapped[BotStatus] = mapped_column(BOT_STATUS_TYPE, default=BotStatus.OFFLINE, index=True)
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_ping_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resource_metrics: Mapped[dict[str, Any]] = mapped_column(_jsonb(), default=dict)


class HiveTaskModel(Base):
    __tablename__ = "hive_tasks"
    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 100", name="ck_hive_task_priority_range"),
        CheckConstraint("max_retries >= 0", name="ck_hive_task_max_retries_non_negative"),
        CheckConstraint(
            "retry_count >= 0 AND retry_count <= max_retries", name="ck_hive_task_retry_count_bounds"
        ),
        Index("ix_hive_task_status_priority", "status", "priority"),
        Index("ix_hive_task_payload_gin", "payload", postgresql_using="gin").ddl_if(dialect="postgresql"),
        Index("ix_hive_task_result_payload_gin", "result_payload", postgresql_using="gin").ddl_if(
            dialect="postgresql"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    assignee_name: Mapped[LegendaryBot | None] = mapped_column(LEGENDARY_BOT_TYPE, nullable=True, index=True)
    status: Mapped[TaskStatus] = mapped_column(TASK_STATUS_TYPE, default=TaskStatus.BACKLOG)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    max_retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payload: Mapped[dict[str, Any]] = mapped_column(_jsonb(), default=dict)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(_jsonb(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # lazy="raise": any un-eager-loaded access fails loudly (N+1 guard). Always use selectinload.
    parents: Mapped[list["HiveTaskModel"]] = relationship(
        "HiveTaskModel",
        secondary="hive_task_dependencies",
        primaryjoin="HiveTaskModel.id == HiveTaskDependencyModel.child_task_id",
        secondaryjoin="HiveTaskModel.id == HiveTaskDependencyModel.parent_task_id",
        viewonly=True,
        lazy="raise",
    )
    children: Mapped[list["HiveTaskModel"]] = relationship(
        "HiveTaskModel",
        secondary="hive_task_dependencies",
        primaryjoin="HiveTaskModel.id == HiveTaskDependencyModel.parent_task_id",
        secondaryjoin="HiveTaskModel.id == HiveTaskDependencyModel.child_task_id",
        viewonly=True,
        lazy="raise",
    )


class HiveTaskDependencyModel(Base):
    __tablename__ = "hive_task_dependencies"
    __table_args__ = (
        CheckConstraint("parent_task_id <> child_task_id", name="ck_hive_dependency_no_self_loop"),
        Index("ix_hive_task_dependencies_child", "child_task_id"),  # PK covers parent-first lookups
    )

    parent_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hive_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    child_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hive_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SelfLearningLogModel(Base):
    __tablename__ = "hive_self_learning_logs"
    __table_args__ = (
        CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0", name="ck_hive_learning_confidence_range"
        ),
        Index("ix_hive_learning_bot_param_created", "bot_name", "parameter_name", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_name: Mapped[LegendaryBot] = mapped_column(LEGENDARY_BOT_TYPE, index=True)
    parameter_name: Mapped[str] = mapped_column(String(255), index=True)
    old_value: Mapped[Any | None] = mapped_column(_jsonb(none_as_null=True), nullable=True)
    new_value: Mapped[Any] = mapped_column(_jsonb())
    reasoning: Mapped[str] = mapped_column(Text)
    confidence_score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


@event.listens_for(SelfLearningLogModel, "before_update")
def _forbid_ledger_update(mapper: Mapper[Any], connection: Connection, target: SelfLearningLogModel) -> None:
    raise ImmutableLedgerError(f"SelfLearningLog {target.id} is immutable; append a new entry instead")


@event.listens_for(SelfLearningLogModel, "before_delete")
def _forbid_ledger_delete(mapper: Mapper[Any], connection: Connection, target: SelfLearningLogModel) -> None:
    raise ImmutableLedgerError(f"SelfLearningLog {target.id} is immutable and cannot be deleted")
