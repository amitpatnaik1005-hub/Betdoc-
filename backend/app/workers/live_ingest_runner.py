"""The live sports feeds from a terminal (Group 112). Developed for Amit Ashok Kumar Patnaik.

    python -m app.workers.live_ingest_runner --status --probe         what stands between the desk and live prices (free probe)
    python -m app.workers.live_ingest_runner --sports cricket,football buy what the cadence says is due, then the context
    python -m app.workers.live_ingest_runner --force                   buy every requested sport now (the reserve, floor and
                                                                       breaker still apply)
    python -m app.workers.live_ingest_runner --hydrate-only            Redis rebuilt from the stored boards, no provider call
    python -m app.workers.live_ingest_runner --context-only            Football-Data.org and Sportmonks context only

Odds are bought through the ingestion fleet's Odds API source (Fleet Command's switch and key, the breaker, the quota
reserve and floor, the cache shield), never around it. ``--json`` prints the full report. No key is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

from app.core.config import get_settings
from app.services import live_sports_ingestion_service as live

logger = logging.getLogger("betdoc.live_ingest_runner")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.workers.live_ingest_runner", description="BetDoc live sports feeds (Group 112)")
    parser.add_argument("--sports", default="cricket,football", help="comma-separated: cricket, football, or Odds API sport keys")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hydrate-only", action="store_true", help="rebuild Redis from the stored boards; no provider call")
    mode.add_argument("--context-only", action="store_true", help="refresh the Football-Data.org / Sportmonks context only")
    mode.add_argument("--status", action="store_true", help="report the source, keys, coverage, store and cadence")
    parser.add_argument("--force", action="store_true", help="buy every requested sport now instead of only the due ones")
    parser.add_argument("--no-context", action="store_true", help="skip the context refresh after buying")
    parser.add_argument("--probe", action="store_true", help="with --status: probe each Odds API key (GET /sports is not metered)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser.parse_args(argv)


def _line(label: str, value: Any) -> str:
    return f"  {label:<22} {value}"


def render(kind: str, report: dict[str, Any]) -> str:
    """A short human summary of a report (the JSON has everything)."""
    out = [f"BetDoc live sports feeds - {kind}"]
    if kind == "status":
        src = report["source"]
        out += [_line("Odds API source", src["state"]), _line("key in use", src["key_in_use"] or "none"),
                _line("quota (fleet's reading)", f"{src['quota_remaining']} left, floor {src['quota_floor']}")]
        for origin, probe in report["keys"].items():
            days = report["cadence"]["days_by_key"].get(origin)
            out.append(_line(f"key {origin}", f"accepted, {probe.get('remaining'):g} credits left" + (f", ~{days:g} days at this pace" if days is not None else "")
                             if probe.get("ok") else f"rejected ({probe.get('http_status') or probe.get('error')})"))
        cov = report["coverage"]
        out.append(_line("in season", ", ".join(cov["in_season"]) if cov["in_season"] is not None else "unknown (run with --probe)"))
        if cov["out_of_season"]:
            out.append(_line("out of season", ", ".join(cov["out_of_season"])))
        cad = report["cadence"]
        out.append(_line("cadence spend", f"~{cad['credits_per_day_total']:g} credits/day at today's fixtures" + (f", {cad['days_of_quota']:g} days of quota" if cad["days_of_quota"] is not None else "")))
        out.append(_line("stored fixtures", report["store"]["fixtures"]))
        out += [f"  ! {b}" for b in report["blockers"]] or ["  nothing blocks the fleet from buying"]
    elif kind == "hydrate":
        out += [_line("fixtures stored", report["fixtures_stored"]), _line("replayed into Redis", report["fixtures_hydrated"]),
                _line("too old to replay", f"{report['skipped_stale']} (older than their cadence tier allows; shown as stored prices)"),
                _line("ticks / quotes", f"{report['ticks']} / {report['quotes']}"), _line("elapsed", f"{report['elapsed_ms']} ms")]
    elif kind == "context":
        for provider, entry in (report.get("providers") or {}).items():
            out.append(_line(provider, entry.get("error") or f"{entry['fixtures']} fixtures, {entry['paired']} paired"))
    else:
        odds = report["odds_api"]
        out += [_line("status", report["status"]), _line("Odds API", f"{odds.get('outcome')}" + (f": {odds['reason']}" if odds.get("reason") else "")),
                _line("scope", ", ".join(odds["scope"]) or "—")]
        if odds.get("not_covered"):
            out.append(_line("not covered", ", ".join(odds["not_covered"])))
        if report.get("hydration"):
            out.append(_line("replayed into Redis", report["hydration"]["fixtures_hydrated"]))
        for provider, entry in ((report.get("context") or {}).get("providers") or {}).items():
            out.append(_line(provider, entry.get("error") or f"{entry['fixtures']} fixtures, {entry['paired']} paired"))
        out.append(_line("stored fixtures", report["store"]["fixtures"]))
    out.append("  Developer credit: Amit Ashok Kumar Patnaik")
    return "\n".join(out)


async def run(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    sports = [s for s in args.sports.split(",") if s.strip()]
    async with live.standalone_fleet_deps(settings) as deps:
        if args.status:
            return "status", await live.status(deps, probe=args.probe)
        if args.hydrate_only:
            scope, _ = live.resolve_scope(sports, settings)
            return "hydrate", await live.hydrate(deps, sports=scope or None)
        if args.context_only:
            return "context", await live.refresh_context(deps)
        return "ingest", await live.ingest(deps, sports=sports, force=args.force, with_context=not args.no_context)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    kind, report = asyncio.run(run(args))
    print(json.dumps(report, indent=2, default=str) if args.json else render(kind, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
