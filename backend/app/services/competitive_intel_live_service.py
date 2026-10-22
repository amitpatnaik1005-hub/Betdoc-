"""Group 85: live competitive market intelligence (Commanders KARNA & PANINI).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Analytics Fleet.

A sync reads the live board and rebuilds the three Group 85 tables from it:

* every live fixture's Match Odds, selection by selection: Parimatch's and 1xBet's fresh prices
  (``COMPETITIVE_LIVE_BOOKS``), each book's measured overround, the zero-vig consensus price (the median de-vig across the books) and BetDoc's
  ensemble probability, the EV at the better of the two books;
* the public ticket and money shares and the opening price, **only where measured**: the fixture's public splits
  evidence (a named feed or an administrator, ``PUT /twin/intel``), else the share of BetDoc's own recorded bets
  (tickets only). An unmeasured share is left empty; the divergence, RLM and trap that need it do not fire;
* the verdict and the contrarian edge score (``app/domain/competitive_intel/live_consensus_math.py``) and a stake
  in INR from KUMBHA's single sizing policy at ``COMPETITIVE_LIVE_REFERENCE_BANKROLL_INR`` (no stake on a trap),
  with its USD figure at TODAR MAL's fresh rate (none without one);
* the margin benchmarks per book and sport, beside the zero-vig consensus benchmark (0% by construction);
* the public-pick signals: one per fixture with a measured crowd pick (DERIVED), and every pick an administrator
  recorded from a named source (RECORDED), re-graded against the board.

Sports are listed in ``COMPETITIVE_LIVE_SPORT_PRIORITY`` order: Cricket and Football first.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.cfo import growth_math as gm
from app.domain.competitive_intel import live_consensus_math as lcm
from app.domain.manual_parlay.cognitive_parlay_rater import overround
from app.domain.oracle.markets import MarketKind
from app.domain.oracle.match_model import consensus_two_way
from app.domain.oracle.parlay_engine import LegCandidate
from app.models.competitive_intel_live import BookmakerMarginBenchmark, CompetitorPublicConsensusSnapshot, CompetitorSignalFeedItem
from app.schemas.competitive_intel import PublicPickIn

logger = logging.getLogger("betdoc.karna.live_intel")

SHARP_KEY = "sharp_consensus"
SHARP_DISPLAY = "BetDoc Zero-Vig Consensus Benchmark"
MARKET = "Match Odds"
OWN_BETS = "BetDoc recorded bets"
CENT = Decimal("0.01")


class LiveIntelRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


@dataclass(frozen=True, slots=True)
class Shares:
    tickets: float | None  # percent
    money: float | None
    opening: float | None
    source: str | None


def sport_label(sport_key: str | None, settings: Settings) -> str:
    from app.services.manual_parlay import sport_of  # noqa: PLC0415 - the workbench pulls in the twin

    return sport_of(sport_key, settings) or "Other"


def priority(column: Any, settings: Settings) -> Any:
    order = settings.COMPETITIVE_LIVE_SPORT_PRIORITY
    return case({name: i for i, name in enumerate(order)}, value=column, else_=len(order))


def _d(value: float | None, places: str = "0.001") -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _book_prices(legs: Sequence[LegCandidate], book: str, now: datetime, max_age: float) -> dict[str, float]:
    out = {}
    for leg in legs:
        q = leg.quotes.get(book)
        if q is not None and q.odds > 1.0 and q.age(now) <= max_age:
            out[leg.selection] = float(q.odds)
    return out


def fair_prices(legs: Sequence[LegCandidate], now: datetime, max_age: float) -> tuple[dict[str, float], dict[str, float], str] | None:
    """(BetDoc's probability, the zero-vig consensus, the basis) per selection of one fixture's Match Odds.

    A complete 1X2 market: the consensus Ashoka's engine computed and the models' ensemble on it. A two-way market
    (cricket, tennis, basketball: no draw is quoted) has no model: its probability is the median multiplicative
    de-vig across the books quoting every selection. None when no book quotes the whole market."""
    if all(leg.consensus is not None for leg in legs):
        consensus = {leg.selection: float(leg.consensus) for leg in legs}  # type: ignore[arg-type]
        try:
            return {leg.selection: float(leg.probability) for leg in legs}, consensus, "models"
        except ValueError:
            return consensus, consensus, "market consensus"
    selections = [leg.selection for leg in legs]
    books = {b for leg in legs for b in leg.quotes}
    lines = [{leg.selection: leg.quotes[b].odds for leg in legs if b in leg.quotes and leg.quotes[b].age(now) <= max_age} for b in books]
    fair = consensus_two_way([line for line in lines if len(line) == len(selections)], selections)
    return None if fair is None else (fair, fair, "market consensus")


def summary_text(label: str, best_book: str, best_odds: float, fair_odds: float | None, prob: float, ev: Decimal, shares: Shares,
                 evaluation: lcm.ConsensusEvaluation, basis: str) -> str:
    parts = [f"{label}: best price {best_book} {best_odds:.2f}" + (f" against the zero-vig {fair_odds:.2f}" if fair_odds else "")
             + f"; BetDoc gives {prob:.1%} ({basis}), EV {ev:+.1f}%."]
    if shares.tickets is not None:
        money = f", {shares.money:.0f}% of the money" if shares.money is not None else ""
        parts.append(f"Public {shares.tickets:.0f}% of tickets{money} ({shares.source}).")
    else:
        parts.append("No public split measured for this market.")
    if evaluation.rlm_detected and shares.opening is not None:
        parts.append(f"Reverse line movement: opened {shares.opening:.2f}, now {best_odds:.2f}.")
    if evaluation.public_trap_flag:
        parts.append("PUBLIC TRAP: the crowd's side is worth less than its price; no stake.")
    return " ".join(parts)[:600]


async def measured_shares(db: AsyncSession, redis: Redis, settings: Settings, legs: Sequence[LegCandidate], now: datetime) -> dict[str, Shares]:
    """leg id -> the measured shares on the leg's own market: the public splits evidence, else BetDoc's own recorded
    bets (tickets only), else nothing. KARNA's board and the Consultant's TipMaster panel (Group 105) read the same."""
    from app.domain.popular_picks.trends import crowd_shares  # noqa: PLC0415 - pulls in the trend scan
    from app.services.twin.intel import read_intel  # noqa: PLC0415

    try:
        intel = await read_intel(redis, settings, {leg.fixture_id for leg in legs})
    except (RedisError, OSError):
        intel = {}
    crowd = await crowd_shares(db, legs, now)
    out: dict[str, Shares] = {}
    for leg in legs:
        splits = intel.get(leg.fixture_id).public_splits if intel.get(leg.fixture_id) is not None else None  # type: ignore[union-attr]
        opening = None if splits is None else splits.opening_odds.get(leg.market.key, {}).get(leg.selection)
        measured = None if splits is None else splits.splits.get(leg.market.key, {}).get(leg.selection)
        if measured is not None:
            out[leg.leg_id] = Shares(100.0 * measured.tickets_pct, None if measured.money_pct is None else 100.0 * measured.money_pct, opening, splits.source)  # type: ignore[union-attr]
        elif leg.leg_id in crowd:
            out[leg.leg_id] = Shares(100.0 * crowd[leg.leg_id], None, opening, OWN_BETS)
        else:
            out[leg.leg_id] = Shares(None, None, opening, None)
    return out


class CompetitiveIntelLiveService:
    """KARNA's live board. Developed for Amit Ashok Kumar Patnaik."""

    async def _usd_rate(self, db: AsyncSession, redis: Redis | None, settings: Settings, now: datetime) -> Decimal | None:
        from app.services.currency_service import CurrencyService, CurrencyUnavailableError  # noqa: PLC0415

        try:
            return (await CurrencyService(settings, redis).inr_per_unit(db, "USD", now, allow_refresh=False)).inr_per_unit  # TODAR MAL refreshes on its own beat
        except CurrencyUnavailableError:
            logger.warning("KARNA: no fresh USD rate; USD figures left empty")
            return None

    async def _shares(self, db: AsyncSession, redis: Redis, settings: Settings, legs: Sequence[LegCandidate], now: datetime) -> dict[str, Shares]:
        return await measured_shares(db, redis, settings, legs, now)

    async def sync_live_consensus(self, db: AsyncSession, redis: Redis | None, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
        from app.services.ashoka_market import load_candidates, market_selection_label  # noqa: PLC0415 - the oracle engine is heavy
        from app.services.twin.vetting import LIVE_LOOKBACK, developer_credit  # noqa: PLC0415

        if redis is None:
            raise LiveIntelRefusal("NO_REDIS", "The live board lives in Redis, which is unavailable", 503)
        now = now or datetime.now(UTC)
        try:
            legs, _ = await load_candidates(redis, settings, now - LIVE_LOOKBACK)
        except (RedisError, OSError, TimeoutError) as exc:
            raise LiveIntelRefusal("NO_BOARD", "The live board could not be read", 503) from exc
        legs = [leg for leg in legs if leg.market.kind is MarketKind.MATCH_ODDS]
        by_fixture: dict[str, list[LegCandidate]] = defaultdict(list)
        for leg in legs:
            by_fixture[leg.fixture_id].append(leg)
        shares = await self._shares(db, redis, settings, legs, now)
        rate = await self._usd_rate(db, redis, settings, now)
        policy, sizing = lcm.ConsensusPolicy.from_settings(settings), gm.SizingPolicy.from_settings(settings)
        bankroll = settings.COMPETITIVE_LIVE_REFERENCE_BANKROLL_INR
        books = settings.COMPETITIVE_LIVE_BOOKS
        max_age = settings.ASHOKA_MAX_QUOTE_AGE_SECONDS

        snapshots: list[CompetitorPublicConsensusSnapshot] = []
        margins: dict[tuple[str, str], list[float]] = defaultdict(list)
        best_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])  # (book, sport) -> [best, contested]
        sharp_samples: dict[str, int] = defaultdict(int)
        for fixture_id, fx_legs in by_fixture.items():
            head = fx_legs[0]
            sport = sport_label(head.sport_key, settings)
            prices = {book: _book_prices(fx_legs, book, now, max_age) for book in books}
            fair = fair_prices(fx_legs, now, max_age)
            if fair is None:
                continue
            probability, consensus, basis = fair
            book_margin = {}
            for book, quoted in prices.items():
                complete = {leg.selection: quoted.get(leg.selection) for leg in fx_legs}
                if all(v is not None for v in complete.values()):
                    m = overround(complete)  # type: ignore[arg-type]
                    if m is not None:
                        book_margin[book] = 100.0 * m
                        margins[(book, sport)].append(100.0 * m)
            sharp_samples[sport] += 1
            for leg in fx_legs:
                quoted = {book: prices[book].get(leg.selection) for book in books}
                offered = {b: o for b, o in quoted.items() if o is not None}
                if not offered:
                    continue
                if len(offered) == len(books):
                    top = max(offered.values())
                    for b, o in offered.items():
                        best_counts[(b, sport)][1] += 1
                        best_counts[(b, sport)][0] += int(o >= top)
                best_book = max(offered, key=lambda b: offered[b])
                best_odds = offered[best_book]
                share = shares[leg.leg_id]
                prob = probability[leg.selection]
                cheapest = min((book_margin[b] for b in offered if b in book_margin), default=None)
                ev = lcm.evaluate_consensus_signal(public_ticket_pct=share.tickets, money_pct=share.money, opening_odds=share.opening, best_odds=best_odds,
                                                   cheapest_margin_pct=cheapest, betdoc_true_prob=prob, policy=policy)
                stake = Decimal("0.00")
                if not ev.public_trap_flag:
                    stake = gm.size(gm.kelly_fraction(best_odds, prob), best_odds, bankroll, 0.0, sizing, settings.TWIN_STAKE_STEP_INR).stake
                fair_odds = 1.0 / consensus[leg.selection] if consensus.get(leg.selection) else None
                label = market_selection_label(MARKET, leg.selection, leg.home, leg.away)
                snapshots.append(CompetitorPublicConsensusSnapshot(
                    id=uuid4(), match_id=fixture_id, sport=sport, sport_key=leg.sport_key, league=leg.league, home_team=leg.home, away_team=leg.away,
                    kickoff_at=leg.kickoff, market_type=MARKET, selection=leg.selection, selection_label=label[:160],
                    public_ticket_pct=_d(share.tickets, "0.01"), sharp_money_pct=_d(share.money, "0.01"), public_source=None if share.source is None else share.source[:96],
                    divergence_pct=ev.divergence_pct, rlm_detected=ev.rlm_detected, opening_odds=_d(share.opening),
                    parimatch_odds=_d(quoted.get("parimatch")), one_xbet_odds=_d(quoted.get("1xbet")), sharp_fair_odds=_d(fair_odds),
                    parimatch_margin_pct=_d(book_margin.get("parimatch"), "0.01"), one_xbet_margin_pct=_d(book_margin.get("1xbet"), "0.01"),
                    best_bookmaker=books.get(best_book, best_book), best_odds=_d(best_odds), betdoc_true_prob=_d(prob, "0.0001"), betdoc_ev_pct=ev.betdoc_ev_pct,
                    public_trap_flag=ev.public_trap_flag, contrarian_edge_score=ev.contrarian_edge_score, verdict=ev.verdict,
                    plain_english_summary=summary_text(label, books.get(best_book, best_book), best_odds, fair_odds, prob, ev.betdoc_ev_pct, share, ev, basis),
                    recommended_stake_inr=stake, recommended_stake_usd=None if rate is None else (stake / rate).quantize(CENT, rounding=ROUND_HALF_UP),
                    source_provider="KARNA_CONSENSUS_ENGINE", captured_at=now,
                ))

        benchmarks = []
        for (book, sport), values in sorted(margins.items()):
            mean = sum(values) / len(values)
            best, contested = best_counts.get((book, sport), [0, 0])
            benchmarks.append(BookmakerMarginBenchmark(
                id=uuid4(), bookmaker_key=book, bookmaker_display=books.get(book, book), sport=sport, market_type=MARKET,
                measured_overround_pct=_d(mean, "0.01"), payout_efficiency_pct=_d(100.0 * 100.0 / (100.0 + mean), "0.01"),
                best_price_share_pct=None if not contested else _d(100.0 * best / contested, "0.01"), sample_size=len(values), updated_at=now))
        for sport, n in sorted(sharp_samples.items()):
            benchmarks.append(BookmakerMarginBenchmark(id=uuid4(), bookmaker_key=SHARP_KEY, bookmaker_display=SHARP_DISPLAY, sport=sport, market_type=MARKET,
                                                       measured_overround_pct=Decimal("0.00"), payout_efficiency_pct=Decimal("100.00"), best_price_share_pct=None,
                                                       sample_size=n, updated_at=now))

        by_match: dict[str, list[CompetitorPublicConsensusSnapshot]] = defaultdict(list)
        for snap in snapshots:
            by_match[snap.match_id].append(snap)
        derived = []
        for match_id, rows in by_match.items():
            measured = [r for r in rows if r.public_ticket_pct is not None]
            if not measured:
                continue
            pick = max(measured, key=lambda r: r.public_ticket_pct or Decimal(0))
            derived.append(self._grade(pick, rows, origin="DERIVED", source=pick.public_source or OWN_BETS, backing=pick.public_ticket_pct or Decimal(0),
                                       settings=settings, rate=rate, now=now))

        await db.execute(delete(CompetitorPublicConsensusSnapshot))
        await db.execute(delete(BookmakerMarginBenchmark))
        await db.execute(delete(CompetitorSignalFeedItem).where(CompetitorSignalFeedItem.origin == "DERIVED"))
        db.add_all(snapshots + benchmarks + derived)
        regraded = 0
        for rec in (await db.execute(select(CompetitorSignalFeedItem).where(CompetitorSignalFeedItem.origin == "RECORDED"))).scalars():
            rows = by_match.get(rec.match_id)
            pick = next((r for r in rows or [] if r.selection == rec.public_selection), None)
            if pick is None:
                continue
            fresh = self._grade(pick, rows or [], origin="RECORDED", source=rec.source_site, backing=rec.public_backing_pct, settings=settings, rate=rate, now=now)
            for field in ("public_pick", "betdoc_verdict", "betdoc_recommended_pick", "best_bookmaker", "best_odds", "betdoc_ev_pct", "plain_english_rationale",
                          "stake_inr", "stake_usd", "graded_at", "sport", "match_title"):
                setattr(rec, field, getattr(fresh, field))
            regraded += 1
        await db.commit()
        credit = await developer_credit(db)
        logger.info("KARNA: %d selections on %d fixtures, %d benchmarks, %d derived and %d re-graded picks", len(snapshots), len(by_match), len(benchmarks), len(derived), regraded)
        return {"snapshots_synced": len(snapshots), "benchmarks_synced": len(benchmarks), "signals_synced": len(derived) + regraded,
                "fixtures_read": len(by_fixture), "usd_rate": None if rate is None else float(rate), "architect": credit}

    def _grade(self, pick: CompetitorPublicConsensusSnapshot, rows: Sequence[CompetitorPublicConsensusSnapshot], *, origin: str, source: str, backing: Decimal,
               settings: Settings, rate: Decimal | None, now: datetime) -> CompetitorSignalFeedItem:
        """The crowd's pick graded: a trap (and the best positive-EV alternative, if any), a sharp-verified side, a high-margin caution, or neutral."""
        ev = float(pick.betdoc_ev_pct)
        trap = float(backing) >= settings.COMPETITIVE_TRAP_PUBLIC_PCT and (pick.public_trap_flag or ev < settings.COMPETITIVE_TRAP_MAX_EV_PCT)
        margins = [float(m) for m in (pick.parimatch_margin_pct, pick.one_xbet_margin_pct) if m is not None]
        rec: CompetitorPublicConsensusSnapshot | None = pick
        if trap:
            verdict = "PUBLIC_TRAP"
            others = sorted((r for r in rows if r.selection != pick.selection and float(r.betdoc_ev_pct) > 0 and not r.public_trap_flag),
                            key=lambda r: float(r.betdoc_ev_pct), reverse=True)
            rec = others[0] if others else None
            why = (f"{float(backing):.0f}% of the public ({source}) backs {pick.selection_label} at {float(pick.best_odds):.2f}, worth EV {ev:+.1f}% to BetDoc. "
                   + (f"The better side is {rec.selection_label} at {rec.best_bookmaker} {float(rec.best_odds):.2f} (EV {float(rec.betdoc_ev_pct):+.1f}%)."
                      if rec is not None else "No side of this market has positive EV: skip it."))
        elif pick.verdict in ("SHARP_CONTRARIAN", "SHARP_CONSENSUS"):
            verdict, why = "SHARP_VERIFIED", f"The crowd's side ({float(backing):.0f}%, {source}) is also BetDoc's: {pick.plain_english_summary}"
        elif margins and min(margins) > settings.COMPETITIVE_HIGH_VIG_PCT:
            verdict, rec = "CAUTION_HIGH_VIG", None
            why = (f"{pick.selection_label} is the crowd's pick ({float(backing):.0f}%, {source}), but the cheaper account book's margin is {min(margins):.1f}%, "
                   f"over {settings.COMPETITIVE_HIGH_VIG_PCT:g}%: the price gives too much away.")
        else:
            verdict, why = "NEUTRAL", f"The crowd's pick ({float(backing):.0f}%, {source}): {pick.plain_english_summary}"
        stake = Decimal("0.00") if rec is None else rec.recommended_stake_inr
        return CompetitorSignalFeedItem(
            id=uuid4(), origin=origin, source_site=source[:96], sport=pick.sport, match_id=pick.match_id, match_title=f"{pick.home_team} v {pick.away_team}"[:260],
            public_selection=pick.selection, public_pick=f"{pick.selection_label} @ {float(pick.best_odds):.2f}"[:160], public_backing_pct=Decimal(str(backing)).quantize(CENT),
            betdoc_verdict=verdict, betdoc_recommended_pick=("Skip this market" if rec is None else f"{rec.selection_label} @ {float(rec.best_odds):.2f}")[:160],
            best_bookmaker=None if rec is None else rec.best_bookmaker, best_odds=None if rec is None else rec.best_odds,
            betdoc_ev_pct=None if rec is None else rec.betdoc_ev_pct, plain_english_rationale=why[:600], stake_inr=stake,
            stake_usd=None if rate is None else (stake / rate).quantize(CENT, rounding=ROUND_HALF_UP), created_at=now, graded_at=now,
        )

    async def record_public_pick(self, db: AsyncSession, redis: Redis | None, settings: Settings, body: PublicPickIn, by: str) -> CompetitorSignalFeedItem:
        """An administrator's reading of a named source's public pick, graded now against the live board."""
        snaps = list((await db.execute(select(CompetitorPublicConsensusSnapshot).where(CompetitorPublicConsensusSnapshot.match_id == body.match_id))).scalars())
        if not snaps:
            await self.sync_live_consensus(db, redis, settings)
            snaps = list((await db.execute(select(CompetitorPublicConsensusSnapshot).where(CompetitorPublicConsensusSnapshot.match_id == body.match_id))).scalars())
        pick = next((s for s in snaps if s.selection == body.selection), None)
        if pick is None:
            raise LiveIntelRefusal("NOT_ON_BOARD", "Neither Parimatch nor 1xBet prices that selection on the live board", 404)
        now = datetime.now(UTC)
        rate = await self._usd_rate(db, redis, settings, now)
        graded = self._grade(pick, snaps, origin="RECORDED", source=" ".join(body.source_site.split()), backing=Decimal(str(body.public_backing_pct)),
                             settings=settings, rate=rate, now=now)
        graded.recorded_by = by[:64]
        existing = (await db.execute(select(CompetitorSignalFeedItem).where(CompetitorSignalFeedItem.origin == "RECORDED",
                                                                            CompetitorSignalFeedItem.source_site == graded.source_site,
                                                                            CompetitorSignalFeedItem.match_id == body.match_id))).scalars().first()
        if existing is not None:
            await db.delete(existing)
            await db.flush()
        db.add(graded)
        await db.commit()
        return graded

    async def _ensure(self, db: AsyncSession, redis: Redis | None, settings: Settings) -> None:
        """The first read syncs when the board has never been read (and Redis is there to read it)."""
        if redis is not None and (await db.execute(select(CompetitorPublicConsensusSnapshot.id).limit(1))).scalar_one_or_none() is None:
            try:
                await self.sync_live_consensus(db, redis, settings)
            except LiveIntelRefusal as exc:
                logger.warning("KARNA: first sync skipped (%s)", exc.reason)

    async def get_live_consensus_board(self, db: AsyncSession, redis: Redis | None, settings: Settings, *, sport: str | None = None,
                                       public_trap_only: bool = False) -> list[CompetitorPublicConsensusSnapshot]:
        await self._ensure(db, redis, settings)
        stmt = select(CompetitorPublicConsensusSnapshot).order_by(priority(CompetitorPublicConsensusSnapshot.sport, settings),
                                                                  CompetitorPublicConsensusSnapshot.contrarian_edge_score.desc(),
                                                                  CompetitorPublicConsensusSnapshot.match_id, CompetitorPublicConsensusSnapshot.selection)
        if sport and sport.strip().casefold() != "all":
            stmt = stmt.where(CompetitorPublicConsensusSnapshot.sport == sport.strip())
        if public_trap_only:
            stmt = stmt.where(CompetitorPublicConsensusSnapshot.public_trap_flag.is_(True))
        return list((await db.execute(stmt)).scalars())

    async def get_bookmaker_benchmarks(self, db: AsyncSession, redis: Redis | None, settings: Settings, *, sport: str | None = None) -> list[BookmakerMarginBenchmark]:
        await self._ensure(db, redis, settings)
        stmt = select(BookmakerMarginBenchmark).order_by(priority(BookmakerMarginBenchmark.sport, settings), BookmakerMarginBenchmark.measured_overround_pct)
        if sport and sport.strip().casefold() != "all":
            stmt = stmt.where(BookmakerMarginBenchmark.sport == sport.strip())
        return list((await db.execute(stmt)).scalars())

    async def get_public_traps_feed(self, db: AsyncSession, redis: Redis | None, settings: Settings, *, sport: str | None = None) -> list[CompetitorSignalFeedItem]:
        await self._ensure(db, redis, settings)
        stmt = select(CompetitorSignalFeedItem).order_by(priority(CompetitorSignalFeedItem.sport, settings), CompetitorSignalFeedItem.public_backing_pct.desc())
        if sport and sport.strip().casefold() != "all":
            stmt = stmt.where(CompetitorSignalFeedItem.sport == sport.strip())
        return list((await db.execute(stmt)).scalars())
