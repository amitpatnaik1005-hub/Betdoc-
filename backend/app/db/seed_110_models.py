"""Seed the model registry: every math model, risk model and bet type into ``SmallcaseRegistry``.

    python -m app.db.seed_110_models                       # the code's catalogue (and the blueprint documents, if found)
    python -m app.db.seed_110_models --docs a.md b.md      # documents at other paths
    python -m app.db.seed_110_models --strict              # fail unless the blueprint's 59 / 24 / 27 are all accounted for
    python -m app.db.seed_110_models --dry-run             # report only

Idempotent: components are upserted by key. The documents (``betdoc_deep_dive.md``,
``betdoc_master_blueprint.md``, looked for in the repository root by default) are reconciled with
the catalogue: a documented component no module implements is seeded too, marked not live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.database import AsyncSessionLocal, engine
from app.models.the_core import ComponentKind
from app.services.hive_registry import CATALOGUE, EXPECTED, documented_components, parse_blueprint, reconcile, seed_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCS = ("betdoc_deep_dive.md", "betdoc_master_blueprint.md")


def _documented(paths: list[Path]) -> dict[ComponentKind, list[str]]:
    merged: dict[ComponentKind, list[str]] = {kind: [] for kind in EXPECTED}
    for path in paths:
        for kind, names in parse_blueprint(path.read_text(encoding="utf-8")).items():
            merged[kind] += [n for n in names if n not in merged[kind]]
    return merged


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", nargs="*", type=Path, default=None)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    candidates = args.docs if args.docs is not None else [REPO_ROOT / name for name in DEFAULT_DOCS]
    found = [p for p in candidates if p.is_file()]
    missing = [str(p) for p in candidates if not p.is_file()]
    components = list(CATALOGUE)
    report: dict[str, object] = {"documents_read": [str(p) for p in found], "documents_missing": missing}
    if found:
        rec = reconcile(_documented(found))
        components += documented_components(rec)
        report["reconciliation"] = rec.counts()
        report["documented_only"] = {k.value: v for k, v in rec.documented_only.items()}

    counts = {kind.value: sum(1 for c in components if c.kind is kind) for kind in EXPECTED}
    report["catalogue"] = counts
    report["expected"] = {k.value: v for k, v in EXPECTED.items()}
    short = {k.value: v - counts[k.value] for k, v in EXPECTED.items() if counts[k.value] < v}
    report["short_of_blueprint"] = short

    if not args.dry_run:
        async with AsyncSessionLocal() as session:
            seeded = await seed_registry(session, components)
            await session.commit()
        report["seeded"] = seeded.as_dict()
        await engine.dispose()
    print(json.dumps(report, indent=2))
    if args.strict and (missing or short):
        print("strict: the blueprint documents are missing or not every component is accounted for", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
