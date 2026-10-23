"""Group 113: automated result settlement, the Section 194BA 30% TDS ledger and the P&L that follows.

* Results come only from a results feed (Football-Data.org, Sportmonks) paired strictly to the fixture a bet waits on,
  or from an explicit admin command; a fixture the grader cannot read, a two-way tie, an extra-time match without its
  regular-time score, an ambiguous or distant pairing stays pending: nothing is voided to make it go away.
* Every ledger settles from fixture_scores: the Oracle's placed bets, the real-money desk's tickets (bankroll, cash
  ledger, 30% TDS and its tax-ledger row, half-won tickets included) and the exchange orders (the Arena's payout rule;
  TDS only for an order a real exchange accepted; never the real-money vault).
* Exact Decimal money: Gross = stake x odds; Net Winnings = max(0, Gross - stake); TDS = 30% of Net Winnings; Net
  Post-Tax = Gross - TDS.
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.adapters.sports_data.football_data import parse_matches
from app.adapters.sports_data.sportsmonks import parse_fixtures
from app.api.v1 import omni_admin
from app.core.config import Settings, get_settings
from app.domain.live_context import ProviderFixture
from app.domain.oracle.markets import LegResult
from app.domain.result_settlement import Waiting, canonical_leg, grade, match_result
from app.domain.wagering.tax import financial_year, section_115bbj
from app.models import BetLedger, ExchangeAccount, User
from app.models.cfo_vault import MarketResult
from app.models.odds import OddsSnapshot
from app.models.real_money import RealBankroll, RealBetTicket, RealCashTransaction
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.user_bets_ledger import FixtureScore, ScoreStatus, UserPlacedBet, UserPlacedLeg
from app.services import live_result_settlement_service as settlement
from app.services import real_money_service as rms
from app.workers import settlement_runner

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)  # finished well before NOW
TABLES = (User, ExchangeAccount, BetLedger, RealBankroll, RealBetTicket, RealCashTransaction, TaxLedgerEntry, FixtureScore, UserPlacedBet,
          UserPlacedLeg, OddsSnapshot, MarketResult)


def _settings(**overrides: Any) -> Settings:
    base = {"FOOTBALL_DATA_ORG_API_KEY": SecretStr("fd-token"), "SPORTSMONKS_API_KEY": SecretStr(""), "FOOTBALL_DATA_COMPETITIONS": {"PL": "soccer_epl"}}
    return get_settings().model_copy(update={**base, **overrides})


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    md = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _match(fid: int, home: str, away: str, kickoff: datetime, *, status: str = "FINISHED", full: tuple[int, int] | None = (2, 1),
           duration: str = "REGULAR", regular: tuple[int, int] | None = None) -> dict[str, Any]:
    score: dict[str, Any] = {"duration": duration, "fullTime": {"home": None if full is None else full[0], "away": None if full is None else full[1]}}
    if regular is not None:
        score["regularTime"] = {"home": regular[0], "away": regular[1]}
    return {"id": fid, "utcDate": kickoff.isoformat().replace("+00:00", "Z"), "status": status, "competition": {"code": "PL", "name": "Premier League"},
            "homeTeam": {"name": f"{home} FC", "shortName": home}, "awayTeam": {"name": f"{away} FC", "shortName": away}, "score": score}


FEED = {"matches": [
    _match(1, "Arsenal", "Chelsea", KICKOFF, full=(2, 1)),
    _match(2, "Everton", "Fulham", KICKOFF, full=(1, 1)),
    _match(3, "Brighton", "Wolves", KICKOFF, full=(2, 2), duration="PENALTY_SHOOTOUT"),  # past 90 minutes, no regular-time score
]}


async def _seed(sessions: async_sessionmaker[AsyncSession], *, exchange: str = "parimatch") -> dict[str, Any]:
    """A user with: a real-money ticket on Arsenal (Chelsea at home? no: Arsenal v Chelsea), exchange orders on the same
    match and on Everton v Fulham, an Oracle bet, and the price history that names the orders' Odds API event ids."""
    ids: dict[str, Any] = {"user": uuid.uuid4(), "account": uuid.uuid4()}
    async with sessions() as db:
        db.add(User(id=ids["user"], username="amit", hashed_password="x"))
        db.add(ExchangeAccount(id=ids["account"], user_id=ids["user"], exchange_name=exchange, api_key_encrypted="k", api_secret_encrypted="s"))
        for event, home, away in (("ev-ars-che", "Arsenal", "Chelsea"), ("ev-eve-ful", "Everton", "Fulham"), ("ev-bri-wol", "Brighton", "Wolves")):
            db.add(OddsSnapshot(match_id=event, sport_key="soccer_epl", commence_time=KICKOFF, home_team=home, away_team=away, bookmaker="pinnacle",
                                market_type="h2h", selection=home, odds=2.0, timestamp=KICKOFF - timedelta(hours=2), bookmaker_last_update=KICKOFF - timedelta(hours=2)))
        orders = {
            "home_win": ("ev-ars-che", "Match Odds", "HOME", "2.0000", "1000.0000"),
            "over": ("ev-ars-che", "Total Goals", "Over 2.5", "1.8500", "2000.0000"),
            "away_lose": ("ev-ars-che", "Match Odds", "Chelsea", "3.8000", "500.0000"),
            "quarter": ("ev-eve-ful", "Asian Handicap -0.25", "HOME", "1.9000", "400.0000"),  # 1-1: half lost
            "dnb": ("ev-eve-ful", "Draw No Bet", "Everton", "1.5000", "300.0000"),  # level: void
            "unreadable": ("ev-ars-che", "Correct Score", "2-1", "9.0000", "100.0000"),
            "extra_time": ("ev-bri-wol", "Match Odds", "HOME", "2.2000", "100.0000"),
        }
        for name, (event, market, selection, odds, stake) in orders.items():
            ids[name] = uuid.uuid4()
            db.add(BetLedger(id=ids[name], idempotency_key=f"k-{name}", exchange_account_id=ids["account"], match_id=event, market_type=market, selection=selection,
                             odds=Decimal(odds), stake=Decimal(stake), true_probability=Decimal("0.5"), status="ACCEPTED", placed_at=KICKOFF - timedelta(hours=3)))
        db.add(RealBankroll(user_id=ids["user"], cash_balance=Decimal("50000.00"), locked_exposure=Decimal("10000.00"), daily_starting_balance=Decimal("60000.00")))
        db.add(RealBetTicket(user_id=ids["user"], ticket_reference="PM-REAL-G113-1", sport="soccer", league="EPL", match_id="fx-ars-che", event_name="Arsenal vs Chelsea",
                             market_type="Match Odds", selection="Arsenal", recommended_stake=Decimal("10000.00"), placed_stake=Decimal("10000.00"),
                             target_odds=Decimal("2.10"), placed_odds=Decimal("2.10"), minimum_profitable_odds=Decimal("1.90"), true_probability=0.52, model_edge_pct=9.2,
                             deep_link_url="https://example.test", clipboard_payload="slip", status="PLACED", placed_at=KICKOFF - timedelta(hours=1)))
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=ids["user"], source="ASHOKA", bookmaker="PARIMATCH", structure="SINGLE", stake_inr=Decimal("500.00"),
                            placed_at=KICKOFF - timedelta(hours=2), status="PENDING")
        ids["oracle_bet"] = bet.id
        db.add(bet)
        db.add(UserPlacedLeg(bet_id=bet.id, position=0, fixture_id="canon-ars-che", home="Arsenal", away="Chelsea", sport_key="soccer_epl", kickoff=KICKOFF,
                             market="Match Odds", selection="HOME", odds=Decimal("2.10"), result="PENDING"))
        await db.commit()
    return ids


def _feed(payload: dict[str, Any] = FEED) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "fd-token" not in str(request.url)
        return httpx.Response(200, json=payload) if request.url.host == "api.football-data.org" else httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def events(monkeypatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def publish(_redis: Any, event: dict[str, Any]) -> None:
        sent.append(event)

    monkeypatch.setattr(settlement, "publish_event", publish)
    return sent


# ================================================================ the money: the brief's own examples
@pytest.mark.parametrize(("stake", "odds", "gross", "tds", "net"), [
    ("1000.00", "2.00", "2000.00", "300.00", "1700.00"),
    ("2000.00", "1.85", "3700.00", "510.00", "3190.00"),
    ("10000.00", "2.10", "21000.00", "3300.00", "17700.00"),
])
def test_the_tds_formula_is_exact(stake: str, odds: str, gross: str, tds: str, net: str) -> None:
    b = section_115bbj(Decimal(stake) * Decimal(odds), Decimal(stake))
    assert (b.gross_payout, b.net_winnings, b.tds, b.net_post_tax_payout) == (Decimal(gross), Decimal(gross) - Decimal(stake), Decimal(tds), Decimal(net))
    assert b.tds_rate == Decimal("0.30")


# ================================================================ reading a bet
@pytest.mark.parametrize(("market", "selection", "expected"), [
    ("Match Odds", "HOME", ("Match Odds", "HOME")), ("Match Odds", "Arsenal", ("Match Odds", "HOME")), ("1X2", "Draw", ("Match Odds", "DRAW")),
    ("Total Goals", "Over 2.5", ("Totals 2.5", "OVER")), ("Over/Under 2.5", "Under", ("Totals 2.5", "UNDER")),
    ("Asian Handicap", "Chelsea +1.5", ("Asian Handicap -1.5", "AWAY")), ("Asian Handicap -0.25", "HOME", ("Asian Handicap -0.25", "HOME")),
    ("Draw No Bet", "Chelsea", ("Draw No Bet", "AWAY")), ("BTTS", "Yes", ("BTTS", "YES")),
    ("Asian Handicap -0.5", "Arsenal +0.5", None),  # the market and the selection disagree on the line
    ("Match Odds", "Spurs", None),  # names neither side
    ("Correct Score", "2-1", None), ("Totals 2.3", "OVER", None),  # a market the grader does not price; not a quarter line
])
def test_a_bet_is_read_or_left_alone(market: str, selection: str, expected: tuple[str, str] | None) -> None:
    leg = canonical_leg(market, selection, "Arsenal", "Chelsea")
    assert (None if leg is None else (leg.ref.key, leg.selection)) == expected


def test_grading_follows_the_books_rules() -> None:
    def g(market: str, selection: str, h: int | None, a: int | None, *, status: str = "FINAL", sport: str = "soccer_epl", kickoff: datetime = KICKOFF) -> LegResult | None:
        leg = canonical_leg(market, selection, "A", "B")
        assert leg is not None
        return grade(leg, status=status, home_goals=h, away_goals=a, sport_key=sport, kickoff=kickoff, now=NOW)

    assert g("Match Odds", "HOME", 2, 1) is LegResult.WON and g("Match Odds", "AWAY", 2, 1) is LegResult.LOST
    assert g("Totals 2", "OVER", 1, 1) is LegResult.VOID and g("Asian Handicap -0.25", "HOME", 1, 1) is LegResult.HALF_LOST
    assert g("Asian Handicap -0.75", "HOME", 2, 1) is LegResult.HALF_WON
    assert g("Match Odds", "HOME", 100, 100, sport="cricket_t20_blast") is None  # a two-way tie is the book's call, not a machine's
    assert g("Match Odds", "HOME", None, None, status="ABANDONED") is LegResult.VOID
    assert g("Match Odds", "HOME", None, None, status="POSTPONED", kickoff=NOW - timedelta(hours=10)) is None  # inside the 48h grace
    assert g("Match Odds", "HOME", None, None, status="POSTPONED", kickoff=NOW - timedelta(hours=60)) is LegResult.VOID


def test_a_result_pairs_one_fixture_strictly() -> None:
    k = KICKOFF
    def p(pid: str, home: str, away: str, kickoff: datetime = k) -> ProviderFixture:
        return ProviderFixture("football-data.org", pid, "soccer_epl", "PL", kickoff, (f"{home} FC", home), (f"{away} FC", away), "FINISHED", regulation=(2, 1))

    waiting = Waiting("x", "soccer", "Arsenal", "Chelsea", k)
    assert match_result(waiting, [p("1", "Arsenal", "Chelsea")], cutoff=0.85, window=timedelta(hours=6)) == (p("1", "Arsenal", "Chelsea"), False)
    assert match_result(waiting, [p("1", "Chelsea", "Arsenal")], cutoff=0.85, window=timedelta(hours=6))[1] is True  # type: ignore[index]
    assert match_result(waiting, [p("1", "Arsenal", "Chelsea", k + timedelta(days=2))], cutoff=0.85, window=timedelta(hours=6)) is None
    assert match_result(waiting, [p("1", "Arsenal", "Leeds United")], cutoff=0.85, window=timedelta(hours=6)) is None  # one side is not enough
    assert match_result(waiting, [p("1", "Arsenal", "Chelsea"), p("2", "Arsenal", "Chelsea", k + timedelta(hours=1))], cutoff=0.85, window=timedelta(hours=6)) is None
    ticket = Waiting("t", "soccer", "Arsenal", "Chelsea", None, k - timedelta(hours=6), k + timedelta(days=4))
    assert match_result(ticket, [p("1", "Arsenal", "Chelsea", k + timedelta(days=1))], cutoff=0.85, window=timedelta(hours=6)) is not None


def test_the_feeds_give_the_score_after_ninety_minutes() -> None:
    fixtures = {f.provider_id: f for f in parse_matches({"matches": [
        _match(1, "A", "B", KICKOFF, full=(3, 2), duration="EXTRA_TIME", regular=(2, 2)),
        _match(2, "C", "D", KICKOFF, full=(1, 1), duration="PENALTY_SHOOTOUT"),
        _match(3, "E", "F", KICKOFF, full=(0, 1)),
        _match(4, "G", "H", KICKOFF, status="TIMED", full=None),
    ]}, {"PL": "soccer_epl"})}
    assert fixtures["1"].regulation == (2, 2) and fixtures["1"].home_score == 3  # the board shows 3-2; bets settle on 2-2
    assert fixtures["2"].regulation is None and fixtures["3"].regulation == (0, 1) and fixtures["4"].regulation is None
    sm = {"data": [{"id": 9, "league_id": 271, "starting_at_timestamp": int(KICKOFF.timestamp()), "state": {"developer_name": state},
                    "participants": [{"name": "X", "meta": {"location": "home"}}, {"name": "Y", "meta": {"location": "away"}}],
                    "scores": [{"description": "CURRENT", "score": {"goals": 2, "participant": "home"}}, {"description": "CURRENT", "score": {"goals": 1, "participant": "away"}}]}
                   for state in ("FT",)]}
    assert parse_fixtures(sm, {271: "soccer_denmark_superliga"})[0].regulation == (2, 1)
    sm["data"][0]["state"] = {"developer_name": "AET"}
    assert parse_fixtures(sm, {271: "soccer_denmark_superliga"})[0].regulation is None


# ================================================================ the sweep, end to end
async def test_one_sweep_settles_every_ledger_exactly(sessions, events) -> None:
    ids = await _seed(sessions)
    async with httpx.AsyncClient(transport=_feed()) as http:
        report = await settlement.sweep(sessions, http, None, _settings(), now=NOW)
    assert report["waiting_fixtures"] == 5 and report["results"]["unpaired"] == 1  # Brighton v Wolves went to penalties: left for an admin
    async with sessions() as db:
        scores = {s.fixture_id: s for s in (await db.execute(select(FixtureScore))).scalars()}
        orders = {o.id: o for o in (await db.execute(select(BetLedger))).scalars()}
        ticket = (await db.execute(select(RealBetTicket))).scalar_one()
        bankroll = (await db.execute(select(RealBankroll))).scalar_one()
        taxes = list((await db.execute(select(TaxLedgerEntry))).scalars())
        cash = list((await db.execute(select(RealCashTransaction).order_by(RealCashTransaction.balance_before))).scalars())
        oracle = await db.get(UserPlacedBet, ids["oracle_bet"])
    assert {k: (s.home_goals, s.away_goals, s.status, s.source) for k, s in scores.items()} == {
        "ev-ars-che": (2, 1, "FINAL", "football-data.org"), "ev-eve-ful": (1, 1, "FINAL", "football-data.org"),
        "fx-ars-che": (2, 1, "FINAL", "football-data.org"), "canon-ars-che": (2, 1, "FINAL", "football-data.org")}

    # exchange orders: the Arena's payout rule, a mock exchange taxes nothing
    expect = {"home_win": ("WON", "2000.0000"), "over": ("WON", "3700.0000"), "away_lose": ("LOST", "0.0000"), "quarter": ("HALF_LOST", "200.0000"),
              "dnb": ("VOID", "300.0000"), "unreadable": ("ACCEPTED", None), "extra_time": ("ACCEPTED", None)}
    for name, (status, payout) in expect.items():
        order = orders[ids[name]]
        assert (order.status, None if order.payout is None else f"{Decimal(order.payout):.4f}") == (status, payout), name
        assert (order.resolved_at is not None) == (status != "ACCEPTED"), name
    assert report["exchange_orders"]["pending_ungradeable"] == 1 and report["exchange_orders"]["tax_entries"] == 0

    # the real-money ticket: Rs 10,000 at 2.10 on Arsenal, 2-1
    assert (ticket.status, ticket.gross_return, ticket.tds_deducted, ticket.settlement_payout, ticket.net_profit) == (
        "WON", Decimal("21000.00"), Decimal("3300.00"), Decimal("17700.00"), Decimal("7700.00"))
    assert (bankroll.cash_balance, bankroll.locked_exposure, bankroll.realized_pnl, bankroll.gross_winnings, bankroll.total_tds_deducted) == (
        Decimal("67700.00"), Decimal("0.00"), Decimal("7700.00"), Decimal("11000.00"), Decimal("3300.00"))
    (tax,) = taxes
    assert (tax.mode, tax.section, tax.gross_payout, tax.stake, tax.net_winnings, tax.tds_amount, tax.net_post_tax_payout, tax.financial_year) == (
        "LIVE", "115BBJ", Decimal("21000.00"), Decimal("10000.00"), Decimal("11000.00"), Decimal("3300.00"), Decimal("17700.00"), financial_year(NOW))
    assert [(c.transaction_type, c.amount) for c in cash] == [("BET_PAYOUT_CREDIT", Decimal("21000.00")), ("TDS_WITHHOLDING_DEBIT", Decimal("3300.00"))]
    assert oracle is not None and oracle.status == "WON" and report["oracle"]["bets_settled"] == 1
    assert report["settled_total"] == 1 + 1 + 5 and report["tds_withheld"] == "3300.00"
    (event,) = events
    assert event["type"] == "settlement" and event["settled"] == 7 and event["tds_withheld"] == "3300.00"

    # a second sweep finds nothing new: no second settlement, no second tax row, no second event
    async with httpx.AsyncClient(transport=_feed()) as http:
        again = await settlement.sweep(sessions, http, None, _settings(), now=NOW)
    async with sessions() as db:
        assert len(list((await db.execute(select(TaxLedgerEntry))).scalars())) == 1
    assert again["settled_total"] == 0 and len(events) == 1


async def test_a_dry_run_writes_nothing(sessions, events) -> None:
    await _seed(sessions)
    async with httpx.AsyncClient(transport=_feed()) as http:
        report = await settlement.sweep(sessions, http, None, _settings(), now=NOW, dry_run=True)
    assert report["dry_run"] and report["real_money_tickets"]["settled"] == 1 and report["exchange_orders"]["settled"] == 5
    assert report["oracle"] == {"legs_settled": 1, "bets_settled": None}
    async with sessions() as db:
        assert list((await db.execute(select(FixtureScore))).scalars()) == [] and list((await db.execute(select(TaxLedgerEntry))).scalars()) == []
        assert {o.status for o in (await db.execute(select(BetLedger))).scalars()} == {"ACCEPTED"}
        assert (await db.execute(select(RealBetTicket))).scalar_one().status == "PLACED"
    assert events == []


async def test_a_real_exchange_order_records_its_tds_and_never_the_vault(sessions, events, monkeypatch) -> None:
    monkeypatch.setattr(settlement, "REAL_EXCHANGES", {"parimatch": object})
    ids = await _seed(sessions)
    async with httpx.AsyncClient(transport=_feed()) as http:
        report = await settlement.sweep(sessions, http, None, _settings(), now=NOW)
    async with sessions() as db:
        taxes = sorted((t.stake, t.gross_payout, t.tds_amount, t.net_post_tax_payout) for t in (await db.execute(select(TaxLedgerEntry))).scalars())
        bankroll = (await db.execute(select(RealBankroll))).scalar_one()
    # the ticket's 10,000 @ 2.10, the orders' 1,000 @ 2.00 and 2,000 @ 1.85 (the brief's examples), nothing for a loss, a push or a void
    assert taxes == sorted([(Decimal("10000.00"), Decimal("21000.00"), Decimal("3300.00"), Decimal("17700.00")),
                            (Decimal("1000.00"), Decimal("2000.00"), Decimal("300.00"), Decimal("1700.00")),
                            (Decimal("2000.00"), Decimal("3700.00"), Decimal("510.00"), Decimal("3190.00"))])
    assert report["exchange_orders"]["tds_withheld"] == "810.00" and report["tds_withheld"] == "4110.00"
    assert bankroll.cash_balance == Decimal("67700.00")  # only the ticket's payout: an exchange order's stake was never locked in the vault
    assert ids  # noqa: S101 - fixture used


async def test_an_admin_result_is_explicit_and_named(sessions, events) -> None:
    await _seed(sessions)
    with pytest.raises(settlement.SettlementError):
        await settlement.record_admin_result(sessions, "unknown-fixture", status=ScoreStatus.FINAL, home_goals=1, away_goals=0)
    with pytest.raises(settlement.SettlementError):
        await settlement.record_admin_result(sessions, "ev-bri-wol", status=ScoreStatus.FINAL, home_goals=None, away_goals=1)
    manual = await settlement.record_admin_result(sessions, "ev-bri-wol", status=ScoreStatus.FINAL, home_goals=1, away_goals=1)
    assert (manual["home"], manual["away"], manual["home_goals"], manual["away_goals"], manual["replaced"]) == ("Brighton", "Wolves", 1, 1, None)
    report = await settlement.sweep(sessions, None, None, _settings(), now=NOW, fetch=False)
    rows = {r["match_id"]: r for r in report["exchange_orders"]["rows"] if "match_id" in r}
    assert rows["ev-bri-wol"]["result"] == "LOST" and rows["ev-bri-wol"]["source"] == "admin"
    async with sessions() as db:
        assert (await db.get(FixtureScore, "ev-bri-wol")).source == "admin"  # type: ignore[union-attr]


async def test_an_admin_score_is_never_overwritten_by_a_feed(sessions, events) -> None:
    await _seed(sessions)
    await settlement.record_admin_result(sessions, "ev-ars-che", status=ScoreStatus.FINAL, home_goals=0, away_goals=0)
    async with httpx.AsyncClient(transport=_feed()) as http:
        await settlement.sweep(sessions, http, None, _settings(), now=NOW)
    async with sessions() as db:
        row = await db.get(FixtureScore, "ev-ars-che")
    assert row is not None and (row.home_goals, row.away_goals, row.source) == (0, 0, "admin")


# ================================================================ the real-money desk's own fixes
async def test_a_half_won_ticket_ledgers_the_tds_of_its_winning_half(sessions) -> None:
    ids = await _seed(sessions)
    async with sessions() as db:
        ticket = await rms.settle_ticket(db, _settings(), ids["user"], "PM-REAL-G113-1", outcome="HALF_WON")
        await db.commit()
        tax = (await db.execute(select(TaxLedgerEntry))).scalar_one()
        cash = [(c.transaction_type, c.amount) for c in (await db.execute(select(RealCashTransaction).order_by(RealCashTransaction.balance_before))).scalars()]
    # 5,000 wins at 2.10 (10,500, net 5,500, TDS 1,650) and 5,000 comes back: gross 15,500, paid 13,850
    assert (ticket.gross_return, ticket.tds_deducted, ticket.settlement_payout, ticket.net_profit) == (
        Decimal("15500.00"), Decimal("1650.00"), Decimal("13850.00"), Decimal("3850.00"))
    assert (tax.gross_payout, tax.stake, tax.net_winnings, tax.tds_amount, tax.net_post_tax_payout) == (
        Decimal("10500.00"), Decimal("5000.00"), Decimal("5500.00"), Decimal("1650.00"), Decimal("8850.00"))
    assert cash == [("BET_PAYOUT_CREDIT", Decimal("15500.00")), ("TDS_WITHHOLDING_DEBIT", Decimal("1650.00"))]


async def test_an_unreadable_scorecard_is_refused_never_voided(sessions) -> None:
    ids = await _seed(sessions)
    async with sessions() as db:
        with pytest.raises(rms.RealMoneyError) as info:
            await rms.settle_ticket(db, _settings(), ids["user"], "PM-REAL-G113-1", scorecard={"nonsense": True})
        await db.rollback()
        assert (await db.execute(select(RealBetTicket))).scalar_one().status == "PLACED"
    assert info.value.code == "UNGRADEABLE"


def test_the_desks_task_reads_scores_by_their_real_key() -> None:
    source = (ROOT / "backend" / "app" / "workers" / "real_money_tasks.py").read_text(encoding="utf-8")
    assert "settle_real_tickets" in source and "score_data" not in source and "FixtureScore.match_id" not in source


# ================================================================ the runner, the endpoint, the beat, the browser
def test_the_runner_validates_its_commands() -> None:
    assert settlement_runner.parse_args(["--sweep", "--dry-run"]).dry_run
    args = settlement_runner.parse_args(["--match-id", "ev-1", "--home-score", "2", "--away-score", "1"])
    assert (args.match_id, args.home_score, args.away_score, args.status) == ("ev-1", 2, 1, "FINAL")
    assert settlement_runner.parse_args(["--match-id", "ev-1", "--status", "ABANDONED"]).home_score is None
    for bad in ([], ["--match-id", "ev-1"], ["--match-id", "ev-1", "--home-score", "1", "--away-score", "0", "--dry-run"]):
        with pytest.raises(SystemExit):
            settlement_runner.parse_args(bad)


async def test_the_admin_settlement_is_token_protected(monkeypatch) -> None:
    calls: list[Any] = []

    async def sweep(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("sweep", kwargs))
        return {"settled_total": 0}

    async def admin(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("admin", kwargs))
        return {"fixture_id": "ev-1"}

    monkeypatch.setattr(omni_admin.settlement, "sweep", sweep)
    monkeypatch.setattr(omni_admin.settlement, "record_admin_result", admin)
    settings = get_settings()
    app = FastAPI()
    app.include_router(omni_admin.router, prefix="/api/v1")
    app.state.settings = settings
    app.state.fleet_deps = SimpleNamespace(session_factory=None, http=None, redis=None, settings=settings)
    headers = {settings.omni_admin_header: settings.omni_admin_token.get_secret_value()}  # type: ignore[union-attr]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/v1/admin/omni/settle-results")).status_code in (401, 503)
        assert (await client.post("/api/v1/admin/omni/settle-results", headers=headers, json={"dry_run": True})).status_code == 200
        assert calls[-1] == ("sweep", {"fetch": True, "dry_run": True})
        bad = {"result": {"match_id": "ev-1", "status": "FINAL", "home_score": 1}}
        assert (await client.post("/api/v1/admin/omni/settle-results", headers=headers, json=bad)).status_code == 422
        both = {"dry_run": True, "result": {"match_id": "ev-1", "home_score": 1, "away_score": 0}}
        assert (await client.post("/api/v1/admin/omni/settle-results", headers=headers, json=both)).status_code == 422
        good = {"result": {"match_id": "ev-1", "home_score": 2, "away_score": 1}}
        res = await client.post("/api/v1/admin/omni/settle-results", headers=headers, json=good)
        assert res.status_code == 200 and res.json()["manual"] == {"fixture_id": "ev-1"} and calls[-2][0] == "admin"


def test_the_sweep_is_scheduled_and_the_browser_refreshes() -> None:
    from app.core.celery_app import celery_app  # noqa: PLC0415

    entry = celery_app.conf.beat_schedule["settlement-sweep"]
    assert entry["task"] == "settlement.sweep" and "app.workers.settlement_tasks" in celery_app.conf.include
    realtime = (ROOT / "frontend" / "src" / "services" / "realtime.ts").read_text(encoding="utf-8")
    assert 'event.type === "settlement"' in realtime and '"real_money"' in realtime and '"oracle"' in realtime


def test_no_fabricated_settlement_path() -> None:
    service = (ROOT / "backend" / "app" / "services" / "live_result_settlement_service.py").read_text(encoding="utf-8")
    desk = (ROOT / "backend" / "app" / "services" / "real_money_service.py").read_text(encoding="utf-8")
    assert "RealBankroll" not in service  # the exchange ledger never touches the real-money vault
    assert "return RealTicketStatus.VOID.value" not in desk  # an unreadable scorecard is refused, not refunded
    assert "REAL_EXCHANGES" in service and "MockExchangeAdapter" in (ROOT / "backend" / "app" / "exchanges" / "factory.py").read_text(encoding="utf-8")
