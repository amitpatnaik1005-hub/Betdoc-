"""Omni-Ingestion persistence: provider vault, endpoints, immutable raw landing zone, quarantine."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base, utc_now as utcnow

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class OmniAuthStrategy(StrEnum):
    HEADER = "HEADER"
    QUERY = "QUERY"
    BEARER = "BEARER"
    PROTOCOL = "PROTOCOL"
    MESSAGE = "MESSAGE"
    NONE = "NONE"


class OmniHealth(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class SourceTimestampOrigin(StrEnum):
    PAYLOAD = "PAYLOAD"
    HEADER = "HEADER"
    INGESTION = "INGESTION"


WS_STRATEGIES: frozenset[OmniAuthStrategy] = frozenset({OmniAuthStrategy.PROTOCOL, OmniAuthStrategy.MESSAGE})


class OmniProviderConfig(Base):
    __tablename__ = "omni_provider_configs"
    __table_args__ = (
        CheckConstraint("category_code IN ('A','B','C','D','E','F','G','H')", name="ck_omni_provider_category"),
        CheckConstraint("requests_per_minute > 0", name="ck_omni_provider_rpm_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    category_code: Mapped[str] = mapped_column(String(1), index=True)
    base_url: Mapped[str] = mapped_column(Text)
    ws_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_strategy: Mapped[OmniAuthStrategy] = mapped_column(Enum(OmniAuthStrategy, native_enum=False, length=16))
    auth_param_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pre-masked; never decrypt to display
    ws_auth_payload: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    ws_subscribe_payloads: Mapped[list[Any] | None] = mapped_column(JsonColumn, nullable=True)
    default_headers: Mapped[dict[str, str] | None] = mapped_column(JsonColumn, nullable=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    queue_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adapter_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalization_spec: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    health_status: Mapped[str] = mapped_column(String(16), default=OmniHealth.UNKNOWN.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    endpoints: Mapped[list[OmniProviderEndpoint]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", lazy="raise"
    )


class OmniProviderEndpoint(Base):
    __tablename__ = "omni_provider_endpoints"
    __table_args__ = (
        UniqueConstraint("provider_id", "http_method", "path", name="uq_omni_endpoint"),
        CheckConstraint("http_method IN ('GET','POST')", name="ck_omni_endpoint_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("omni_provider_configs.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    http_method: Mapped[str] = mapped_column(String(8), default="GET")
    query_params: Mapped[dict[str, str] | None] = mapped_column(JsonColumn, nullable=True)
    request_body: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    min_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    provider: Mapped[OmniProviderConfig] = relationship(back_populates="endpoints", lazy="raise")


class OmniRawPayload(Base):
    """Append-only landing zone (UPDATE/DELETE blocked by trigger on PostgreSQL)."""

    __tablename__ = "omni_raw_payloads"
    __table_args__ = (Index("ix_omni_raw_provider_ingested", "provider_id", "ingestion_timestamp"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # RESTRICT: deleting a provider must never cascade into the immutable ledger.
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("omni_provider_configs.id", ondelete="RESTRICT"))
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("omni_provider_endpoints.id", ondelete="SET NULL"), nullable=True
    )
    endpoint_path: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(8))  # "rest" | "ws"
    raw_payload: Mapped[dict[str, Any] | list[Any]] = mapped_column(JsonColumn)
    is_json: Mapped[bool] = mapped_column(Boolean)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)  # SHA-256 of the exact received bytes
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_timestamp_origin: Mapped[str] = mapped_column(String(16))
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 101 for WebSocket frames


class OmniQuarantineLog(Base):
    __tablename__ = "omni_quarantine_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(256), index=True)
    reason: Mapped[str] = mapped_column(String(64))
    variance: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    events: Mapped[list[Any]] = mapped_column(JsonColumn)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---- Immutability: DB-level (authoritative) + ORM-level (fast fail) ----
_IMMUTABLE_FN = DDL(
    """
    CREATE OR REPLACE FUNCTION omni_raw_payload_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'omni_raw_payloads is append-only (% blocked)', TG_OP;
    END;
    $$;
    """
)
_IMMUTABLE_TRIGGER = DDL(
    "CREATE TRIGGER trg_omni_raw_payload_immutable BEFORE UPDATE OR DELETE ON omni_raw_payloads "
    "FOR EACH ROW EXECUTE FUNCTION omni_raw_payload_immutable();"
)
event.listen(OmniRawPayload.__table__, "after_create", _IMMUTABLE_FN.execute_if(dialect="postgresql"))
event.listen(OmniRawPayload.__table__, "after_create", _IMMUTABLE_TRIGGER.execute_if(dialect="postgresql"))


@event.listens_for(OmniRawPayload, "before_update")
def _block_raw_update(*_: object) -> None:
    raise PermissionError("OmniRawPayload rows are immutable.")


@event.listens_for(OmniRawPayload, "before_delete")
def _block_raw_delete(*_: object) -> None:
    raise PermissionError("OmniRawPayload rows are immutable.")
