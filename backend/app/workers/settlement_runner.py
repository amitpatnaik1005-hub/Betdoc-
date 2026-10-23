"""Settlement from a terminal (Group 113). Developed for Amit Ashok Kumar Patnaik.

    python -m app.workers.settlement_runner --sweep --dry-run           what would settle now (results fetched, nothing written)
    python -m app.workers.settlement_runner --sweep                     record the feeds' results, settle every ledger
    python -m app.workers.settlement_runner --sweep --no-fetch          settle from the scores already recorded
    python -m app.workers.settlement_runner --match-id ID --home-score 2 --away-score 1
                                                                       an admin's final score for one fixture id, then a sweep
    python -m app.workers.settlement_runner --match-id ID --status ABANDONED

A manual result is an explicit administrative command (it replaces any score a feed gave for that fixture id; bets
already settled on the earlier score are not regraded here). ``--json`` prints the full report. No key is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.user_bets_ledger import ScoreStatus
from app.services import live_result_settlement_service as settlement
from app.services.live_sports_ingestion_service import standalone_fleet_deps


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.workers.settlement_runner", description="BetDoc automated result settlement (Group 113)")
    parser.add_argument("--sweep", action="store_true", help="record the feeds' results and settle every ledger")
    parser.add_argument("--dry-run", action="store_true", help="compute everything, write nothing")
    parser.add_argument("--no-fetch", action="store_true", help="settle from the scores already recorded (no feed call)")
    parser.add_argument("--match-id", help="record an admin result for this fixture id (as the ledger names it), then sweep")
    parser.add_argument("--home-score", type=int, help="home goals (runs, points) after 90 minutes")
    parser.add_argument("--away-score", type=int, help="away goals (runs, points) after 90 minutes")
    parser.add_argument("--status", choices=[s.value for s in ScoreStatus], default=ScoreStatus.FINAL.value)
    parser.add_argument("--home", help="the home side's name, when no ledger names this fixture")
    parser.add_argument("--away", help="the away side's name, when no ledger names this fixture")
    parser.add_argument("--sport", help="the Odds API sport key, when no ledger names this fixture")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    if not args.sweep and not args.match_id:
        parser.error("give --sweep, or --match-id with a result")
    if args.match_id and args.status == ScoreStatus.FINAL.value and (args.home_score is None or args.away_score is None):
        parser.error("a FINAL result needs --home-score and --away-score")
    if args.match_id and args.dry_run:
        parser.error("an admin result is written; preview what the feeds would settle with --sweep --dry-run")
    return args


def _line(label: str, value: Any) -> str:
    return f"  {label:<24} {value}"


def render(report: dict[str, Any], manual: dict[str, Any] | None) -> str:
    out = ["BetDoc settlement" + (" - DRY RUN (nothing written)" if report.get("dry_run") else "")]
    if manual is not None:
        replaced = manual.get("replaced")
        score = f"{manual['home_goals']}-{manual['away_goals']}" if manual["home_goals"] is not None else manual["status"]
        out.append(_line("admin result", f"{manual['home']} v {manual['away']}: {score}" + (f" (replaced {replaced['source']} {replaced['status']})" if replaced else "")))
    out.append(_line("fixtures waiting", report["waiting_fixtures"]))
    for provider, entry in (report.get("feeds") or {}).items():
        if provider != "finished_matches":
            out.append(_line(provider, entry.get("error") or f"{entry.get('calls', 0)} call(s)"))
    results = report["results"]
    out.append(_line("results recorded", f"{len(results['recorded'])} (unpaired: {results['unpaired']})"))
    for r in results["recorded"]:
        out.append(f"    {r['fixture_id'][:36]:<36} {r['status']:<10} {'' if r['score'] is None else '%d-%d' % tuple(r['score'])}  ({r['provider']})")
    oracle = report["oracle"]
    out.append(_line("Oracle bets", f"{oracle['bets_settled']} settled ({oracle['legs_settled']} legs)" if oracle["bets_settled"] is not None
                     else f"{oracle['legs_settled']} legs would settle"))
    for label, key in (("real-money tickets", "real_money_tickets"), ("exchange orders", "exchange_orders")):
        led = report[key]
        out.append(_line(label, f"{led['settled']} settled (won {led['won']}, lost {led['lost']}, void {led['void']}); gross INR {led['gross_payout']}, "
                                f"TDS INR {led['tds_withheld']}, after TDS INR {led['net_post_tax']}"))
        for row in led["rows"]:
            out.append(f"    {json.dumps(row)}")
    out.append(_line("30% TDS withheld", f"INR {report['tds_withheld']} (Section 194BA)"))
    out.append("  Developer credit: Amit Ashok Kumar Patnaik")
    return "\n".join(out)


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    settings = get_settings()
    async with standalone_fleet_deps(settings) as deps:
        manual = None
        if args.match_id:
            manual = await settlement.record_admin_result(deps.session_factory, args.match_id, status=ScoreStatus(args.status), home_goals=args.home_score,
                                                          away_goals=args.away_score, home=args.home, away=args.away, sport_key=args.sport)
        async with httpx.AsyncClient(timeout=settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS) as http:
            report = await settlement.sweep(deps.session_factory, http, deps.redis, settings, fetch=not args.no_fetch, dry_run=args.dry_run)
        return report, manual


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    try:
        report, manual = asyncio.run(run(args))
    except settlement.SettlementError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"manual": manual, **report}, indent=2, default=str) if args.json else render(report, manual))
    return 0


if __name__ == "__main__":
    sys.exit(main())
