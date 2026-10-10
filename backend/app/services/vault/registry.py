"""The Vault's accounts and provider keys: masked views, edits, execution credentials, backup (Group 70).

Nothing here returns a secret to an API caller. ``account_secrets`` opens an account's credentials for
the duration of one ``with`` block (the execution engine's use), and ``venue_for_account`` hands them
to an execution venue sealed again, for the session manager to open the same way it opens a venue's own.

The disaster-recovery backup is a single JSON document sealed with AES-256-GCM under a key derived
from the operator's passphrase (scrypt, N=2^15, r=8, p=1): it opens on any BetDoc install, without the
MASTER_VAULT_KEY that sealed the live rows, and without the passphrase it is noise.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.core.security_vault import VaultCrypto, VaultDecryptionError
from app.models.omni_vault import OmniFleetSource, VaultAccountReservation, VaultBookmakerAccount, VaultProviderCredential
from app.services.vault import catalog
from app.services.vault.account_rotator import capacity, free_funds
from app.services.vault.markdown_importer import (
    ParsedAccount,
    ParsedProvider,
    ParseResult,
    account_context,
    account_fingerprint,
    identity_digests,
    import_parsed,
    mask_identity,
    provider_context,
)

ACCOUNT_SECRET_COLUMNS = {
    "username": "encrypted_username", "password": "encrypted_password", "api_key": "encrypted_api_key", "token": "encrypted_token",
    "totp_seed": "encrypted_totp_seed", "notes": "encrypted_notes", "url": "encrypted_target_url",
}
BACKUP_FORMAT = "betdoc-vault-backup"
_BACKUP_AAD = b"betdoc-vault-backup-v1"
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


class VaultConflictError(ValueError):
    """The change would clash with another row (another account already has that login)."""


class BackupError(ValueError):
    """A backup that is malformed, or a passphrase that does not open it."""


def _str(value: Decimal | None) -> str | None:
    return None if value is None else format(Decimal(value).normalize(), "f")


# ------------------------------------------------------------------------------------------ views
def account_view(row: VaultBookmakerAccount, open_holds: int = 0) -> dict[str, Any]:
    funds, cap = free_funds(row), capacity(row)
    return {
        "id": str(row.id), "bookmaker_id": row.bookmaker_id, "bookmaker_name": catalog.bookmaker_display(row.bookmaker_id),
        "label": row.label, "username_hint": row.username_hint or (f"{row.bookmaker_id}_user_***" if row.encrypted_username else None),
        "has_password": bool(row.encrypted_password), "has_api_key": bool(row.encrypted_api_key), "has_token": bool(row.encrypted_token),
        "has_2fa": bool(row.encrypted_totp_seed), "has_notes": bool(row.encrypted_notes), "target_host": row.target_host,
        "currency": row.currency, "adapter": row.adapter_key or catalog.adapter_key(row.bookmaker_id), "is_active": bool(row.is_active),
        "priority": int(row.priority or 100), "balance": _str(row.balance), "stake_cap": _str(row.stake_cap), "reserved": _str(row.reserved or Decimal(0)),
        "free_funds": _str(funds), "capacity": _str(cap), "open_holds": open_holds,
        "verification_status": row.verification_status, "verification_detail": row.verification_detail,
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "source": row.source, "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def provider_view(row: VaultProviderCredential) -> dict[str, Any]:
    return {
        "id": str(row.id), "provider_id": row.provider_id,
        "provider_name": catalog.provider_display(row.provider_id) if catalog.is_known_provider(row.provider_id) else row.label,
        "generic": not catalog.is_known_provider(row.provider_id), "label": row.label,
        "key_hint": row.api_key_hint, "has_secret": bool(row.encrypted_secret), "base_url": row.base_url,
        "linked_source_id": row.linked_source_id, "fleet_source": catalog.PROVIDER_FLEET_SOURCE.get(row.provider_id),
        "is_active": bool(row.is_active), "verification_status": row.verification_status, "verification_detail": row.verification_detail,
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None, "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def open_holds(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = (await session.execute(
        select(VaultAccountReservation.account_id, func.count()).where(VaultAccountReservation.released_at.is_(None)).group_by(VaultAccountReservation.account_id)
    )).all()
    return {account_id: int(n) for account_id, n in rows}


async def list_accounts(session: AsyncSession) -> list[dict[str, Any]]:
    holds = await open_holds(session)
    rows = (await session.execute(select(VaultBookmakerAccount).order_by(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.priority, VaultBookmakerAccount.created_at))).scalars()
    return [account_view(r, holds.get(r.id, 0)) for r in rows]


async def list_providers(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(VaultProviderCredential).order_by(VaultProviderCredential.provider_id, VaultProviderCredential.created_at))).scalars()
    return [provider_view(r) for r in rows]


# ------------------------------------------------------------------------------------------ secrets
@contextlib.contextmanager
def account_secrets(vault: VaultCrypto, row: VaultBookmakerAccount) -> Iterator[dict[str, str]]:
    """The account's credentials, plaintext only inside the block. ``VaultDecryptionError`` if any field fails."""
    opened: dict[str, str] = {}
    try:
        for name, column in ACCOUNT_SECRET_COLUMNS.items():
            cipher = getattr(row, column)
            if cipher:
                opened[name] = vault.decrypt_key(cipher, context=account_context(row.id, name))
        yield opened
    finally:
        opened.clear()


def venue_for_account(venue: VenueConfig, row: VaultBookmakerAccount, vault: VaultCrypto) -> VenueConfig:
    """The venue, logging in as this account: its credentials sealed the way venue credentials are, its
    own session cache (``session_scope``) and its currency."""
    with account_secrets(vault, row) as secrets:
        creds = {k: secrets[k] for k in ("username", "password", "token") if k in secrets}
        key = secrets.get("api_key") or secrets.get("token")
        if key:
            creds["api_key"] = key
            creds["app_key"] = secrets.get("api_key") or key  # Betfair's X-Application
        blob = vault.encrypt_key(json.dumps(creds, separators=(",", ":")))
        creds.clear()
    return dataclasses.replace(venue, encrypted_credentials=blob, session_scope=str(row.id), currency=row.currency.upper())


# ------------------------------------------------------------------------------------------ edits
async def create_account(session: AsyncSession, vault: VaultCrypto, settings: Settings, data: ParsedAccount, actor_id: uuid.UUID | None) -> VaultBookmakerAccount:
    if data.identity is None:
        raise ValueError("an account needs a login (username / email / phone), an API key or a token")
    if await _account_by_identity(session, vault, data.bookmaker_id, data.identity) is not None:
        raise VaultConflictError(f"a {catalog.bookmaker_display(data.bookmaker_id)} account with that login is already in the Vault")
    await import_parsed(session, vault, ParseResult(accounts=[data]), settings, origin="manual", content_sha256="manual", actor_id=actor_id)
    row = await _account_by_identity(session, vault, data.bookmaker_id, data.identity)
    assert row is not None
    return row


async def _account_by_identity(session: AsyncSession, vault: VaultCrypto, book: str, identity: str) -> VaultBookmakerAccount | None:
    return (await session.execute(
        select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == book, VaultBookmakerAccount.identity_digest.in_(identity_digests(vault, book, identity)))
    )).scalars().first()


async def update_account(session: AsyncSession, vault: VaultCrypto, row: VaultBookmakerAccount, changes: dict[str, Any], secrets: dict[str, str | None]) -> VaultBookmakerAccount:
    """Plain fields from ``changes``; ``secrets``: a value replaces the field, None clears it (the login cannot be cleared)."""
    for name in ("label", "currency", "priority", "balance", "stake_cap", "is_active"):
        if name in changes:
            value = changes[name]
            setattr(row, name, value.upper() if name == "currency" and value else value)
    if not secrets:
        return row
    with account_secrets(vault, row) as current:
        merged = dict(current)
    for name, value in secrets.items():
        if name not in ACCOUNT_SECRET_COLUMNS:
            continue
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value.strip()
    parsed = ParsedAccount(row.bookmaker_id, 0, "edit", label=row.label, **{k: merged.get(k) for k in ACCOUNT_SECRET_COLUMNS})
    identity = parsed.identity
    if identity is None:
        raise ValueError("the account would have no login, key or token left")
    digest = identity_digests(vault, row.bookmaker_id, identity)
    clash = (await session.execute(
        select(VaultBookmakerAccount.id).where(VaultBookmakerAccount.bookmaker_id == row.bookmaker_id, VaultBookmakerAccount.identity_digest.in_(digest), VaultBookmakerAccount.id != row.id)
    )).first()
    if clash is not None:
        raise VaultConflictError("another account of this bookmaker already has that login")
    for name, column in ACCOUNT_SECRET_COLUMNS.items():
        value = merged.get(name)
        setattr(row, column, vault.encrypt_key(value, context=account_context(row.id, name)) if value else None)
    row.username_hint = mask_identity(merged["username"]) if merged.get("username") else None
    if "url" in secrets:
        from urllib.parse import urlsplit  # noqa: PLC0415

        row.target_host = (urlsplit(merged["url"]).hostname or None) if merged.get("url") else None
    row.identity_digest = digest[0]
    row.secrets_fingerprint = account_fingerprint(vault, parsed)
    if any(k in secrets for k in ("password", "api_key", "token", "totp_seed", "username")):
        row.verification_status, row.verification_detail = "UNVERIFIED", "credentials edited"
    merged.clear()
    return row


# ------------------------------------------------------------------------------------------ rotation
async def rotate_all(session: AsyncSession, vault: VaultCrypto) -> dict[str, int]:
    """Re-seal every Vault secret (and Fleet Command's source keys) under the current MASTER_VAULT_KEY and
    re-index the logins. Run after promoting a new key, with the old one still in MASTER_VAULT_PREVIOUS_KEYS."""
    counts = {"accounts": 0, "providers": 0, "fleet_sources": 0}
    for row in (await session.execute(select(VaultBookmakerAccount))).scalars():
        with account_secrets(vault, row) as secrets:
            for name, column in ACCOUNT_SECRET_COLUMNS.items():
                if secrets.get(name):
                    setattr(row, column, vault.encrypt_key(secrets[name], context=account_context(row.id, name)))
            parsed = ParsedAccount(row.bookmaker_id, 0, "rotate", **{k: secrets.get(k) for k in ACCOUNT_SECRET_COLUMNS})
            if parsed.identity:
                row.identity_digest = identity_digests(vault, row.bookmaker_id, parsed.identity)[0]
        counts["accounts"] += 1
    for row in (await session.execute(select(VaultProviderCredential))).scalars():
        key = vault.decrypt_key(row.encrypted_api_key, context=provider_context(row.id, "api_key"))
        row.encrypted_api_key = vault.encrypt_key(key, context=provider_context(row.id, "api_key"))
        row.key_digest = vault.blind_index(f"provider|{row.provider_id}|{key}")
        if row.encrypted_secret:
            row.encrypted_secret = vault.rotate(row.encrypted_secret, context=provider_context(row.id, "secret"))
        counts["providers"] += 1
    for source in (await session.execute(select(OmniFleetSource).where(OmniFleetSource.encrypted_api_key.is_not(None)))).scalars():
        if source.encrypted_api_key and not vault.is_current(source.encrypted_api_key):
            source.encrypted_api_key = vault.rotate(source.encrypted_api_key)
            counts["fleet_sources"] += 1
    return counts


# ------------------------------------------------------------------------------------------ backup
def _backup_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, **_SCRYPT).derive(passphrase.encode("utf-8"))


async def export_backup(session: AsyncSession, vault: VaultCrypto, passphrase: str, settings: Settings) -> dict[str, Any]:
    if len(passphrase) < settings.VAULT_BACKUP_MIN_PASSPHRASE:
        raise BackupError(f"the passphrase needs at least {settings.VAULT_BACKUP_MIN_PASSPHRASE} characters")
    from app.services.vault.fleet_config import get_config  # noqa: PLC0415

    accounts: list[dict[str, Any]] = []
    for row in (await session.execute(select(VaultBookmakerAccount).order_by(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.priority))).scalars():
        with account_secrets(vault, row) as secrets:
            accounts.append({
                "bookmaker_id": row.bookmaker_id, "label": row.label, **dict(secrets), "currency": row.currency, "is_active": bool(row.is_active),
                "priority": int(row.priority or 100), "balance": _str(row.balance), "stake_cap": _str(row.stake_cap),
            })
    providers = []
    for row in (await session.execute(select(VaultProviderCredential).order_by(VaultProviderCredential.provider_id))).scalars():
        providers.append({
            "provider_id": row.provider_id, "label": row.label, "api_key": vault.decrypt_key(row.encrypted_api_key, context=provider_context(row.id, "api_key")),
            "secret": vault.decrypt_key(row.encrypted_secret, context=provider_context(row.id, "secret")) if row.encrypted_secret else None,
            "base_url": row.base_url, "is_active": bool(row.is_active), "linked_source_id": row.linked_source_id,
        })
    config = await get_config(session)
    fleet = {"sports": list(config.sports or []), "markets_by_sport": dict(config.markets_by_sport or {}), "quiet_start": config.quiet_start,
             "quiet_end": config.quiet_end, "timezone": config.timezone, "account_routing": bool(config.account_routing)}
    counts = {"accounts": len(accounts), "providers": len(providers), "sports": len(fleet["sports"])}
    plain = json.dumps({"accounts": accounts, "providers": providers, "fleet": fleet}, separators=(",", ":")).encode("utf-8")
    accounts.clear()
    providers.clear()
    salt, nonce = os.urandom(16), os.urandom(12)
    sealed = AESGCM(_backup_key(passphrase, salt)).encrypt(nonce, plain, _BACKUP_AAD)
    plain = b""
    return {
        "format": BACKUP_FORMAT, "version": 1, "created_at": datetime.now(UTC).isoformat(),
        "kdf": {"name": "scrypt", **_SCRYPT, "salt": base64.b64encode(salt).decode()}, "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(sealed).decode(), "counts": counts,
    }


def open_backup(payload: dict[str, Any], passphrase: str) -> dict[str, Any]:
    try:
        if payload.get("format") != BACKUP_FORMAT or int(payload.get("version", 0)) != 1:
            raise BackupError("not a BetDoc vault backup (format / version)")
        kdf = payload["kdf"]
        if kdf.get("name") != "scrypt" or any(int(kdf.get(k, -1)) != v for k, v in _SCRYPT.items()):
            raise BackupError("unsupported key derivation in the backup")
        key = _backup_key(passphrase, base64.b64decode(kdf["salt"]))
        plain = AESGCM(key).decrypt(base64.b64decode(payload["nonce"]), base64.b64decode(payload["ciphertext"]), _BACKUP_AAD)
    except InvalidTag as exc:
        raise BackupError("the passphrase does not open this backup (or it was altered)") from exc
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("the backup is malformed") from exc
    return json.loads(plain)


async def restore_backup(session: AsyncSession, vault: VaultCrypto, payload: dict[str, Any], passphrase: str, settings: Settings, actor_id: uuid.UUID | None) -> dict[str, Any]:
    """Upsert a backup into this vault (idempotent: restoring twice changes nothing); the caller commits."""
    from app.services.vault.fleet_config import get_config  # noqa: PLC0415

    data = open_backup(payload, passphrase)
    parsed = ParseResult()
    for a in data.get("accounts", []):
        parsed.accounts.append(ParsedAccount(
            a["bookmaker_id"], 0, "backup", label=a.get("label"), **{k: a.get(k) for k in ACCOUNT_SECRET_COLUMNS}, currency=a.get("currency"),
            balance=Decimal(a["balance"]) if a.get("balance") else None, stake_cap=Decimal(a["stake_cap"]) if a.get("stake_cap") else None,
        ))
    for p in data.get("providers", []):
        parsed.providers.append(ParsedProvider(p["provider_id"], 0, "backup", label=p.get("label"), api_key=p.get("api_key"), secret=p.get("secret"), url=p.get("base_url")))
    report = await import_parsed(session, vault, parsed, settings, origin="backup", content_sha256="backup", actor_id=actor_id)
    await session.flush()
    for a, account in zip(data.get("accounts", []), parsed.accounts, strict=True):
        row = await _account_by_identity(session, vault, account.bookmaker_id, account.identity or "")
        if row is not None:
            row.is_active, row.priority = bool(a.get("is_active", True)), int(a.get("priority") or row.priority)
    fleet = data.get("fleet") or {}
    config = await get_config(session)
    for name in ("sports", "markets_by_sport", "quiet_start", "quiet_end", "timezone", "account_routing"):
        if name in fleet:
            setattr(config, name, fleet[name])
    data.clear()
    return report.as_dict()


__all__ = [
    "BackupError", "VaultConflictError", "VaultDecryptionError", "account_secrets", "account_view", "create_account", "export_backup",
    "list_accounts", "list_providers", "open_backup", "provider_view", "restore_backup", "rotate_all", "update_account", "venue_for_account",
]
