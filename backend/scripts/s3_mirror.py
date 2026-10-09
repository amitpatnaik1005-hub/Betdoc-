#!/usr/bin/env python3
"""Mirror Nalanda's cold tier (``<NALANDA_ARCHIVE_DIR>/**/*.parquet`` and the chain anchors) to S3.

    python scripts/s3_mirror.py              # dry run: what would be uploaded, and to where
    python scripts/s3_mirror.py --upload     # upload (needs NALANDA_S3_BUCKET and boto3 + AWS credentials)
    python scripts/s3_mirror.py --verify     # also re-hash every local file against its manifest row

A placeholder for the off-site copy: it uploads only when a bucket is configured and ``--upload`` is
given, and it never deletes anything, here or in the bucket. The nightly cold export
(``nalanda.compress_historical_ticks``) already mirrors each new file when a bucket is set, and with
``NALANDA_REQUIRE_MIRROR_BEFORE_DROP`` drops a partition from PostgreSQL only once its file is
mirrored; this script catches up anything exported before the bucket existed. Each object carries its
SHA-256 as metadata; the manifest row (``nalanda_cold_exports``) is marked mirrored.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models.nalanda_lake import ColdExport  # noqa: E402
from app.services.nalanda_tiering import S3Mirror, archive_root  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def main(upload: bool, verify: bool) -> int:
    from app.core.database import AsyncSessionLocal  # noqa: PLC0415

    settings = get_settings()
    root = archive_root(settings)
    mirror = S3Mirror(settings)
    if upload and not mirror.available:
        print("No mirror: set NALANDA_S3_BUCKET and install boto3 (with AWS credentials) to upload.", file=sys.stderr)
        return 2
    async with AsyncSessionLocal() as session:
        manifest = {Path(r.file_path).resolve(): r for r in (await session.execute(select(ColdExport))).scalars()}
        files = sorted([*root.rglob("*.parquet"), *(root / "anchors").glob("*.jsonl")]) if root.exists() else []
        problems = 0
        for path in files:
            row = manifest.get(path.resolve())
            if verify and row is not None and _sha256(path) != row.sha256:
                print(f"MISMATCH {path}: the file no longer matches its manifest SHA-256")
                problems += 1
                continue
            if row is not None and row.mirrored_at is not None:
                continue
            target = f"s3://{settings.NALANDA_S3_BUCKET or '<NALANDA_S3_BUCKET>'}/{mirror.key_for(path, root) if path.is_relative_to(root) else path.name}"
            if not upload:
                print(f"would upload {path} -> {target}")
                continue
            uploaded = mirror.upload(path, root, row.sha256 if row is not None else _sha256(path))
            print(f"uploaded {path} -> {uploaded}")
            if row is not None:
                row.mirror_target, row.mirrored_at = uploaded, datetime.now(UTC)
        await session.commit()
    print(f"{len(files)} file(s) under {root}; {problems} mismatch(es)")
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upload", action="store_true", help="upload (default: dry run)")
    parser.add_argument("--verify", action="store_true", help="re-hash each file against its manifest row")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.upload, args.verify)))
