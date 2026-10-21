"""Group 87: the encrypted backup vault's cryptography and retention rule (SHIVAJI & TODAR MAL).
Developer Credit: Amit Ashok Kumar Patnaik

* A snapshot is the database's rows as canonical JSON (sorted keys), gzip-compressed (``mtime=0``: the same rows give
  the same bytes) and sealed with Fernet (AES-128-CBC with an HMAC-SHA256 tag). Two digests are kept: of the
  plaintext JSON and of the ciphertext file.
* Verification runs outside-in: the file's SHA-256, then the Fernet token's HMAC (wrong key or altered token), then
  the plaintext SHA-256, then the envelope (its schema version and declared counts against the rows it holds).
* Manifests chain: each manifest's hash covers the previous one's, its file, both digests, its counts, the bankroll it
  protected and its timestamp.
* Retention keeps the newest ``retention_count`` live snapshots; a pinned snapshot is never pruned, whatever its age.
* The key is never stored: only a fingerprint (the first 16 hex of its SHA-256) that names which key sealed a snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

CENT = Decimal("0.01")
GENESIS_BACKUP_HASH: str = "0" * 64
SCHEMA_VERSION = "betdoc.backup.v1"


@dataclass(frozen=True, slots=True)
class EncryptedSnapshotBlob:
    ciphertext_bytes: bytes
    file_bytes: int  # the sealed file's size
    uncompressed_bytes: int  # the JSON's size
    plaintext_sha256: str
    ciphertext_sha256: str
    total_tables: int
    total_rows: int


@dataclass(frozen=True, slots=True)
class SnapshotVerificationResult:
    valid: bool
    verdict: str  # PASS | FAIL
    tables_checked: int
    rows_verified: int
    plaintext_sha256: str | None
    ciphertext_sha256: str
    detail: str
    payload: dict[str, Any] | None


def _fernet(key: str | bytes) -> Fernet:
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def key_fingerprint(key: str | bytes) -> str:
    raw = key.encode("utf-8") if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()[:16]


def convert_inr_to_usd(amount_inr: Decimal | None, usd_inr_rate: Decimal | None) -> Decimal | None:
    """INR to USD at TODAR MAL's measured rate (INR per USD); None when either is unmeasured."""
    if amount_inr is None or usd_inr_rate is None or usd_inr_rate <= 0:
        return None
    return (Decimal(str(amount_inr)) / Decimal(str(usd_inr_rate))).quantize(CENT, rounding=ROUND_HALF_UP)


def compute_backup_manifest_hash(*, prev_hash: str, filename: str, plaintext_sha256: str, ciphertext_sha256: str, total_tables: int, total_rows: int,
                                 protected_bankroll_inr: Decimal | None, timestamp_iso: str) -> str:
    cap = "NONE" if protected_bankroll_inr is None else f"{Decimal(str(protected_bankroll_inr)):.2f}"
    canonical = "|".join([prev_hash.strip() or GENESIS_BACKUP_HASH, filename.strip(), plaintext_sha256.strip().lower(), ciphertext_sha256.strip().lower(),
                          str(int(total_tables)), str(int(total_rows)), cap, timestamp_iso.strip()])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_encrypted_snapshot_blob(*, tables_payload: dict[str, list[dict[str, Any]]], fernet_key: str | bytes, metadata: dict[str, Any] | None = None) -> EncryptedSnapshotBlob:
    total_tables = len(tables_payload)
    total_rows = sum(len(rows) for rows in tables_payload.values())
    envelope = {"schema_version": SCHEMA_VERSION, "metadata": metadata or {}, "total_tables": total_tables, "total_rows": total_rows, "tables": tables_payload}
    plaintext = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = _fernet(fernet_key).encrypt(gzip.compress(plaintext, compresslevel=6, mtime=0))
    return EncryptedSnapshotBlob(ciphertext, len(ciphertext), len(plaintext), hashlib.sha256(plaintext).hexdigest(), hashlib.sha256(ciphertext).hexdigest(),
                                 total_tables, total_rows)


def _fail(detail: str, cipher_sha: str, plain_sha: str | None = None) -> SnapshotVerificationResult:
    return SnapshotVerificationResult(False, "FAIL", 0, 0, plain_sha, cipher_sha, detail, None)


def verify_and_decrypt_snapshot_blob(*, ciphertext_bytes: bytes, fernet_key: str | bytes, expected_plaintext_sha256: str,
                                     expected_ciphertext_sha256: str) -> SnapshotVerificationResult:
    cipher_sha = hashlib.sha256(ciphertext_bytes).hexdigest()
    if cipher_sha != expected_ciphertext_sha256.strip().lower():
        return _fail("Ciphertext SHA-256 mismatch: the archive file was modified on disk.", cipher_sha)
    try:
        compressed = _fernet(fernet_key).decrypt(ciphertext_bytes)
    except (InvalidToken, ValueError, TypeError):
        return _fail("Fernet HMAC-SHA256 authentication failed: the key differs from the one that sealed it, or the token is corrupt.", cipher_sha)
    try:
        plaintext = gzip.decompress(compressed)
    except (OSError, EOFError) as exc:
        return _fail(f"Decompression failed: {exc}", cipher_sha)
    plain_sha = hashlib.sha256(plaintext).hexdigest()
    if plain_sha != expected_plaintext_sha256.strip().lower():
        return _fail("Plaintext SHA-256 mismatch after decompression.", cipher_sha, plain_sha)
    try:
        envelope = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _fail(f"The snapshot is not valid JSON: {exc}", cipher_sha, plain_sha)
    tables = envelope.get("tables") if isinstance(envelope, dict) else None
    if envelope.get("schema_version") != SCHEMA_VERSION or not isinstance(tables, dict) or not all(isinstance(r, list) for r in tables.values()):
        return _fail("The snapshot's envelope is not a betdoc.backup.v1 table map.", cipher_sha, plain_sha)
    rows = sum(len(r) for r in tables.values())
    if envelope.get("total_tables") != len(tables) or envelope.get("total_rows") != rows:
        return _fail(f"Declared counts ({envelope.get('total_tables')} tables, {envelope.get('total_rows')} rows) differ from the content ({len(tables)}, {rows}).",
                     cipher_sha, plain_sha)
    return SnapshotVerificationResult(True, "PASS", len(tables), rows, plain_sha, cipher_sha,
                                      f"Integrity verified: file SHA-256, Fernet HMAC and plaintext SHA-256 match across {len(tables)} tables ({rows} rows).", envelope)


def select_snapshots_for_retention_pruning(snapshots: Sequence[tuple[str, bool, str]], retention_count: int) -> list[str]:
    """``(id, is_pinned, status)`` newest first: the live, unpinned snapshots past the newest ``retention_count`` live ones."""
    keep = max(1, int(retention_count))
    live = 0
    out = []
    for snap_id, pinned, status in snapshots:
        if status == "PRUNED":
            continue
        live += 1
        if live > keep and not pinned:
            out.append(snap_id)
    return out
