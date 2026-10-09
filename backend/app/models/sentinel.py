"""The Sentinel (Group 68): outbound alert channels, the routing matrix, and the alert trail.

* ``sentinel_channels``: one row per dispatcher (Telegram, Discord, Twilio, PagerDuty). Secrets (bot
  token, webhook URL, auth token, routing key) are one JSON object encrypted with the master vault
  key (``app.core.security_vault``); the API only ever shows ``credentials_hint``. The non-secret
  half (chat ids, phone numbers, whether FATAL also places a voice call) is plain JSON.
* ``sentinel_routing``: the single routing matrix row, severity (and the daily hype) -> channels.
* ``sentinel_alerts``: every alert the bus carried, once, keyed by its own id (the dispatcher writes
  it when it takes the alert off the stream, so a redelivery never duplicates it).
* ``sentinel_deliveries``: what happened on each channel: sent, failed, batched into a digest, or
  the digest itself.
* ``sentinel_commands``: every Telegram command received, authorised or not, and what it did.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.models import Base, utc_now


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2, Severity.FATAL: 3}


class ChannelName(StrEnum):
    TELEGRAM = "TELEGRAM"
    DISCORD = "DISCORD"
    TWILIO = "TWILIO"
    PAGERDUTY = "PAGERDUTY"


class DeliveryStatus(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    BATCHED = "BATCHED"  # held by the debouncer, went out inside a digest
    DIGEST = "DIGEST"  # the digest message itself
    SKIPPED = "SKIPPED"  # routed, but the channel is disabled or not configured


class SentinelChannel(Base):
    __tablename__ = "sentinel_channels"

    channel: Mapped[str] = mapped_column(String(16), primary_key=True)  # ChannelName
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now())


class SentinelRouting(Base):
    __tablename__ = "sentinel_routing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    matrix: Mapped[dict[str, list[str]]] = mapped_column(JSON, default=dict)  # INFO|WARNING|CRITICAL|FATAL|HYPE -> [channel]
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now())


class SentinelAlertLog(Base):
    __tablename__ = "sentinel_alerts"
    __table_args__ = (
        Index("ix_sentinel_alerts_occurred_at", "occurred_at"),
        Index("ix_sentinel_alerts_kind_occurred_at", "kind", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stream_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


class SentinelDelivery(Base):
    __tablename__ = "sentinel_deliveries"
    __table_args__ = (
        Index("ix_sentinel_deliveries_alert_id", "alert_id"),
        Index("ix_sentinel_deliveries_attempted_at", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # None: a digest
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(10))  # DeliveryStatus
    digest_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    batched: Mapped[int] = mapped_column(Integer, default=0)  # a digest: how many alerts it carried
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SentinelCommandLog(Base):
    __tablename__ = "sentinel_commands"
    __table_args__ = (Index("ix_sentinel_commands_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(64), nullable=True)  # the Telegram username or id, never a name
    command: Mapped[str] = mapped_column(String(32))
    authorised: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(32))  # HALTED, RESUMED, STATUS, CONFIRM_SENT, REFUSED, UNKNOWN, IGNORED
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
