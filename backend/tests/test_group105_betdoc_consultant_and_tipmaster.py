"""Group 105: the BetDoc Consultant (ASHVATTHAMA: the Doctor, the Lawyer and the Consultant, point b.6) and TipMaster
merged into it (point b.13b). Developed for Amit Ashok Kumar Patnaik.

The specialists are pure and checked directly: the Doctor's pathologies (each from a pillar or a measurement, missing
evidence listed as not examined), the Lawyer's rules (raised only for the legs they govern, a book's clause or maximum
payout only when the user recorded it), the capital arithmetic (Section 115BBJ tax, the stop-loss floor, USD only at a
fresh rate), TipMaster's classes (unmeasured shares fire nothing), the verdict and the seal. End to end, on SQLite and
PostgreSQL with the live market in Redis: a consultation prescribes the auto-fix's quoted cure, records one sealed
verdict and no twin audit, the seal catches a tampered report, the prescription applies, measured public splits turn
into a public trap, a recorded payout cap rejects the slip, and ASHVATTHAMA's probe counts the verdicts.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.v1 import consultant as consultant_api
from app.core.config import Settings, get_settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.competitive_intel import live_consensus_math as lcm
from app.domain.consultant import diagnostics as dx
from app.models import User
from app.models.consultant import ConsultantVerdictModel, VerdictStatus
from app.models.digital_twin import TwinVettingAudit
from app.models.real_money import RealBetTicket
from app.models.tax_and_fx import CurrencyExchangeRate
from app.models.the_hive import LegendaryBot
from app.services.commander_supervisor import PROBES, ProbeContext
from tests.test_backtest_and_parlay import TABLES, _sqlite_metadata, app_for, make_user, pillars, redis, settings  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market

S = get_settings()
D = Decimal
SECRET = "k" * 40
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
EXTRA = [ConsultantVerdictModel.__table__, CurrencyExchangeRate.__table__, RealBetTicket.__table__]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [*TABLES, *EXTRA]
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        md = _sqlite_metadata()
        for table in EXTRA:
            table.to_metadata(md)
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
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


def _leg(leg_id: str, *, ev: float | None = 0.05, fair: float = 0.5, price: float = 2.1, sport: str = "soccer_epl", league: str = "EPL",
         market: str = "Match Odds", label: str | None = None) -> dict[str, Any]:
    return {"leg_id": leg_id, "fixture": leg_id.split("|", 1)[0], "label": label or leg_id.rsplit("|", 1)[-1], "ev": ev, "fair_probability": fair,
            "prices": {"1xbet": price}, "sport_key": sport, "league": league, "market": market}


# ================================================================ the Doctor (pure)
def test_the_doctor_diagnoses_only_what_was_measured() -> None:
    legs = [_leg("f1|Match Odds|HOME", ev=-0.08, fair=0.45, price=2.04, label="Everton"), _leg("f2|Match Odds|AWAY", label="Wolves")]
    statuses = ["PASS"] * 15
    statuses[2], statuses[3], statuses[11], statuses[14], statuses[8] = "ADVISORY", "FAIL", "FAIL", "FAIL", "UNVERIFIED"
    found, unexamined = dx.diagnose(pillars(statuses), legs, "1xbet", {"f1|Match Odds|HOME": 0.09, "f2|Match Odds|AWAY": 0.05},
                                    heavy_juice_min=0.075, payout=D("5000"), max_payout=D("4000"))
    codes = [(p.code.value, p.severity.value) for p in found]
    assert ("NEGATIVE_EV", "CRITICAL") in codes and ("INJURY_LINEUP_SHOCK", "CRITICAL") in codes and ("CORRELATED_SYNDROME", "CRITICAL") in codes
    assert ("PILLAR_VETO", "CRITICAL") in codes and ("BOOKMAKER_LIMIT_TRAP", "CRITICAL") in codes  # pillar 15, and the recorded payout cap
    assert ("HIGH_VIG_FEVER", "WARNING") in codes and ("FATIGUE_TRAVEL_PENALTY", "MILD") in codes  # a 9% margin; an advisory pillar
    assert [p.severity for p in found] == sorted((p.severity for p in found), key=["CRITICAL", "WARNING", "MILD"].index)  # most severe first
    negative = next(p for p in found if p.code is dx.DiseaseCode.NEGATIVE_EV)
    assert negative.legs == ("f1|Match Odds|HOME",) and "45.0%" in negative.diagnosis and f"{1 / 2.04:.1%}" in negative.diagnosis and "-8.0%" in negative.diagnosis
    assert next(p for p in found if p.code is dx.DiseaseCode.PILLAR_VETO).diagnosis.startswith("Pillar 15, never forget: reason 15")
    assert [u["number"] for u in unexamined] == [9]  # missing evidence is not examined, never healthy
    assert all(p.code is not dx.DiseaseCode.HIGH_VIG_FEVER or p.legs == ("f1|Match Odds|HOME",) for p in found)  # 5% is under the heavy-juice line

    same = [_leg("f1|Match Odds|HOME"), _leg("f1|Totals 2.5|OVER")]
    statuses = ["PASS"] * 15
    statuses[11] = "FAIL"
    corr = next(p for p in dx.diagnose(pillars(statuses), same, "1xbet", {}, heavy_juice_min=0.075, payout=None, max_payout=None)[0])
    assert corr.code is dx.DiseaseCode.CORRELATED_SYNDROME and set(corr.legs) == {"f1|Match Odds|HOME", "f1|Totals 2.5|OVER"}

    clean, nothing = dx.diagnose(pillars(["PASS"] * 15), legs[1:], "1xbet", {"f2|Match Odds|AWAY": 0.03}, heavy_juice_min=0.075, payout=D("100"), max_payout=None)
    assert clean == [] and nothing == []


# ================================================================ the Lawyer (pure)
def _policy(**over: Any) -> dx.LawyerPolicy:
    return dx.LawyerPolicy.from_settings(S.model_copy(update=over))


def test_the_lawyer_raises_only_the_rules_these_legs_fall_under() -> None:
    passed, rain = pillars(["PASS"] * 15), pillars(["PASS", "FAIL"] + ["PASS"] * 13)
    league = dx.counsel([_leg("f1|Match Odds|HOME")], "1xbet", passed, _policy(), payout=D("2100"))
    assert [c.rule.code for c in league] == ["MAX_PAYOUT"]  # a league single: no extra time, no void-leg question
    assert league[0].risk is dx.RiskLevel.LOW and "No maximum payout is recorded for 1xbet" in league[0].finding and league[0].reference is None

    cup = dx.counsel([_leg("f1|Match Odds|HOME", sport="soccer_fa_cup", league="FA Cup"), _leg("f2|Match Odds|AWAY")], "1xbet", passed, _policy(), payout=D("4000"))
    by = {c.rule.code: c for c in cup}
    assert by["REGULATION_TIME"].risk is dx.RiskLevel.MEDIUM and by["REGULATION_TIME"].legs == ("f1|Match Odds|HOME",)
    assert "VOID_LEG_IN_MULTIPLE" in by  # a double: a void leg counts at 1.00

    t20 = dx.counsel([_leg("c1|Match Odds|HOME", sport="cricket_ipl", league="IPL")], "parimatch", rain, _policy(), payout=None)
    overs = next(c for c in t20 if c.rule.code == "CRICKET_MINIMUM_OVERS")
    assert overs.risk is dx.RiskLevel.HIGH and "T20 needs at least 5 overs a side" in overs.finding and "weather pillar failed" in overs.finding
    odi = next(c for c in dx.counsel([_leg("c2|Match Odds|AWAY", sport="cricket_odi")], "parimatch", passed, _policy(), payout=None)
               if c.rule.code == "CRICKET_MINIMUM_OVERS")
    assert odi.risk is dx.RiskLevel.LOW and "at least 20 overs" in odi.finding
    assert not any(c.rule.code == "CRICKET_MINIMUM_OVERS" for c in dx.counsel([_leg("c3|Match Odds|HOME", sport="cricket_test_match")], "parimatch", passed,
                                                                             _policy(), payout=None))  # a Test's draw is a result

    lines = dx.counsel([_leg("f1|Totals 2.0|OVER", market="Totals 2.0", label="Over 2.0"), _leg("f2|Totals 2.5|UNDER", market="Totals 2.5"),
                        _leg("f3|Asian Handicap -0.25|HOME", market="Asian Handicap -0.25", label="Home -0.25")], "1xbet", passed, _policy(), payout=None)
    push = next(c for c in lines if c.rule.code == "LINE_PUSH")
    assert push.legs == ("f1|Totals 2.0|OVER", "f3|Asian Handicap -0.25|HOME") and "whole line" in push.finding and "quarter line" in push.finding

    error = dx.counsel([_leg("f1|Match Odds|HOME", ev=0.40, fair=0.5, price=2.8), _leg("t1|Match Odds|HOME", sport="tennis_atp_paris")], "1xbet", passed,
                       _policy(), payout=None)
    assert error[0].rule.code == "PALPABLE_ERROR" and error[0].risk is dx.RiskLevel.HIGH  # the riskiest first
    assert "TENNIS_RETIREMENT" in {c.rule.code for c in error}

    recorded = _policy(CONSULTANT_MAX_PAYOUT_INR={"1XBet": D("3000")}, CONSULTANT_RULE_REFERENCES={"1xbet": {"MAX_PAYOUT": "Rule 3.1 (read 2026-10-20)"}})
    over = dx.counsel([_leg("f1|Match Odds|HOME")], "1xbet", passed, recorded, payout=D("4000"))[0]
    assert over.risk is dx.RiskLevel.HIGH and "₹3,000" in over.finding and over.reference == "Rule 3.1 (read 2026-10-20)"
    under = dx.counsel([_leg("f1|Match Odds|HOME")], "1xbet", passed, recorded, payout=D("2000"))[0]
    assert under.risk is dx.RiskLevel.LOW and "inside" in under.finding
    assert set(dx.RULES) == {"REGULATION_TIME", "CRICKET_MINIMUM_OVERS", "TENNIS_RETIREMENT", "LINE_PUSH", "VOID_LEG_IN_MULTIPLE", "PALPABLE_ERROR", "MAX_PAYOUT"}


# ================================================================ the Consultant (pure)
def test_the_capital_view_is_the_house_formulas_and_usd_fails_closed() -> None:
    stop = slm.StopLossPolicy.from_settings(S)
    common: dict[str, Any] = {"bankroll": D("100000"), "stake": D("1000.00"), "odds": D("2.5"), "kelly_fraction": 0.01, "kelly_multiplier": 0.25,
                              "sizing": {"full_kelly": 0.06, "regime": "OPTIMAL", "drawdown": 0.0}, "stop_policy": stop, "tds_rate": D("0.30"),
                              "simulation": {"joint_probability": 0.44, "joint_ev": 0.1, "p_total_loss": 0.56, "var_95": -1.0, "cvar_95": -1.0}, "max_payout": None}
    view = dx.capital(**common, inr_per_usd=D("84.10"), usd_as_of="2026-10-22T09:00:00+00:00")
    assert view["payout_inr"] == "2500.00" and view["tax"]["net_winnings"] == 1500.0 and view["tax"]["tds"] == 450.0 and view["tax"]["net_post_tax_payout"] == 2050.0
    assert view["stake_usd"] == "11.89" and view["stake_pct_of_bankroll"] == 0.01 and view["expected_profit_inr"] == "100.00"
    pct = slm.recommended_pct(0.44, stop)
    assert view["stop_loss"]["pct"] == pct and view["stop_loss"]["floor_inr"] == str(slm.floor(D("1000.00"), pct)) and view["what_if"]["tds_rate"] == 0.30
    unpriced = dx.capital(**common, inr_per_usd=None, usd_as_of=None)
    assert unpriced["stake_usd"] is None and unpriced["fx"]["inr_per_usd"] is None  # no fresh rate: no USD figure, never a constant
    capped = dx.capital(**{**common, "max_payout": D("2000")}, inr_per_usd=None, usd_as_of=None)
    assert capped["payout_over_cap"] is True


# ================================================================ TipMaster (pure)
def test_tipmaster_compares_measured_shares_only() -> None:
    policy = lcm.ConsensusPolicy.from_settings(S)
    leg = _leg("f1|Match Odds|HOME", fair=0.40, price=2.2, label="Everton")  # EV -12%
    unmeasured = dx.leg_consensus(leg, "1xbet", tickets=None, money=None, opening=None, source=None, margin=0.05, policy=policy)
    assert unmeasured.divergence is dx.Divergence.NOT_MEASURED
    trap = dx.leg_consensus(leg, "1xbet", tickets=80.0, money=30.0, opening=None, source="feed-x", margin=0.05, policy=policy)
    assert trap.divergence is dx.Divergence.PUBLIC_TRAP and trap.as_dict()["ev_pct"] == pytest.approx(-12.0)
    value = _leg("f2|Match Odds|AWAY", fair=0.50, price=2.3, label="Wolves")  # EV +15%
    alpha = dx.leg_consensus(value, "1xbet", tickets=20.0, money=60.0, opening=None, source="feed-x", margin=0.03, policy=policy)
    assert alpha.divergence is dx.Divergence.CONTRARIAN_ALPHA
    summary = dx.slip_consensus([alpha, trap, unmeasured])
    assert summary["verdict"] == "PUBLIC_TRAP" and "Everton: 80% of tickets (feed-x)" in summary["explanation"] and summary["measured_legs"] == 2
    none = dx.slip_consensus([unmeasured])
    assert none["verdict"] == "NOT_MEASURED" and "Nothing is estimated" in none["explanation"]


# ================================================================ the verdict and the seal (pure)
def test_the_verdict_approves_only_a_vetted_gold_slip_or_a_clean_cure() -> None:
    critical = [dx.Pathology(dx.DiseaseCode.NEGATIVE_EV, dx.Severity.CRITICAL, "x", ("a",))]
    gold = dx.verdict([], is_vetted=True, score=96, tier="DIAMOND", gold_floor=75, joint_ev=0.08, stake=D("500"), cure=None, unexamined=0)
    assert gold[0] is VerdictStatus.APPROVED_GOLD and gold[1] == "APPROVED — GOLD GRADE" and "not that it cannot lose" in gold[2]
    cure = dx.CureView("Everton", "Brighton", 100, "DIAMOND", clean=True)
    cured = dx.verdict(critical, is_vetted=False, score=50, tier="BRONZE", gold_floor=75, joint_ev=-0.05, stake=D("0"), cure=cure, unexamined=0)
    assert cured[0] is VerdictStatus.APPROVED_WITH_CURE and "Everton for Brighton lifts it to 100" in cured[2]
    dirty = dx.verdict(critical, is_vetted=False, score=50, tier="BRONZE", gold_floor=75, joint_ev=-0.05, stake=D("0"),
                       cure=dx.CureView("Everton", "Brighton", 70, "SILVER", clean=False), unexamined=0)
    assert dirty[0] is VerdictStatus.REJECTED and "negative ev infection" in dirty[2] and "joint EV -5.0%" in dirty[2] and "no stake sized" in dirty[2]
    thin = dx.verdict([], is_vetted=False, score=80, tier="GOLD", gold_floor=75, joint_ev=0.04, stake=D("300"), cure=None, unexamined=3)
    assert thin[0] is VerdictStatus.REJECTED and "3 pillars without evidence" in thin[2]  # unchecked is not approved


def test_the_seal_is_keyed_canonical_and_tamper_evident() -> None:
    body = {"verdict": {"status": "REJECTED"}, "ev": -0.0, "big": 1e16, "legs": [{"p": 0.30000000000000004}]}
    mark = dx.seal(body, SECRET)
    assert re.fullmatch(r"[0-9a-f]{64}", mark)
    assert dx.seal_holds({"legs": [{"p": 0.30000000000000004}], "big": 10**16, "ev": 0, "verdict": {"status": "REJECTED"}}, SECRET, mark)  # jsonb's spelling
    assert not dx.seal_holds({**body, "verdict": {"status": "APPROVED_GOLD"}}, SECRET, mark)
    assert not dx.seal_holds(body, "z" * 40, mark)  # the key is the server's: a client cannot mint one


# ================================================================ end to end (live market in Redis)
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> FastAPI:
    app = app_for(sessions, redis, settings, user)
    app.include_router(consultant_api.router, prefix="/api/v1")
    return app


async def _count(sessions: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sessions() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


BAD = {"leg_ids": ["fx-fair|Match Odds|HOME", "fx-bad|Match Odds|HOME"], "kind": "DOUBLE", "skin": "one_xbet", "bankroll_inr": "100000"}


@pytest.mark.asyncio
async def test_a_consultation_prescribes_the_quoted_cure_and_records_one_sealed_verdict(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        report = (await client.post("/api/v1/consultant/audit", json=BAD)).json()
        doctor = report["doctor"]
        negative = next(p for p in doctor["pathologies"] if p["code"] == "NEGATIVE_EV")
        assert negative["severity"] == "CRITICAL" and negative["legs"] == ["fx-bad|Match Odds|HOME"]
        rx = doctor["prescription"]
        assert rx["available"] is True and rx["replaced"]["leg_id"] == "fx-bad|Match Odds|HOME" and "NEGATIVE_EV" in rx["treats"]
        assert rx["replacement"]["leg_id"].startswith("fx-good|") and rx["after"]["score"] > rx["before"]["score"] == report["rating"]["score"]
        assert rx["leg_ids"] == ["fx-fair|Match Odds|HOME", rx["replacement"]["leg_id"]]
        expected = VerdictStatus.APPROVED_WITH_CURE.value if rx["clean"] else VerdictStatus.REJECTED.value
        assert report["verdict"]["status"] == expected and report["slip"]["book"] == "1xbet"
        capital = report["consultant"]
        assert D(capital["payout_inr"]) == (D(capital["stake_inr"]) * D(report["slip"]["odds"])).quantize(D("0.01"))
        assert capital["stake_usd"] is None and capital["fx"]["inr_per_usd"] is None and capital["tax"]["tds_rate_pct"] == 30.0  # no fresh FX rate here
        sizing = next(p for p in report["rating"]["breakdown"] if p["number"] == 13)
        assert capital["sizing_reason"] == sizing["reason"]  # the stake (or why there is none) in pillar 13's own words
        clauses = {c["code"]: c for c in report["lawyer"]["clauses"]}
        assert set(clauses) >= {"MAX_PAYOUT", "VOID_LEG_IN_MULTIPLE"} and "REGULATION_TIME" not in clauses  # two Premier League legs
        assert clauses["MAX_PAYOUT"]["reference"] is None and report["lawyer"]["references_on_file"] == 0
        assert report["tipmaster"]["verdict"] == "NOT_MEASURED" and report["tipmaster"]["measured_legs"] == 0
        assert re.fullmatch(r"[0-9a-f]{64}", report["seal"]) and report["signed_for"] == str(user.id)
        assert await _count(sessions, ConsultantVerdictModel) == 1 and await _count(sessions, TwinVettingAudit) == 0  # trials only: no twin audit

        listed = (await client.get("/api/v1/consultant/verdicts")).json()["verdicts"]
        assert len(listed) == 1 and listed[0]["report_id"] == report["report_id"] and listed[0]["seal_holds"] is True
        stored = (await client.get(f"/api/v1/consultant/verdicts/{report['report_id']}")).json()
        assert stored["seal_holds"] is True and stored["seal"] == report["seal"] and stored["verdict"] == report["verdict"]
        assert {k: v for k, v in stored.items() if k not in ("seal_holds",)} == report

        async with sessions() as session:  # tamper with the stored report: the seal no longer holds
            row = await session.get(ConsultantVerdictModel, uuid.UUID(report["report_id"]))
            assert row is not None
            await session.execute(update(ConsultantVerdictModel).where(ConsultantVerdictModel.id == row.id)
                                  .values(report={**row.report, "verdict": {**row.report["verdict"], "status": "APPROVED_GOLD"}}))
            await session.commit()
        assert (await client.get(f"/api/v1/consultant/verdicts/{report['report_id']}")).json()["seal_holds"] is False

        async with sessions() as db:
            status, metrics = await PROBES[LegendaryBot.ASHVATTHAMA](db, ProbeContext(redis=None, vault_configured=True, ws_clients=lambda: 0))
        assert metrics["verdicts_24h"] == 1 and metrics["activity_summary"].startswith("1 verdict signed in 24h")

    other = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, other)), base_url="http://test") as client:
        assert (await client.get(f"/api/v1/consultant/verdicts/{report['report_id']}")).status_code == 404  # nobody else's verdict
        assert (await client.get("/api/v1/consultant/verdicts")).json()["verdicts"] == []


@pytest.mark.asyncio
async def test_the_prescription_applies_and_the_cured_slip_is_re_examined(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        report = (await client.post("/api/v1/consultant/audit", json=BAD)).json()
        rx = report["doctor"]["prescription"]
        cured = (await client.post("/api/v1/consultant/apply-prescription",
                                   json={**BAD, "replace_leg_id": rx["replaced"]["leg_id"], "with_leg_id": rx["replacement"]["leg_id"]})).json()
        assert cured["slip"]["leg_ids"] == rx["leg_ids"] and cured["rating"]["score"] == pytest.approx(rx["after"]["score"], abs=0.01)
        assert not any(p["code"] == "NEGATIVE_EV" for p in cured["doctor"]["pathologies"])
        critical = any(p["severity"] == "CRITICAL" for p in cured["doctor"]["pathologies"])
        if cured["slip"]["is_vetted"] and not critical and cured["rating"]["score"] >= settings.MANUAL_PARLAY_TIERS["GOLD"]:
            assert cured["verdict"]["status"] == "APPROVED_GOLD"
        assert await _count(sessions, ConsultantVerdictModel) == 2
        same = await client.post("/api/v1/consultant/apply-prescription", json={**BAD, "replace_leg_id": "fx-bad|Match Odds|HOME", "with_leg_id": "fx-fair|Match Odds|AWAY"})
        assert same.status_code == 422 and same.json()["detail"]["reason"] == "SAME_FIXTURE"
        stray = await client.post("/api/v1/consultant/apply-prescription", json={**BAD, "replace_leg_id": "fx-good|Match Odds|HOME", "with_leg_id": "fx-good|Match Odds|AWAY"})
        assert stray.status_code == 422 and stray.json()["detail"]["reason"] == "NOT_IN_SLIP"
        assert (await client.post("/api/v1/consultant/audit", json={**BAD, "skin": "betway"})).status_code == 422
        assert (await client.post("/api/v1/consultant/audit", json={**BAD, "leg_ids": [f"fx-{i}|Match Odds|HOME" for i in range(9)]})).status_code == 422


@pytest.mark.asyncio
async def test_measured_splits_make_a_public_trap_and_a_recorded_cap_rejects(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    capped = settings.model_copy(update={"CONSULTANT_MAX_PAYOUT_INR": {"1xbet": D("10")}, "CONSULTANT_RULE_REFERENCES": {"1xbet": {"MAX_PAYOUT": "Rule 3.1"}}})
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, capped, user)), base_url="http://test") as client:
        await _market(redis, capped, client)
        seen = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        splits = {"public_splits": {"source": "test-splits-feed", "observed_at": seen, "splits": {"Match Odds": {"HOME": {"tickets_pct": 0.82, "money_pct": 0.35}}}}}
        assert (await client.put("/api/v1/twin/intel/fx-bad", json=splits)).status_code == 200
        report = (await client.post("/api/v1/consultant/audit", json={**BAD, "leg_ids": ["fx-bad|Match Odds|HOME"], "kind": "SINGLE"})).json()
        tip = report["tipmaster"]
        assert tip["verdict"] == "PUBLIC_TRAP" and tip["legs"][0]["tickets_pct"] == pytest.approx(82.0) and tip["legs"][0]["source"] == "test-splits-feed"
        assert "82% of tickets (test-splits-feed)" in tip["explanation"]
        cap = next(c for c in report["lawyer"]["clauses"] if c["code"] == "MAX_PAYOUT")
        if D(report["consultant"]["stake_inr"]) > 0:
            assert cap["risk"] == "HIGH" and cap["reference"] == "Rule 3.1" and report["consultant"]["payout_over_cap"] is True
            assert any(p["code"] == "BOOKMAKER_LIMIT_TRAP" for p in report["doctor"]["pathologies"])
        assert report["verdict"]["status"] == "REJECTED"  # a single priced at a loss, over the recorded cap


# ================================================================ routes and the front end
def test_the_routes_are_signed_in_only_and_the_mock_tipmaster_is_gone() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path, method in (("/api/v1/consultant/audit", "post"), ("/api/v1/consultant/apply-prescription", "post"), ("/api/v1/consultant/verdicts", "get"),
                         ("/api/v1/consultant/verdicts/{report_id}", "get"), ("/api/v1/consultant/rules-library", "get")):
        assert paths[path][method].get("security"), path
    assert "/api/v1/dashboard/tips" not in paths  # TipMaster lives in the Consultant now, on measured shares
    import importlib.util  # noqa: PLC0415

    assert importlib.util.find_spec("app.domain.dashboard.tip_master") is None  # its hash-made "sharp" probability is retired


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_consultant_page_is_wired_and_reads_the_real_slip() -> None:
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    assert re.search(r"to: ['\"]/consultant['\"].*bot: ['\"]ASHVATTHAMA['\"]", app) and 'path="/consultant"' in app
    page = (FRONTEND / "pages" / "TheConsultant.tsx").read_text(encoding="utf-8")
    assert "useParlaySlipStore" in page and page.index("<CommanderHero") < page.index("<ComponentGuideBanner")
    consultant = [page, *(p.read_text(encoding="utf-8") for p in (FRONTEND / "components" / "consultant").glob("*.tsx")), (FRONTEND / "lib" / "consultant.ts").read_text(encoding="utf-8")]
    for source in consultant:
        assert "lucide-react" not in source and "Arsenal ML" not in source and not re.search(r"/\s*84(\.0+)?\b", source)  # no stock legs, no constant FX
    command = (FRONTEND / "pages" / "CommandCenter.tsx").read_text(encoding="utf-8")
    assert "/dashboard/tips" not in command and "/consultant" in command
