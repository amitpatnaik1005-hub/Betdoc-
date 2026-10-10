"""Omni-Ingestion persistence: provider vault, endpoints, immutable raw landing zone, quarantine."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
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
    Numeric,
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


class OmniFleetSource(Base):
    """Operator state for one code-defined fleet ingestor (``app.adapters.ingestion.INGESTORS``).

    Fleet Command writes ``is_enabled``, ``interval_seconds`` and the vault-encrypted API key; the
    workers write the run bookkeeping. ``paused_at`` is the dead-letter state: set after
    ``OMNI_FLEET_FAILURE_THRESHOLD`` consecutive failures, cleared when an operator re-enables it.
    """

    __tablename__ = "omni_fleet_sources"
    __table_args__ = (
        CheckConstraint("interval_seconds IS NULL OR interval_seconds >= 5", name="interval_floor"),
        CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
    )

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Config-driven providers (Universal Ingestion Matrix): the ProviderSpec JSON. None = a built-in adapter.
    spec: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pre-masked; never decrypt to display
    interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # None: the adapter's default
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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


# ---- The Vault: fleet credentials and accounts (Group 70) --------------------------------------
class VerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    OK = "OK"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"  # no sanctioned API to check it with: confirm it by hand


_VERIFICATION_VALUES = ", ".join(f"'{v.value}'" for v in VerificationStatus)
VAULT_MONEY = Numeric(18, 4)


class VaultBookmakerAccount(Base):
    """One bookmaker account in the Vault. Every secret column is AES-256-GCM ciphertext bound to this
    row and field (``vault-account:<id>:<field>``); ``identity_digest`` is a keyed HMAC of the login
    (or, without one, the API key), so a re-import finds the row without the plaintext."""

    __tablename__ = "vault_bookmaker_accounts"
    __table_args__ = (
        UniqueConstraint("bookmaker_id", "identity_digest", name="uq_vault_bookmaker_accounts_identity"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("balance IS NULL OR balance >= 0", name="balance_non_negative"),
        CheckConstraint("stake_cap IS NULL OR stake_cap > 0", name="stake_cap_positive"),
        CheckConstraint("priority >= 1", name="priority_positive"),
        CheckConstraint(f"verification_status IN ({_VERIFICATION_VALUES})", name="verification_status"),
        Index("ix_vault_bookmaker_accounts_book_active", "bookmaker_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bookmaker_id: Mapped[str] = mapped_column(String(32))  # canonical: parimatch, 1xbet, stake, pinnacle, betfair...
    label: Mapped[str] = mapped_column(String(128))
    identity_digest: Mapped[str] = mapped_column(String(64))
    username_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pre-masked: "pa***23"
    encrypted_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_totp_seed: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # keyed HMAC over every secret: a re-import sees a change without decrypting anything
    secrets_fingerprint: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8))
    adapter_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # 1 = the primary account of its bookmaker
    balance: Mapped[Decimal | None] = mapped_column(VAULT_MONEY, nullable=True)  # account currency, as the user last saw it
    stake_cap: Mapped[Decimal | None] = mapped_column(VAULT_MONEY, nullable=True)  # the user's own ceiling per order
    reserved: Mapped[Decimal] = mapped_column(VAULT_MONEY, default=Decimal(0))  # stakes of orders in flight on this account
    source: Mapped[str] = mapped_column(String(16), default="manual")
    verification_status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.UNVERIFIED.value)
    verification_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VaultAccountReservation(Base):
    """An order's stake held on one account until the order is settled, refused or abandoned."""

    __tablename__ = "vault_account_reservations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_vault_account_reservations_open", "account_id", "released_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vault_bookmaker_accounts.id", ondelete="CASCADE"))
    order_ref: Mapped[str] = mapped_column(String(128), unique=True)  # the order's idempotency key
    amount: Mapped[Decimal] = mapped_column(VAULT_MONEY)
    currency: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class VaultProviderCredential(Base):
    """A data provider's key (The Odds API, Pinnacle API, SharpAPI...). ``linked_source_id``: the Fleet
    Command source that runs on it (whose ``encrypted_api_key`` then holds the same key)."""

    __tablename__ = "vault_provider_credentials"
    __table_args__ = (
        UniqueConstraint("provider_id", "key_digest", name="uq_vault_provider_credentials_key"),
        CheckConstraint(f"verification_status IN ({_VERIFICATION_VALUES})", name="verification_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128))
    key_digest: Mapped[str] = mapped_column(String(64))
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    verification_status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.UNVERIFIED.value)
    verification_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VaultFleetConfig(Base):
    """The runtime fleet configuration (one row): what ``app.core.fleet_overlay`` installs everywhere."""

    __tablename__ = "vault_fleet_config"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    sports: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    markets_by_sport: Mapped[dict[str, str]] = mapped_column(JsonColumn, default=dict)
    quiet_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM", local time
    quiet_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    account_routing: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VaultImportRun(Base):
    """One import's audit trail: counts and warnings, never a value."""

    __tablename__ = "vault_import_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    origin: Mapped[str] = mapped_column(String(16))  # upload | text | path | cli | backup
    content_sha256: Mapped[str] = mapped_column(String(64))
    accounts_created: Mapped[int] = mapped_column(Integer, default=0)
    accounts_updated: Mapped[int] = mapped_column(Integer, default=0)
    accounts_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    providers_created: Mapped[int] = mapped_column(Integer, default=0)
    providers_updated: Mapped[int] = mapped_column(Integer, default=0)
    providers_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    sports_added: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    warnings: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
