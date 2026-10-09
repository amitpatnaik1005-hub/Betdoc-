"""Omni-Sniper (Group 63): where orders go and how our ids become each bookmaker's ids.

* ``ExecutionVenue``: one bookmaker execution API. Credentials are one Fernet blob (MASTER_VAULT_KEY)
  that is only decrypted in memory while a session is being opened.
* ``EntityMapping``: canonical fixture / selection -> the venue's own event and outcome ids. Filled
  by catalog sync (the venue's event list resolved through the alias dictionary) or by hand.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")

AUTH_TYPES = ("oauth2_client_credentials", "static_bearer")
ADAPTERS = ("generic_json",)
MAPPING_KINDS = ("fixture", "selection")


class ExecutionVenue(Base):
    __tablename__ = "sniper_execution_venues"
    __table_args__ = (
        CheckConstraint("bets_per_second > 0 AND bets_per_second <= 50", name="rate_range"),
        CheckConstraint("burst >= 1 AND burst <= 50", name="burst_range"),
        CheckConstraint(f"auth_type IN ({', '.join(repr(a) for a in AUTH_TYPES)})", name="auth_type_valid"),
        CheckConstraint(f"adapter IN ({', '.join(repr(a) for a in ADAPTERS)})", name="adapter_valid"),
        CheckConstraint("commission_rate IS NULL OR (commission_rate >= 0 AND commission_rate < 0.5)", name="commission_range"),
        CheckConstraint("currency IS NULL OR length(currency) = 3", name="currency_code"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # the bookmaker_id signals carry
    display_name: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(32), default="generic_json")
    base_url: Mapped[str] = mapped_column(String(255))
    auth_type: Mapped[str] = mapped_column(String(32), default="oauth2_client_credentials")
    token_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refresh_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    place_path: Mapped[str] = mapped_column(String(128), default="/bets")
    status_path: Mapped[str] = mapped_column(String(128), default="/bets")
    events_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bets_per_second: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("2.00"))
    burst: Mapped[int] = mapped_column(Integer, default=2)
    # Extra bookmaker ids this venue executes for (a venue's own id always routes to it); ["*"] = any (sandbox only)
    routes: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    # Static outcome codes when the venue uses them for every event, e.g. {"HOME": "1", "DRAW": "X", "AWAY": "2"}
    selection_codes: Mapped[dict[str, str]] = mapped_column(JsonColumn, default=dict)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pre-masked, never decrypted to show
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)  # the in-process simulated bookmaker
    # Group 64: commission on net winnings and the account currency (None: the bookmaker's defaults in settings)
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EntityMapping(Base):
    __tablename__ = "sniper_entity_mappings"
    __table_args__ = (
        UniqueConstraint("venue_id", "kind", "canonical_key", name="uq_sniper_entity_mappings_key"),
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in MAPPING_KINDS)})", name="kind_valid"),
        CheckConstraint("length(remote_id) > 0", name="remote_id_not_empty"),
        Index("ix_sniper_entity_mappings_venue_remote", "venue_id", "kind", "remote_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[str] = mapped_column(ForeignKey("sniper_execution_venues.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    # fixture: the canonical fixture id; selection: "<fixture_id>|<market>|<selection>"
    canonical_key: Mapped[str] = mapped_column(String(256))
    remote_id: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32), default="manual")  # catalog | manual
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
