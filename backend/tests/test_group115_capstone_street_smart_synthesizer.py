"""Group 115, the capstone: the street-smart multi-sport synthesizer, the three-ticket system splitter, TODAR MAL's daily
market health with its once-a-day Telegram/SMS intimation, and the Telegram commands /today, /synthesize, /split.

* Every leg comes from the live board (Ashoka's candidates) priced at Parimatch or 1xBet: an anchor clears the
  probability floor, the book pays at least fair, the quote is fresh, the fixture has not started; one leg per fixture,
  no sport over its share while another sport is priced. An unreachable target is said so; an empty board builds nothing.
* The split is exact Decimal money: 40/40/20 of the session; Slip A the fewest anchors whose odds return the whole
  session stake (else the shield is reported off); the nested tickets' four outcomes and the chance of getting the
  stake back. Recorded tickets lock nothing; a placement confirmed with its booking code locks the stake and its legs
  become the Oracle bet that settles it, with the 30% TDS on the net winnings.
* The day's score comes from measured inputs only (the desk books' overround, top-tier depth from the league registry,
  the Wire's rotation news and absences, the anchors); what cannot be measured is named, never defaulted. A rest day or
  a prime day pages the phone once and the 08:00 hype stands down; a selective day sends nothing.
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user, get_db
from app.api.v1.real_money import router as real_money_router
from app.api.v1.synthesizer import router as synthesizer_router
from app.core.config import Settings, get_settings
from app.domain import street_smart as ss
from app.domain.oracle.markets import parse_market
from app.domain.oracle.parlay_engine import LegCandidate, Quote
from app.models import User
from app.models.cfo_vault import MarketResult
from app.models.digital_twin import TwinInPlayMonitor
from app.models.market_health import DailyMarketHealthSnapshot
from app.models.real_money import RealBankroll, RealBetTicket, RealCashTransaction
from app.models.sentinel import SentinelDelivery
from app.models.sports_config import LeagueRegistry, SportConfiguration
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import ScoreIn
from app.services import handcrafted_execution_service as hes
from app.services import live_result_settlement_service as settlement
from app.services import market_health_service as health
from app.services import pocket_twin as twin
from app.services import real_money_service as rms
from app.services import sentinel_hype
from app.services import street_smart_synthesizer as synth
from app.services import user_pnl_tracker
from app.services.sentinel_bus import SentinelKeys

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
NOW = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)  # 11:30 IST: the fixtures below kick off later the same local day
DEVELOPER = "Amit Ashok Kumar Patnaik"
MATCH_ODDS = parse_market("Match Odds")
MODELS = (User, UserPlacedBet, UserPlacedLeg, TwinInPlayMonitor, RealBankroll, RealBetTicket, RealCashTransaction, TaxLedgerEntry, FixtureScore, MarketResult,
          DailyMarketHealthSnapshot, SportConfiguration, LeagueRegistry, NewsArticleSentiment, InjuryRosterReport, SentinelDelivery)


@pytest.fixture
def settings() -> Settings:
    return get_settings()


# ================================================================ the board, built leg by leg
def leg(n: int, sport_key: str, p: float, odds: float, *, now: datetime = NOW, home: str | None = None, away: str | None = None, books: Iterable[str] = ("parimatch",),
        kickoff: timedelta = timedelta(hours=3), age: timedelta = timedelta(seconds=30), market: str = "Match Odds", selection: str = "HOME") -> LegCandidate:
    ref = parse_market(market)
    assert ref is not None
    return LegCandidate(fixture_id=f"fx-{n}", home=home or f"Home {n}", away=away or f"Away {n}", market=ref, selection=selection,
                        quotes={b: Quote(b, odds, now - age) for b in books}, consensus=p, consensus_books=4, sport_key=sport_key,
                        league=sport_key.split("_", 1)[1].upper(), kickoff=now + kickoff)


SPORTS = ("soccer_epl", "cricket_ipl", "tennis_atp_paris", "basketball_nba")


def board(now: datetime = NOW, n: int = 16) -> list[LegCandidate]:
    """``n`` anchors across four sports, each priced just above fair at Parimatch (probability falls with n)."""
    out = []
    for i in range(n):
        p = round(0.90 - 0.01 * i, 2)
        out.append(leg(i, SPORTS[i % 4], p, round(1 / p + 0.03, 2), now=now, books=("parimatch", "1xbet")))
    return out


def use_board(monkeypatch: pytest.MonkeyPatch, candidates: list[LegCandidate]) -> None:
    async def fake(redis: Any, settings: Settings, now: datetime) -> list[LegCandidate]:  # noqa: ARG001
        return candidates
    monkeypatch.setattr(synth, "board", fake)


class FakeRedis:
    """A dict: what the commands and the intimation keep (set NX/EX, get, exists, delete)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> bool:  # noqa: ARG002
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.data)

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def lrange(self, *_: Any) -> list[str]:
        return []


# ================================================================ the synthesizer
def test_anchors_are_fresh_fair_priced_and_before_kickoff(settings: Settings) -> None:
    ok = leg(1, "soccer_epl", 0.80, 1.30)  # EV +4%
    cases = [
        ok,
        leg(2, "soccer_epl", 0.55, 1.90),  # under the 60% floor
        leg(3, "soccer_epl", 0.80, 1.20),  # EV -4%: the book pays under fair
        leg(4, "soccer_epl", 0.80, 1.30, age=timedelta(seconds=settings.ASHOKA_MAX_QUOTE_AGE_SECONDS + 60)),  # a stale price
        leg(5, "soccer_epl", 0.80, 1.30, kickoff=timedelta(hours=-1)),  # already started
        leg(6, "soccer_epl", 0.80, 1.30, kickoff=timedelta(hours=60)),  # beyond the 48-hour horizon
        leg(7, "soccer_epl", 0.80, 1.30, books=("stake",)),  # not quoted at the book
    ]
    found = synth.anchors(cases, "parimatch", settings, NOW)
    assert [a.leg_id for a in found] == [ok.leg_id]
    a = found[0]
    assert (a.odds, a.sport, a.code, a.book) == (D("1.3"), "Football", "1", "parimatch") and a.ev == pytest.approx(0.04)
    assert synth.anchors(cases, "1xbet", settings, NOW) == []


def test_the_synthesizer_spreads_anchors_across_sports_to_the_target(settings: Settings) -> None:
    anchors = synth.anchors(board(), "parimatch", settings, NOW)
    result = ss.synthesize(anchors, stake=D("250"), target_return=D("5000"), min_legs=4, max_legs=15, max_sport_share=0.5)
    assert result.reached_target and result.total_odds >= D("20") and result.target_odds == D("20.0000")
    assert result.projected_return == ss.paisa(D("250") * result.total_odds) and result.projected_return >= D("5000")
    assert len(result.sports) == 4 and len({x.fixture_id for x in result.legs}) == len(result.legs)
    assert [x.probability for x in result.legs] == sorted((x.probability for x in result.legs), reverse=True)  # safest first
    assert result.joint_probability == pytest.approx(__import__("math").prod(x.probability for x in result.legs))
    assert result.shortfall is None and result.diversification_relaxed is False
    # one sport only: the share cap does not apply when no other sport is priced
    football = [x for x in anchors if x.sport == "Football"]
    solo = ss.synthesize(football, stake=D("100"), target_return=D("150"), min_legs=2, max_legs=15, max_sport_share=0.5)
    assert solo.reached_target and {x.sport for x in solo.legs} == {"Football"}
    # a target the anchors cannot reach is said so, with what they do reach
    far = ss.synthesize(anchors[:5], stake=D("100"), target_return=D("100000"), min_legs=4, max_legs=15, max_sport_share=0.5)
    assert not far.reached_target and len(far.legs) == 5 and "short of the 1000.00x target" in (far.shortfall or "")
    empty = ss.synthesize([], stake=D("250"), target_return=D("5000"), min_legs=4, max_legs=15, max_sport_share=0.5)
    assert empty.legs == [] and empty.projected_return == D("0.00") and empty.shortfall.startswith("No leg on the live board")


def test_preferred_sports_and_the_share_cap(settings: Settings) -> None:
    anchors = synth.anchors(board(n=16), "parimatch", settings, NOW)
    tennis_cricket = ss.synthesize(anchors, stake=D("100"), target_return=D("1000"), min_legs=4, max_legs=8, max_sport_share=0.5, preferred_sports=["Tennis", "cricket"])
    assert {x.sport for x in tennis_cricket.legs} <= {"Tennis", "Cricket"}
    capped = ss.synthesize(anchors, stake=D("100"), target_return=D("100000"), min_legs=4, max_legs=8, max_sport_share=0.25)
    counts = {s: sum(1 for x in capped.legs if x.sport == s) for s in {x.sport for x in capped.legs}}
    assert max(counts.values()) <= 2  # ceil(8 x 0.25)


# ================================================================ the splitter
def _flat(n: int, odds: str, p: float) -> list[ss.PricedLeg]:
    return [ss.PricedLeg(f"fx-{i}|Match Odds|HOME", f"fx-{i}", f"H{i}", f"A{i}", "soccer_epl", "Football", "EPL", NOW + timedelta(hours=3), "Match Odds", "HOME",
                         f"H{i}", "parimatch", D(odds), p, p * float(odds) - 1, 4, "1") for i in range(n)]


def test_the_split_is_exact_and_the_shield_covers_the_session() -> None:
    result = ss.split(_flat(15, "1.25", 0.85), D("250.00"), (D("0.40"), D("0.40"), D("0.20")))
    a, b, c = result.slips
    assert [len(s.legs) for s in result.slips] == [5, 9, 15] and [s.stake for s in result.slips] == [D("100.00"), D("100.00"), D("50.00")]
    assert (a.odds, a.payout, b.odds, b.payout, c.odds, c.payout) == (D("3.0518"), D("305.18"), D("7.4506"), D("745.06"), D("28.4217"), D("1421.09"))
    assert a.covers_session and result.shield_active and result.needed_a_odds == D("2.5000")  # 1.25^4 = 2.44 would not cover
    assert (a.probability, b.probability, c.probability) == pytest.approx((0.85 ** 5, 0.85 ** 9, 0.85 ** 15))
    returns = {s["code"]: D(s["return_inr"]) for s in result.scenarios}
    assert returns == {"ALL_WIN": D("2471.33"), "A_AND_B": D("1050.24"), "A_ONLY": D("305.18"), "NONE": D("0.00")}
    assert sum(s["probability"] for s in result.scenarios) == pytest.approx(1.0)
    assert result.recover_probability == pytest.approx(0.85 ** 5)  # every outcome but "an anchor loses" returns the stake
    assert result.expected_return == ss.paisa(sum((D(str(p)) * r for p, r in ((0.85 ** 15, D("2471.33")), (0.85 ** 9 - 0.85 ** 15, D("1050.24")),
                                                                              (0.85 ** 5 - 0.85 ** 9, D("305.18")))), D(0)))
    assert result.single == {"legs": 15, "odds": "28.4217", "payout_inr": "7105.43", "probability": round(0.85 ** 15, 6),
                             "expected_return_inr": str(ss.paisa(D("7105.43") * D(str(0.85 ** 15)))), "recover_probability": round(0.85 ** 15, 6)}


def test_the_shield_is_reported_off_and_bad_splits_are_refused() -> None:
    low = ss.split(_flat(9, "1.10", 0.9), D("1000.00"), (D("0.40"), D("0.40"), D("0.20")))
    assert not low.shield_active and len(low.slips[0].legs) == 5 and low.slips[0].odds == D("1.6105")  # 1.1^5 < 2.5
    five = ss.split(_flat(5, "1.40", 0.75), D("1000.00"), (D("0.40"), D("0.40"), D("0.20")))
    assert [len(s.legs) for s in five.slips] == [3, 4, 5] and five.shield_active and five.slips[0].payout == D("1097.60")  # 400 x 2.744
    with pytest.raises(ValueError, match="at least 5 legs"):
        ss.split(_flat(4, "1.40", 0.75), D("100"), (D("0.40"), D("0.40"), D("0.20")))
    twins = _flat(5, "1.40", 0.75)
    twins[1] = replace(twins[1], fixture_id="fx-0", leg_id="fx-0|Totals 2.5|OVER")
    with pytest.raises(ValueError, match="one leg per fixture"):
        ss.split(twins, D("100"), (D("0.40"), D("0.40"), D("0.20")))


# ================================================================ the day's score
def _anchors(n: int) -> list[ss.PricedLeg]:
    return _flat(n, "1.30", 0.8)


def test_the_day_is_scored_from_measured_deductions_only() -> None:
    t = ss.HealthThresholds()
    rest = ss.score_day(ss.HealthInputs(2, 1, 0.082, 4, 4, [], 4), t)
    assert (rest.score, rest.tier) == (0, "REST_DAY") and {d["code"] for d in rest.deductions} == {"MARGIN_SEVERE", "SLATE_THIN", "ROTATION_HEAVY", "VALUE_NONE"}
    assert rest.title == "⏸️ TODAR MAL ADVICE: NO BETTING TODAY (0/100)." and "excessive bookmaker juice (8.2% average Match Odds overround)" in rest.body
    assert rest.body.endswith("True mastery is capital preservation. Sit out today and await optimal conditions.")
    prime = ss.score_day(ss.HealthInputs(9, 8, 0.038, 9, 0, _anchors(6), 4), t)
    assert (prime.score, prime.tier, prime.deductions) == (100, "PRIME_HARVEST", [])
    assert prime.title.startswith("🔥 PRIME MULTI-SPORT OPPORTUNITY (100/100)") and "H0 (Football) + H1 (Football) + H2 (Football)" in prime.body
    selective = ss.score_day(ss.HealthInputs(5, 4, 0.06, 5, 1, _anchors(5), 4), t)
    assert (selective.score, selective.tier) == (76, "SELECTIVE") and selective.title == "⚠️ SELECTIVE WINDOW (76/100)."
    blind = ss.score_day(ss.HealthInputs(7, None, None, 0, None, _anchors(4), 4), t)
    assert blind.score == 100 and blind.deductions == [] and len(blind.unmeasured) == 3  # margin, tiers, rotation: named, not deducted


# ================================================================ on a database
def _tables() -> list[Table]:
    wanted: dict[str, Table] = {}
    pending = [m.__table__ for m in MODELS]  # type: ignore[attr-defined]
    while pending:
        table = pending.pop()
        if table.name not in wanted:
            wanted[table.name] = table
            pending.extend(fk.column.table for fk in table.foreign_keys)
    return [t for t in User.metadata.sorted_tables if t.name in wanted]


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = _tables()
    if request.param == "postgres":
        if not TEST_POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=tables))
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(tables))))
            await engine.dispose()
        return
    md = MetaData()
    for table in tables:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _registry(db: AsyncSession) -> None:
    db.add(SportConfiguration(sport_key="soccer", display_name="Football", primary_odds_api_sport_key="soccer_epl"))
    await db.flush()
    db.add(LeagueRegistry(sport_key="soccer", league_key="epl", display_name="Premier League", tier=1, odds_api_sport_key="soccer_epl"))
    db.add(LeagueRegistry(sport_key="soccer", league_key="ipl-x", display_name="Second tier", tier=2, odds_api_sport_key="cricket_ipl"))


@pytest.mark.asyncio
async def test_market_health_measures_the_board_the_registry_and_the_wire(sessions: async_sessionmaker[AsyncSession], settings: Settings,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    # two three-way football fixtures with full desk-book prices (Parimatch 6.0% overround, 1xBet 4.5%), one cricket
    candidates = []
    for n, (pm, x1) in enumerate([((2.0, 3.5, 3.85), (2.05, 3.6, 3.95)), ((1.5, 4.2, 6.4), (1.52, 4.3, 6.6))]):
        for sel, pm_odds, x1_odds in zip(("HOME", "DRAW", "AWAY"), pm, x1, strict=True):
            c = leg(n, "soccer_epl", {"HOME": 0.62, "DRAW": 0.22, "AWAY": 0.16}[sel] if n else {"HOME": 0.49, "DRAW": 0.27, "AWAY": 0.24}[sel], pm_odds, selection=sel,
                    books=())
            c.quotes.update({"parimatch": Quote("parimatch", pm_odds, NOW), "1xbet": Quote("1xbet", x1_odds, NOW)})
            candidates.append(c)
    candidates.append(leg(9, "cricket_ipl", 0.70, 1.50, books=("parimatch",)))  # an anchor: EV +5%
    use_board(monkeypatch, candidates)
    expected = []
    for prices in ((2.0, 3.5, 3.85), (2.05, 3.6, 3.95), (1.5, 4.2, 6.4), (1.52, 4.3, 6.6)):
        expected.append(sum(1 / o for o in prices) - 1)
    async with sessions() as db:
        await _registry(db)
        db.add(User(id=(uid := uuid.uuid4()), username="amit", hashed_password="x"))
        await db.flush()
        db.add(NewsArticleSentiment(fingerprint="f1", title="Manager confirms rotation", summary="", source="x", url="https://x", source_credibility=0.9, sport="soccer",
                                    fixtures=[{"fixture_id": "fx-0", "team": "Home 0", "side": "HOME"}], events=["ROTATION"], sentiment_score=-0.2, tactical_impact="HIGH",
                                    published_at=NOW - timedelta(hours=2), ingested_at=NOW - timedelta(hours=2)))
        for i in range(3):
            db.add(InjuryRosterReport(fixture_id="fx-9", sport="cricket", side="AWAY", team_name="Away 9", player_name=f"P{i}", status="OUT", source_name="espn",
                                      rated_by=uid, updated_at=NOW - timedelta(hours=1)))
        await db.commit()
        snapshot, call = await health.evaluate(db, None, settings, NOW)
        await db.commit()
        assert (snapshot.snapshot_date.isoformat(), snapshot.active_fixtures_count, snapshot.top_tier_fixtures_count) == ("2026-10-23", 3, 2)
        assert snapshot.average_bookmaker_margin == pytest.approx(sum(expected) / 4, abs=1e-6) and snapshot.rotation_risks_count == 2
        assert snapshot.anchors_count >= 1 and snapshot.top_anchors[0]["fixture_id"] == "fx-9"
        assert call.tier == snapshot.advisory_tier and snapshot.advisory_message == call.message
        # re-evaluated the same day: the one row is updated
        again, _ = await health.evaluate(db, None, settings, NOW + timedelta(minutes=1))
        await db.commit()
        assert again.id == snapshot.id and await db.scalar(select(func.count()).select_from(DailyMarketHealthSnapshot)) == 1
        view = health.view(again, await health.delivered(db, again.alert_id))
        assert view["dispatched_telegram"] is False and view["developer_name"] == DEVELOPER and view["average_bookmaker_margin_pct"] == round(again.average_bookmaker_margin * 100, 2)


@pytest.mark.asyncio
async def test_a_rest_day_pages_the_phone_once_and_the_hype_stands_down(sessions: async_sessionmaker[AsyncSession], settings: Settings,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    use_board(monkeypatch, [])  # nothing priced today: a rest day
    sent: list[Any] = []

    async def emit(redis: Any, settings: Settings, alert: Any) -> bool:  # noqa: ARG001
        sent.append(alert)
        return True
    monkeypatch.setattr(health, "emit_alert", emit)
    redis = FakeRedis()
    first = await health.intimate(sessions, redis, settings, NOW)
    assert first["sent"] is True and first["tier"] == "REST_DAY" and len(sent) == 1
    alert = sent[0]
    assert alert.kind == "MARKET_HEALTH" and alert.title.startswith("⏸️ TODAR MAL ADVICE: NO BETTING TODAY") and "Developed for Amit Ashok Kumar Patnaik" in alert.body
    assert "no fixture at Parimatch or 1xBet" in alert.body
    second = await health.intimate(sessions, redis, settings, NOW + timedelta(minutes=10))
    assert second["sent"] is False and second["reason"] == "today's intimation already went out" and len(sent) == 1
    dry = await health.intimate(sessions, FakeRedis(), settings, NOW, dry_run=True)
    assert dry["dry_run"] is True and dry["sent"] is False and len(sent) == 1
    async with sessions() as db:
        snap = (await db.execute(select(DailyMarketHealthSnapshot))).scalar_one()
        assert snap.alert_id == alert.id and snap.dispatched_at is not None
        db.add(SentinelDelivery(alert_id=alert.id, channel="TELEGRAM", status="SENT"))
        db.add(SentinelDelivery(alert_id=alert.id, channel="TWILIO", status="SKIPPED"))
        await db.commit()
        assert await health.delivered(db, snap.alert_id) == {"telegram": True, "sms": False}  # what the log says was delivered

    # the 08:00 hype, on a day worth hyping, stands down after the rest-day warning went out
    forecast = sentinel_hype.MarketForecast(NOW.date(), "Asia/Kolkata", 20, 10, D("40"), 0)

    async def built(*_: Any) -> sentinel_hype.MarketForecast:
        return forecast
    monkeypatch.setattr(sentinel_hype, "build_forecast", built)
    hyped: list[Any] = []

    async def hype_emit(*args: Any) -> bool:
        hyped.append(args)
        return True
    monkeypatch.setattr(sentinel_hype, "emit_alert", hype_emit)
    result = await sentinel_hype.market_forecast_hype(redis, settings, now=NOW)  # type: ignore[arg-type]
    assert result["suppressed_by"] == "market_health" and result["sent"] is False and hyped == []
    assert sentinel_hype.market_health_sent_key(SentinelKeys(settings), NOW.date()) in redis.data


@pytest.mark.asyncio
async def test_a_selective_day_sends_nothing(sessions: async_sessionmaker[AsyncSession], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    use_board(monkeypatch, board(n=2))  # two anchors, no league tier known, no full market: a thin slate and thin value, 100 - 30 - 10
    monkeypatch.setattr(health, "emit_alert", lambda *a: (_ for _ in ()).throw(AssertionError("nothing is sent on a selective day")))
    result = await health.intimate(sessions, FakeRedis(), settings, NOW)
    assert (result["tier"], result["score"], result["sent"], result["reason"]) == ("SELECTIVE", 60, False, "a selective day sends nothing to the phone")


# ================================================================ the three tickets, placed by hand and settled
async def _user(sessions: async_sessionmaker[AsyncSession], cash: str = "10000.00") -> uuid.UUID:
    uid = uuid.uuid4()
    async with sessions() as db:
        db.add(User(id=uid, username=f"amit-{uid.hex[:6]}", hashed_password="x"))
        await db.flush()
        await rms.deposit(db, uid, D(cash))
        await db.commit()
    return uid


def _five(now: datetime) -> list[LegCandidate]:
    return [leg(i, "soccer_epl", 0.75, 1.40, now=now, home=f"Club {i}", away=f"Rival {i}") for i in range(5)]


@pytest.mark.asyncio
async def test_recorded_tickets_lock_nothing_until_placed_then_settle_from_their_legs(sessions: async_sessionmaker[AsyncSession], settings: Settings,
                                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    uid = await _user(sessions)
    use_board(monkeypatch, _five(NOW))
    split = await synth.split_for(None, settings, NOW, leg_ids=[f"fx-{i}|Match Odds|HOME" for i in range(5)], session_stake=D("1000.00"), book="parimatch")
    async with sessions() as db:
        tickets = await synth.record_split(db, settings, uid, split, "parimatch", NOW)
        await db.commit()
    refs = [t.ticket_reference for t in tickets]
    assert [r[:10] for r in refs] == ["PM-SPLIT-A", "PM-SPLIT-B", "PM-SPLIT-C"] and len({r[-6:] for r in refs}) == 1
    a = tickets[0]
    assert (a.status, a.recommended_stake, a.placed_stake, a.target_odds, len(a.legs or [])) == ("PENDING_PLACEMENT", D("400.00"), D("0.00"), D("2.7440"), 3)
    assert a.minimum_profitable_odds == rms.calculate_odds_floor(0.75 ** 3, settings.REAL_MIN_EDGE) and a.settleable_from_score is False
    assert a.clipboard_payload.startswith(f"BETDOC SYSTEM TICKET · {a.ticket_reference}") and "[1] @ 1.4" in a.clipboard_payload
    async with sessions() as db:
        bank = (await db.execute(select(RealBankroll).where(RealBankroll.user_id == uid))).scalar_one()
        assert (bank.cash_balance, bank.locked_exposure) == (D("10000.00"), D("0.00"))  # recording locks nothing
        listed = await synth.system_tickets(db, settings, uid)
        assert [t["ticket_reference"] for t in listed if t["status"] == "PENDING_PLACEMENT"] and all(t["legs"] for t in listed)

    async with sessions() as db:
        placed = await hes.confirm_ticket_placement(db, None, settings, uid, a.ticket_reference, "PM77AB", NOW)
        await db.commit()
    t = placed.ticket
    assert (t.status, t.placed_stake, t.placed_odds, t.bookmaker_slip_id) == ("PLACED", D("400.00"), D("2.7440"), "PM77AB") and placed.monitor is not None
    async with sessions() as db:
        bet = await db.get(UserPlacedBet, t.user_placed_bet_id)
        assert (bet.structure, bet.stake_inr, bet.booking_code) == ("TREBLE", D("400.00"), "PM77AB")
        again = await hes.confirm_ticket_placement(db, None, settings, uid, a.ticket_reference, "PM77AB", NOW)
        assert again.replayed is True
        c_ref = refs[2]
        cancelled = await synth.cancel_ticket(db, uid, c_ref, NOW)
        await db.commit()
        assert cancelled.status == "CANCELLED"
        for ref in (c_ref, a.ticket_reference):
            with pytest.raises(synth.SynthesisError) as refused:
                await synth.cancel_ticket(db, uid, ref, NOW)
            assert refused.value.status_code == 409

    async with sessions() as db:  # Slip A's three anchors win: the ticket settles from its Oracle bet, 30% TDS on the net winnings
        for i in range(3):
            await user_pnl_tracker.record_score(db, ScoreIn(fixture_id=f"fx-{i}", home=f"Club {i}", away=f"Rival {i}", sport_key="soccer_epl", home_goals=2, away_goals=0),
                                                source="test", by=None, now=NOW)
        await db.commit()
    await user_pnl_tracker.settle_pending(sessions, NOW)
    async with sessions() as db:
        report = await settlement.settle_real_tickets(db, settings, NOW)
        await db.commit()
        settled = (await db.execute(select(RealBetTicket).where(RealBetTicket.ticket_reference == a.ticket_reference))).scalar_one()
        bank = (await db.execute(select(RealBankroll).where(RealBankroll.user_id == uid))).scalar_one()
    assert report.settled == 1
    assert (settled.status, settled.gross_return, settled.tds_deducted, settled.settlement_payout, settled.net_profit) == (
        "WON", D("1097.60"), D("209.28"), D("888.32"), D("488.32"))
    assert (bank.cash_balance, bank.locked_exposure) == (D("10488.32"), D("0.00"))


@pytest.mark.asyncio
async def test_stale_legs_and_unplaceable_tickets_are_refused(sessions: async_sessionmaker[AsyncSession], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    use_board(monkeypatch, _five(NOW)[:4])
    with pytest.raises(synth.SynthesisError) as stale:
        await synth.split_for(None, settings, NOW, leg_ids=[f"fx-{i}|Match Odds|HOME" for i in range(5)], session_stake=D("100"), book="parimatch")
    assert (stale.value.reason, stale.value.status_code, stale.value.detail["missing"]) == ("STALE_LEGS", 409, ["fx-4|Match Odds|HOME"])
    with pytest.raises(synth.SynthesisError) as book:
        await synth.split_for(None, settings, NOW, leg_ids=["x"], session_stake=D("100"), book="stake")
    assert book.value.reason == "BOOK_NOT_SUPPORTED"
    uid = await _user(sessions)
    async with sessions() as db:
        with pytest.raises(rms.RealMoneyError) as missing:
            await hes.confirm_ticket_placement(db, None, settings, uid, "PM-SPLIT-A-NONE", "CODE1", NOW)
        assert missing.value.status == 404


# ================================================================ Telegram
@pytest.mark.asyncio
async def test_telegram_synthesize_then_split_and_today(sessions: async_sessionmaker[AsyncSession], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    use_board(monkeypatch, board())
    redis = FakeRedis()
    bad = await twin.synthesize(redis, settings, NOW, 42, ["250"])  # type: ignore[arg-type]
    assert bad.outcome == "BAD_ARGUMENT" and "/synthesize 250 20" in bad.text
    reply = await twin.synthesize(redis, settings, NOW, 42, ["250", "20"])  # type: ignore[arg-type]
    assert reply.outcome == "SYNTHESIZED" and reply.detail["reached_target"] is True and "Street-smart slip" in reply.text and "You place it yourself" in reply.text
    kept = json.loads(redis.data[f"{SentinelKeys(settings).prefix}:synth:last:42"])
    assert kept["book"] == "parimatch" and kept["stake"] == "250" and len(kept["leg_ids"]) == reply.detail["legs"]
    split = await twin.split(redis, settings, NOW, 42, [])  # type: ignore[arg-type]
    assert split.outcome == "SPLIT" and "Slip A · Fortress Anchor Slip" in split.text and "Record the three tickets in BetDoc" in split.text
    nothing = await twin.split(FakeRedis(), settings, NOW, 7, [])  # type: ignore[arg-type]
    assert nothing.outcome == "EMPTY"
    today = await twin.today(sessions, None, settings, NOW)
    assert today.outcome == "TODAY" and "Market health" in today.text and "Developed for Amit Ashok Kumar Patnaik" in today.text
    from app.services import sentinel_commands as commands

    assert {"/today", "/synthesize", "/split"} <= set(commands.COMMANDS) and "/synthesize &lt;stake&gt; &lt;odds&gt;" in commands.HELP


# ================================================================ the API
@pytest.mark.asyncio
async def test_the_endpoints_end_to_end(sessions: async_sessionmaker[AsyncSession], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    use_board(monkeypatch, board(now))
    uid = await _user(sessions)
    async with sessions() as db:
        user = await db.get(User, uid)

    async def db_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(synthesizer_router, prefix="/api/v1")
    app.include_router(real_money_router, prefix="/api/v1")
    app.dependency_overrides.update({get_db: db_override, get_current_user: lambda: user, get_current_admin: lambda: user, get_settings: lambda: settings})
    from app.api.v1.cfo_execution import get_session_factory

    app.dependency_overrides[get_session_factory] = lambda: sessions
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        h = await client.get("/api/v1/synthesizer/market-health")
        assert h.status_code == 200 and h.headers["X-Engineered-For"] == DEVELOPER and h.json()["advisory_tier"] in ("REST_DAY", "SELECTIVE", "PRIME_HARVEST")
        bad = await client.post("/api/v1/synthesizer/synthesize-street-smart", json={"target_stake_inr": 500, "target_return_inr": 400})
        assert bad.status_code == 422
        syn = await client.post("/api/v1/synthesizer/synthesize-street-smart", json={"target_stake_inr": 250, "target_return_inr": 5000, "max_legs": 15})
        body = syn.json()
        assert syn.status_code == 200 and body["reached_target"] is True and body["is_multi_sport"] is True and body["legs"][0]["recipe_rationale"]
        ids = [x["leg_id"] for x in body["legs"]]
        split = await client.post("/api/v1/synthesizer/split-system-bets", json={"leg_ids": ids, "total_session_stake_inr": 250})
        assert split.status_code == 200 and split.json()["slip_c_moonshot"]["leg_count"] == len(ids) and split.json()["slip_a_fortress"]["stake_inr"] == "100.00"
        rec = await client.post("/api/v1/synthesizer/record-system-tickets", json={"leg_ids": ids, "total_session_stake_inr": 250})
        assert rec.status_code == 201 and len(rec.json()["ticket_references"]) == 3
        listed = (await client.get("/api/v1/synthesizer/tickets")).json()["tickets"]
        assert {t["ticket_reference"] for t in listed} == set(rec.json()["ticket_references"]) and all(t["status"] == "PENDING_PLACEMENT" for t in listed)
        ref = rec.json()["ticket_references"][0]
        conf = await client.post(f"/api/v1/real-money/tickets/{ref}/confirm-handcrafted", json={"booking_code": "PM88XY"})
        assert conf.status_code == 200 and conf.json()["ticket"]["status"] == "PLACED" and conf.json()["ticket"]["placed_stake"] == 100.0
        gone = await client.post(f"/api/v1/synthesizer/tickets/{rec.json()['ticket_references'][2]}/cancel")
        assert gone.status_code == 200 and gone.json()["status"] == "CANCELLED"
        dry = await client.post("/api/v1/synthesizer/market-health/dispatch", params={"dry_run": True})
        assert dry.status_code == 200 and dry.json()["dry_run"] is True and dry.json()["sent"] is False


@pytest.mark.asyncio
async def test_without_the_board_nothing_is_priced(settings: Settings) -> None:
    with pytest.raises(synth.SynthesisError) as down:
        await synth.synthesize_for(None, settings, NOW, stake=D("250"), target_return=D("5000"), book="parimatch")
    assert (down.value.reason, down.value.status_code) == ("BOARD_UNAVAILABLE", 503)


# ================================================================ the migration and the page
def test_the_migration_creates_the_day_table_and_the_ticket_legs() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "backend" / "alembic.ini")))
    revision = script.get_revision("g115a9c3d4e2")
    assert revision is not None and revision.down_revision == "f114a8b7c2d1" and script.get_heads() == ["g115a9c3d4e2"]
    source = (ROOT / "backend" / "alembic" / "versions" / "g115a9c3d4e2_create_daily_market_health_table.py").read_text(encoding="utf-8")
    for text in ("ck_daily_market_health_snapshots_tier_valid", "ck_daily_market_health_snapshots_score_range", "uq_daily_market_health_snapshots_snapshot_date",
                 "op.add_column('real_bet_tickets', sa.Column('legs'", "wait to be placed"):
        assert text in source, text


def test_the_synthesizer_component_and_api_remain() -> None:
    src = ROOT / "frontend" / "src"
    component = (src / "components" / "builder" / "StreetSmartSynthesizer.tsx").read_text(encoding="utf-8")
    for text in ("TODAR MAL · market health", "Synthesize the street-smart slip", "Split into Fortress, Growth &amp; Moonshot", "Record the 3 tickets (nothing is locked)",
                 "Lock stake &amp; confirm", "The four outcomes (the tickets are nested)", "<InPlayDesk"):
        assert text in component, text
    builder = (src / "components" / "builder" / "BetBuilder.tsx").read_text(encoding="utf-8")
    assert "StreetSmartSynthesizer" not in builder  # Group 131: 5(d) left the builder; the component and its API remain
    lib = (src / "lib" / "synthesizer.ts").read_text(encoding="utf-8")
    for text in ("/synthesizer/market-health", "/synthesizer/synthesize-street-smart", "/synthesizer/split-system-bets", "/synthesizer/record-system-tickets",
                 "/confirm-handcrafted", '"real_money:system_tickets"'):
        assert text in lib, text


def test_unused_fixture_names_stay_honest() -> None:
    """No sample slate: the brief's stock fixtures and fixed margin are not in the synthesizer or the day's score."""
    for path in ("app/services/street_smart_synthesizer.py", "app/services/market_health_service.py", "app/api/v1/synthesizer.py", "app/domain/street_smart.py"):
        source = (ROOT / "backend" / path).read_text(encoding="utf-8")
        for stock in ("SAMPLE_SLATE", "Valladolid", "Bumrah", "0.048", "fix_rm_01"):
            assert stock not in source, (path, stock)
