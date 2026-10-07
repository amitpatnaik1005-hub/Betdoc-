"""DataResearchAgent: research reports computed from BetDoc's own market data.

Given a topic ("Arsenal", "Arsenal v Chelsea", "EPL draw pricing"), it finds the fixtures whose teams
the topic names and reports what the stored odds actually show: consensus drift from first to latest
snapshot, best current price per side, bookmaker dispersion, and the desk's own ledger results on
those fixtures. Nothing is invented; with no matching data the report says so.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BetLedger
from app.models.odds import OddsSnapshot

MAX_TEAMS_SCANNED = 5_000
MAX_FIXTURES_REPORTED = 6
SETTLED = ("WON", "LOST", "HALF_WON", "HALF_LOST", "CASH_OUT", "VOID")


def _clean(text: str) -> str:
    return " ".join(text.split()).replace("|", "/")  # no Markdown table/structure injection


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


class DataResearchAgent:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def generate_report(self, topic: str) -> str:
        safe_topic = _clean(topic)
        async with self._session_factory() as db:
            teams = await self._teams_in_topic(db, safe_topic)
            snapshots = await self._snapshots(db, teams, safe_topic) if teams or self._keywords(safe_topic) else []
            generated_at = datetime.now(UTC).isoformat(timespec="seconds")
            lines = [f"# Research Report: {safe_topic}", "", f"_Computed by DataResearchAgent from stored odds and the ledger at {generated_at}_", ""]
            if not snapshots:
                lines += await self._no_data_section(db, safe_topic)
                return "\n".join(lines)
            lines += self._fixture_sections(snapshots)
            lines += await self._ledger_section(db, sorted({s.match_id for s in snapshots}))
        return "\n".join(lines)

    # ------------------------------------------------------------------ matching
    @staticmethod
    def _keywords(topic: str) -> list[str]:
        return [w for w in re.findall(r"[A-Za-z][A-Za-z.'-]{3,}", topic)][:6]

    async def _teams_in_topic(self, db: AsyncSession, topic: str) -> list[str]:
        rows = await db.execute(select(OddsSnapshot.home_team).distinct().limit(MAX_TEAMS_SCANNED))
        away = await db.execute(select(OddsSnapshot.away_team).distinct().limit(MAX_TEAMS_SCANNED))
        names = {r[0] for r in rows} | {r[0] for r in away}
        lowered = topic.lower()
        return sorted((n for n in names if n and n.lower() in lowered), key=len, reverse=True)

    async def _snapshots(self, db: AsyncSession, teams: list[str], topic: str) -> list[OddsSnapshot]:
        if teams:
            cond = or_(*(or_(OddsSnapshot.home_team == t, OddsSnapshot.away_team == t) for t in teams))
        else:
            cond = or_(*(or_(OddsSnapshot.home_team.ilike(f"%{w}%"), OddsSnapshot.away_team.ilike(f"%{w}%")) for w in self._keywords(topic)))
        stmt = select(OddsSnapshot).where(cond, OddsSnapshot.market_type == "h2h").order_by(OddsSnapshot.timestamp)
        return list((await db.execute(stmt.limit(20_000))).scalars().all())

    # ------------------------------------------------------------------ sections
    def _fixture_sections(self, snapshots: list[OddsSnapshot]) -> list[str]:
        by_match: dict[str, list[OddsSnapshot]] = defaultdict(list)
        for s in snapshots:
            by_match[s.match_id].append(s)
        fixtures = sorted(by_match.values(), key=lambda rows: rows[-1].commence_time, reverse=True)[:MAX_FIXTURES_REPORTED]
        books = {s.bookmaker for s in snapshots}
        lines = [
            "## Executive Summary",
            f"{len(by_match)} fixture(s), {len(books)} bookmaker(s) and {len(snapshots)} head-to-head price points match this topic.",
            "",
        ]
        for rows in fixtures:
            first, last = rows[0], rows[-1]
            lines += [f"## {_clean(last.home_team)} v {_clean(last.away_team)}", f"Kick-off {last.commence_time:%d %b %Y %H:%M} UTC · {len({r.bookmaker for r in rows})} books · {len(rows)} quotes", ""]
            lines += ["| Selection | Opening consensus | Latest consensus | Drift | Best price (book) | Book spread |", "| --- | --- | --- | --- | --- | --- |"]
            opening = self._consensus([r for r in rows if r.timestamp == first.timestamp])
            latest_ts = last.timestamp
            latest_rows = [r for r in rows if r.timestamp == latest_ts]
            latest = self._consensus(latest_rows)
            for sel in sorted(latest):
                prices = [r for r in latest_rows if r.selection == sel]
                best = max(prices, key=lambda r: r.odds)
                spread = max(p.odds for p in prices) - min(p.odds for p in prices)
                drift = latest[sel] - opening.get(sel, latest[sel])
                arrow = "▲" if drift > 0.005 else "▼" if drift < -0.005 else "·"
                lines.append(
                    f"| {_clean(sel)} | {_pct(opening.get(sel, latest[sel]))} | {_pct(latest[sel])} | {arrow} {drift * 100:+.1f} pts | "
                    f"{best.odds:.2f} ({_clean(best.bookmaker)}) | {spread:.2f} |"
                )
            lines.append("")
        lines += [
            "## Reading the table",
            "- Consensus is the vig-free average of every bookmaker's implied probability at that timestamp.",
            "- Drift above +1 pt into kick-off usually means money (or news) backing that side; check the Wire.",
            "- A wide book spread is where line-shopping edge lives: the Oracle's value list prices against it.",
            "",
        ]
        return lines

    @staticmethod
    def _consensus(rows: list[OddsSnapshot]) -> dict[str, float]:
        by_book: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            if r.odds > 1:
                by_book[r.bookmaker][r.selection] = 1 / r.odds
        sums: dict[str, float] = defaultdict(float)
        books = 0
        for implied in by_book.values():
            total = sum(implied.values())
            if total <= 0:
                continue
            books += 1
            for sel, p in implied.items():
                sums[sel] += p / total
        return {sel: v / books for sel, v in sums.items()} if books else {}

    async def _ledger_section(self, db: AsyncSession, match_ids: list[str]) -> list[str]:
        stmt = select(BetLedger.status, func.count(), func.sum(BetLedger.stake), func.sum(BetLedger.payout)).where(
            BetLedger.match_id.in_(match_ids)
        ).group_by(BetLedger.status)
        rows = (await db.execute(stmt)).all()
        if not rows:
            return ["## Desk exposure", "No orders have been placed on these fixtures.", ""]
        lines = ["## Desk exposure", "| Status | Orders | Staked | Returned |", "| --- | --- | --- | --- |"]
        for status, count, staked, paid in rows:
            lines.append(f"| {status} | {count} | {float(staked or 0):,.2f} | {float(paid or 0):,.2f} |")
        return lines + [""]

    async def _no_data_section(self, db: AsyncSession, topic: str) -> list[str]:
        total = int(await db.scalar(select(func.count()).select_from(OddsSnapshot)) or 0)
        fixtures = int(await db.scalar(select(func.count(func.distinct(OddsSnapshot.match_id)))) or 0)
        span = (await db.execute(select(func.min(OddsSnapshot.timestamp), func.max(OddsSnapshot.timestamp)))).one()
        lines = ["## No matching market data", f'No stored fixture names a team mentioned in "{topic}".', ""]
        if total:
            lines.append(f"The odds store holds {total:,} price points across {fixtures:,} fixtures ({span[0]:%d %b %Y} to {span[1]:%d %b %Y}). Name a team exactly as it is listed there.")
        else:
            lines.append("The odds store is empty: configure ODDS_API_KEY and ODDS_SPORT_KEYS so the poller can collect prices.")
        return lines + [""]
