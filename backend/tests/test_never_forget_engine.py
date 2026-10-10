"""Group 75: the Never-Forget shield (pillar 15) and experience (FA-2).

The brief's proofs on the real code paths:

* the situation: each feature scaled as the brief writes it (rain / 10, wind / 50, (72 - rest) / 48, cards / 8,
  penalties / 1), indoors dry and still, missing evidence absent (never imputed);
* the weighted Gaussian similarity exp(-4 D_w^2), renormalised over the shared features, inconclusive under
  the coverage floor; a lesson guards its own shape of bet only;
* pillar 15 inside the fortress: an ACTIVE lesson vetoes, an EXPERIMENTAL one only shadows, an unreadable
  vault or an incomparable lesson is unverified, the shield switched off is advisory;
* the verdict on a new lesson: variance and thin evidence stay EXPERIMENTAL, so does a lesson that would
  have matched too much of what the fortress saw recently;
* XP: the tiers, an award paid once whatever retries, the promotion, the disciplined streak.

Then the whole loop through the API, Redis and the feedback sweep: a slip vetted, placed and lost in rain
the forecast missed; the post-mortem memorises it; the same situation on another fixture is vetoed, once;
that leg loses too and the veto is credited; an administrator archives and re-activates the lesson.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import dataclasses
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user
from app.api.v1 import digital_twin as twin_api
from app.api.v1 import never_forget as nf_api
from app.api.v1 import user_xp as xp_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import fortress
from app.domain.oracle import never_forget as nf
from app.domain.oracle.fortress import FortressPolicy, Status
from app.domain.oracle.markets import MarketKind, MarketRef
from app.models import User
from app.models.cfo_vault import BankrollAccount, MarketResult
from app.models.control_panel import SystemSettingsModel
from app.models.digital_twin import TwinInPlayMonitor, TwinVettingAudit
from app.models.feedback import ModelPredictionFeedback, SettlementRootCauseAudit
from app.models.hive_bots import TradingBot
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit
from app.models.nalanda_lake import NalandaTick
from app.models.never_forget import AshokaMistakeMemory, NeverForgetPreventionAudit, NeverForgetRule, RuleStatus, UserXPProfile, XPActionType, XPAuditLog
from app.models.omni_vault import OmniFleetSource
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg
from app.services.sentinel_bus import AlertKind
from app.services.twin import feedback_tracker, never_forget, xp_engine
from tests.test_ultra_vetting import NOW, PINNACLE_NO_ARB, SOFT, inputs, intel_ok, seed, stream

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, BankrollAccount.__table__, OmniFleetSource.__table__, MarketResult.__table__, SystemSettingsModel.__table__,
    UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__, PopularParlayModel.__table__, ParlayReviewGateModel.__table__,
    TwinVettingAudit.__table__, TwinInPlayMonitor.__table__, NalandaTick.__table__, ModelPredictionFeedback.__table__, SettlementRootCauseAudit.__table__,
    ModelRecalibrationRun.__table__, ModelWeightAudit.__table__,
    AshokaMistakeMemory.__table__, NeverForgetRule.__table__, NeverForgetPreventionAudit.__table__, UserXPProfile.__table__, XPAuditLog.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-never-forget"


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_nf", "ASHOKA_MC_PATHS": 4_000, "ORACLE_TIMEZONE": "Asia/Kolkata",
                                             "NEVER_FORGET_ENABLED": True})


@pytest_asyncio.fixture
async def user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        row = User(username=f"nf_{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
        session.add(row)
        await session.commit()
        return row


def nfp(settings: Settings, **override: Any) -> nf.NeverForgetPolicy:
    return dataclasses.replace(nf.NeverForgetPolicy.from_settings(settings), **override)


def run(settings: Settings, lessons: list[nf.Lesson] | None, **kw: Any) -> fortress.FortressVerdict:
    policy = FortressPolicy.from_settings(settings)
    return fortress.run(dataclasses.replace(inputs(max_stake=D("50000"), **kw), lessons=lessons), policy, ("pinnacle",), situations=nf.NeverForgetPolicy.from_settings(settings))


def lesson_from(verdict: fortress.FortressVerdict, status: str = nf.ACTIVE, code: str = "NF-TEST-1") -> nf.Lesson:
    row = verdict.pillars[14].metrics["legs"][0]
    return nf.Lesson(str(uuid.uuid4()), code, str(uuid.uuid4()), status, row["shape"], row["vector"], "lost it in exactly this situation")


# ================================================================ the mathematics
def test_the_situation_scales_each_feature_as_the_brief_writes_it(settings: Settings) -> None:
    p = nfp(settings)
    s = nf.situation(p, odds=2.10, rain_mmh=5.2, wind_kmh=30, rest_hours=48, cards_per_game=6.0, penalties_per_90=0.3, steam_against=True, model_ev=0.05, sharp_edge=0.3, public_share=0.75)
    assert s.vector == pytest.approx({"rain": 0.52, "wind": 0.6, "fatigue": 0.5, "cards": 0.75, "penalties": 0.3, "steam": 1.0, "odds": 0.11, "model_ev": 0.25, "sharp_edge": 1.0, "public": 0.75})
    assert s.raw["rain"] == 5.2 and s.raw["fatigue"] == 48.0  # the evidence as read is kept beside the scaled vector
    rested = nf.situation(p, odds=1.5, rain_mmh=40, rest_hours=200)
    assert rested.vector["rain"] == 1.0 and rested.vector["fatigue"] == 0.0  # clamped at both ends
    indoor = nf.situation(p, odds=2.0, indoor=True)
    assert indoor.vector["rain"] == 0.0 and indoor.vector["wind"] == 0.0  # the roof is closed
    bare = nf.situation(p, odds=2.0)
    assert set(bare.vector) == {"odds"}  # nothing imputed: no weather is no weather, not a dry day
    assert nf.describe(s.raw).startswith("rain 5.2 mm/h, wind 30 km/h, rest 48h, referee 6 cards a game")


def test_weighted_gaussian_similarity_coverage_and_the_threshold(settings: Settings) -> None:
    p = nfp(settings)
    lesson = {"rain": 0.52, "wind": 0.6, "fatigue": 0.5, "cards": 0.75, "penalties": 0.3, "steam": 0.0, "odds": 0.11}
    assert nf.compare(lesson, lesson, p).similarity == 1.0
    near = {**lesson, "rain": 0.40, "fatigue": 0.40}
    d2 = 0.25 * 0.12 ** 2 + 0.25 * 0.10 ** 2  # the weights sum to 1 over the seven features
    c = nf.compare(near, lesson, p)
    assert c.coverage == 1.0 and c.similarity == pytest.approx(math.exp(-4.0 * d2)) and c.similarity >= 0.82
    far = {**lesson, "rain": 0.0, "wind": 0.1}
    apart = nf.compare(far, lesson, p).similarity
    assert apart == pytest.approx(math.exp(-4.0 * (0.25 * 0.52 ** 2 + 0.15 * 0.5 ** 2))) and apart < 0.82
    # the shared features only, renormalised: no weather on the candidate leaves 60% of the weight
    dry = {k: v for k, v in lesson.items() if k not in ("rain", "wind")}
    partial = nf.compare(dry, lesson, p)
    assert partial.coverage == pytest.approx(0.60) and partial.similarity == 1.0 and partial.missing == ("rain", "wind")
    thin = {k: v for k, v in lesson.items() if k in ("odds", "steam", "penalties")}
    assert nf.compare(thin, lesson, p).similarity is None  # 20% of the weight: inconclusive, never a match
    assert nf.own_coverage(thin, p) == pytest.approx(0.20)


def test_a_lesson_guards_its_own_shape_of_bet(settings: Settings) -> None:
    totals = MarketRef(MarketKind.TOTALS, 2.5)
    assert nf.bet_shape(totals, "OVER") == "TOTALS:OVER" != nf.bet_shape(totals, "UNDER")
    assert nf.bet_shape(MarketRef(MarketKind.MATCH_ODDS), "HOME") == nf.bet_shape(MarketRef(MarketKind.MATCH_ODDS), "AWAY") == "MATCH_ODDS:SIDE"
    v = {"rain": 0.6, "wind": 0.5, "fatigue": 0.2, "cards": 0.5, "penalties": 0.2, "steam": 0.0, "odds": 0.1}
    over = nf.Lesson("r", "NF-1", "m", nf.ACTIVE, "TOTALS:OVER", v, "an over lost in the rain")
    assert nf.scan("TOTALS:UNDER", v, [over], nfp(settings)).vetoes == []  # a lost Over says nothing about an Under
    assert len(nf.scan("TOTALS:OVER", v, [over], nfp(settings)).vetoes) == 1
    assert len(nf.scan("TOTALS:UNDER", v, [over], nfp(settings, scope="any")).vetoes) == 1
    shadow = nf.scan("TOTALS:OVER", v, [dataclasses.replace(over, status=nf.EXPERIMENTAL)], nfp(settings))
    assert shadow.vetoes == [] and len(shadow.shadow) == 1
    assert nf.scan("TOTALS:OVER", v, [dataclasses.replace(over, status=nf.ARCHIVED)], nfp(settings)).compared == 0


# ================================================================ pillar 15 in the fortress
def test_pillar_15_vetoes_the_same_situation_and_records_every_leg(settings: Settings) -> None:
    clean = run(settings, [])
    p15 = clean.pillars[14]
    assert clean.is_vetted and clean.passed == 15 and p15.status is Status.PASS and p15.reason == "no lesson memorised yet"
    row = p15.metrics["legs"][0]
    assert row["shape"] == "MATCH_ODDS:SIDE" and row["raw"]["rain"] == 0.0 and row["raw"]["fatigue"] == 144.0 and row["raw"]["cards"] == 3.9
    assert row["vector"]["steam"] == 0.0 and "model_ev" in row["vector"] and "sharp_edge" in row["vector"]  # pillars 1 and 7's numbers, recorded

    vetoed = run(settings, [lesson_from(clean)])
    assert not vetoed.is_vetted and fortress.PILLAR_KEYS[15] == "never_forget"
    assert vetoed.pillars[14].status is Status.FAIL and "100% like lesson NF-TEST-1" in vetoed.pillars[14].reason
    assert any(r.startswith("P15 Never-Forget shield") for r in vetoed.reasons) and vetoed.passed == 14

    shadow = run(settings, [lesson_from(clean, nf.EXPERIMENTAL)])
    assert shadow.is_vetted and shadow.pillars[14].metrics["shadow_matches"][0]["rule_code"] == "NF-TEST-1"  # reported, never a veto

    seen = {"source": "feed", "observed_at": (NOW - timedelta(minutes=5)).isoformat()}
    drizzle = intel_ok(NOW, weather={**seen, "wind_kmh": 12, "precipitation_mmh": 2.4})
    # inside every limit, 2.4 mm/h against 0 is still the same situation: exp(-4 x 0.25 x 0.24^2) = 94%
    assert run(settings, [lesson_from(clean)], intel=drizzle).pillars[14].status is Status.FAIL
    other = intel_ok(NOW, weather={**seen, "wind_kmh": 24, "precipitation_mmh": 2.4}, referee={**seen, "name": "B. Strict", "cards_per_game": 7.5, "penalties_per_90": 0.21})
    different = run(settings, [lesson_from(clean)], intel=other)
    d2 = 0.25 * 0.24 ** 2 + 0.15 * 0.24 ** 2 + 0.15 * 0.45 ** 2  # rain, wind, cards
    assert different.pillars[14].status is Status.PASS and "the closest is 81% alike" in different.pillars[14].reason
    assert different.pillars[14].metrics["legs"][0]["closest"]["similarity"] == pytest.approx(math.exp(-4 * d2), abs=1e-4)


def test_pillar_15_is_unverified_without_the_vault_or_the_evidence_and_advisory_when_off(settings: Settings) -> None:
    clean = run(settings, [])
    lesson = lesson_from(clean)
    unread = run(settings, None)
    assert unread.pillars[14].status is Status.UNVERIFIED and "cannot be read" in unread.pillars[14].reason and not unread.is_vetted
    no_weather = intel_ok(NOW, weather=None, travel=None)  # without weather and travel only 40% of the lesson's weight is shared
    blind = run(settings, [lesson], intel=no_weather)
    assert blind.pillars[14].status is Status.UNVERIFIED and "against NF-TEST-1 (no fresh fatigue, rain, wind)" in blind.pillars[14].reason
    off = settings.model_copy(update={"NEVER_FORGET_ENABLED": False})
    policy = FortressPolicy.from_settings(off)
    assert policy.never_forget is None
    verdict = fortress.run(dataclasses.replace(inputs(max_stake=D("50000")), lessons=[lesson]), policy, ("pinnacle",), situations=nf.NeverForgetPolicy.from_settings(off))
    assert verdict.pillars[14].status is Status.ADVISORY and verdict.is_vetted and verdict.passed == 14
    assert "vector" in verdict.pillars[14].metrics["legs"][0]  # switched off, the situation is still recorded for later lessons


def test_a_new_lesson_is_active_only_when_it_names_a_trap(settings: Settings) -> None:
    p = nfp(settings)
    v = {"rain": 0.52, "wind": 0.6, "fatigue": 0.5, "cards": 0.75, "penalties": 0.3, "steam": 0.0, "odds": 0.11}
    none_seen = nf.specificity(v, "MATCH_ODDS:SIDE", "fx-1", [], p)
    status, why = never_forget.verdict(p, settings, "WEATHER_ANOMALY", v, none_seen)
    assert status is RuleStatus.ACTIVE and "guarding conservatively" in why
    assert never_forget.verdict(p, settings, "VARIANCE_BAD_LUCK", v, none_seen)[0] is RuleStatus.EXPERIMENTAL
    thin = {"odds": 0.11, "steam": 0.0}
    status, why = never_forget.verdict(p, settings, "WEATHER_ANOMALY", thin, none_seen)
    assert status is RuleStatus.EXPERIMENTAL and "10% of the situation's weight" in why
    # thirty other fixtures seen recently in nearly the same situation: the lesson describes ordinary betting
    alike = [nf.SeenLeg(f"seen-{i}", "MATCH_ODDS:SIDE", {**v, "odds": 0.12}) for i in range(30)] + [nf.SeenLeg("fx-1", "MATCH_ODDS:SIDE", v)] * 5
    broad = nf.specificity(v, "MATCH_ODDS:SIDE", "fx-1", alike, p)
    assert (broad.comparable, broad.matched) == (30, 30)  # its own fixture is not counted
    status, why = never_forget.verdict(p, settings, "WEATHER_ANOMALY", v, broad)
    assert status is RuleStatus.EXPERIMENTAL and "30 of the 30 comparable legs" in why
    varied = [nf.SeenLeg(f"seen-{i}", "MATCH_ODDS:SIDE", {**v, "rain": 0.0, "wind": 0.1, "fatigue": 0.0}) for i in range(30)]
    status, why = never_forget.verdict(p, settings, "WEATHER_ANOMALY", v, nf.specificity(v, "MATCH_ODDS:SIDE", "fx-1", varied, p))
    assert status is RuleStatus.ACTIVE and "specific: it matches 0 of the 30" in why


# ================================================================ experience
def test_the_tiers(settings: Settings) -> None:
    assert xp_engine.tier_for(0, settings).rank == "ROOKIE" and xp_engine.tier_for(1000, settings).rank == "ROOKIE"
    t = xp_engine.tier_for(1001, settings)
    assert (t.level, t.rank, t.floor, t.next_floor, t.next_rank) == (2, "QUANT_APPRENTICE", 1001, 5001, "HIGH_ROLLER") and t.progress(3001) == 50.0
    top = xp_engine.tier_for(90_000, settings)
    assert top.rank == "THE_ORACLE" and top.level == 5 and top.next_floor is None and top.progress(90_000) == 100.0
    with pytest.raises(ValueError):
        Settings.model_validate({**settings.model_dump(), "XP_TIERS": {"A": 10, "B": 5}})


@pytest.mark.asyncio
async def test_xp_is_paid_once_and_promotes(sessions: async_sessionmaker[AsyncSession], settings: Settings, user: User) -> None:
    rich = settings.model_copy(update={"XP_AWARD_BET_WON": 500, "XP_AWARD_LOSS_PREVENTED": 600})
    async with sessions() as session:
        assert await xp_engine.award(session, rich, xp_engine.Award(user.id, XPActionType.BET_WON, "bet:1", "won"), NOW) == 500
        assert await xp_engine.award(session, rich, xp_engine.Award(user.id, XPActionType.BET_WON, "bet:1", "won again?"), NOW) == 0  # the same bet pays once
        await session.commit()
        profile = await xp_engine.profile_for(session, user.id, rich, NOW)
        assert (profile.total_xp, profile.level, profile.rank_title) == (500, 1, "ROOKIE")
        await xp_engine.award(session, rich, xp_engine.Award(user.id, XPActionType.LOSS_PREVENTED, "veto:1", "shielded"), NOW)
        await session.commit()
        profile = await xp_engine.profile_for(session, user.id, rich, NOW)
        assert (profile.total_xp, profile.level, profile.rank_title, profile.bets_won_count, profile.losses_prevented_count) == (1100, 2, "QUANT_APPRENTICE", 1, 1)
        assert await session.scalar(select(func.count()).select_from(XPAuditLog)) == 2


@pytest.mark.asyncio
async def test_the_disciplined_streak(sessions: async_sessionmaker[AsyncSession], settings: Settings, user: User) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)  # 17:30 in Kolkata

    def audit(day: int, vetted: bool = True) -> TwinVettingAudit:
        return TwinVettingAudit(id=uuid.uuid4(), user_id=user.id, slip_id=f"s{day}", kind="SINGLE", leg_ids=[], bookmaker="1xbet", stake_inr=D("0"), pillars_passed=15 if vetted else 10,
                                conviction_score=100.0, is_vetted=vetted, pillars=[], rejection_reasons=[], slip={}, created_at=now - timedelta(days=day))

    async with sessions() as session:
        session.add_all([audit(d) for d in range(6)])
        await session.commit()
        assert await xp_engine.streak_days(session, user.id, settings, now) == 6 and await xp_engine.streak_award(session, user.id, settings, now) is None
        session.add(audit(6))
        await session.commit()
        bonus = await xp_engine.streak_award(session, user.id, settings, now)
        assert bonus is not None and bonus.source_ref == "streak:2026-10-10" and bonus.metadata == {"days": 7}
        # a bet placed three days ago from a slip the fortress did not vet breaks the run
        failed = audit(3, vetted=False)
        session.add(failed)
        session.add(UserPlacedBet(id=uuid.uuid4(), user_id=user.id, vetting_audit_id=failed.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("100"), placed_at=now - timedelta(days=3)))
        await session.commit()
        assert await xp_engine.streak_days(session, user.id, settings, now) == 3


# ================================================================ the whole loop
def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    for module in (twin_api, nf_api, xp_api):
        app.include_router(module.router, prefix="/api/v1")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


async def final_score(sessions: async_sessionmaker[AsyncSession], fixture: str, home: str, away: str, home_goals: int, away_goals: int) -> None:
    async with sessions() as session:
        session.add(FixtureScore(fixture_id=fixture, home=home, away=away, sport_key="soccer_epl", home_goals=home_goals, away_goals=away_goals, status="FINAL", source="admin"))
        await session.commit()


@pytest.mark.asyncio
async def test_lose_once_memorise_it_and_never_step_into_it_again(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    calm = intel_ok(datetime.now(UTC)).model_dump(mode="json", exclude_none=True)
    first, second = "fx-ars-che", "fx-tot-whu"
    await seed(redis, settings, first, "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        # 1. vetted in a calm forecast, placed, and lost: it rained after all
        await client.put(f"/api/v1/twin/intel/{first}", json=calm)
        vetted = (await client.post("/api/v1/twin/vet", json={"leg_ids": [f"{first}|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
        assert vetted["is_vetted"] and vetted["pillars_passed"] == 15, vetted["rejection_reasons"]
        assert (await client.post(f"/api/v1/twin/audits/{vetted['id']}/ledger", json={"bookmaker": "1XBET", "stake_inr": vetted["stake_inr"], "placed_odds": "2.45", "watch": False})).status_code == 201
        storm = {**calm["weather"], "precipitation_mmh": 6.5, "observed_at": datetime.now(UTC).isoformat()}
        await client.put(f"/api/v1/twin/intel/{first}", json={"weather": storm})
        await final_score(sessions, first, "Arsenal", "Chelsea", 0, 1)
        report = await feedback_tracker.sweep(sessions, redis, settings, datetime.now(UTC))
        assert report.root_causes == {"WEATHER_ANOMALY": 1} and report.never_forget["memorised"] == 1, report.as_dict()

        # 2. the post-mortem: the situation as vetted, the cause as found, an ACTIVE rule, a page, XP
        memories = (await client.get("/api/v1/twin/never-forget/memories")).json()
        assert len(memories) == 1
        m = memories[0]
        assert m["loss_root_cause"] == "WEATHER_ANOMALY" and m["shape"] == "MATCH_ODDS:SIDE" and m["situation"]["rain"] == 0.0  # the forecast it was vetted on
        assert "rain 6.5 mm/h" in m["extracted_lesson"] and "Lost Arsenal v Chelsea Match Odds HOME @ 2.45" in m["extracted_lesson"]
        assert m["rule"]["status"] == "ACTIVE" and m["rule"]["rule_code"].startswith("NF-") and m["developer_credit"] == "Amit Ashok Kumar Patnaik"
        lessons = [a for a in await stream(redis, settings) if a.kind is AlertKind.NEVER_FORGET_LESSON]
        assert len(lessons) == 1 and m["rule"]["rule_code"] in lessons[0].title and lessons[0].severity.value == "WARNING"
        again = await feedback_tracker.sweep(sessions, redis, settings, datetime.now(UTC))
        assert again.never_forget.get("memorised", 0) == 0  # never memorised twice

        # 3. the same situation on another fixture: vetoed, and counted once however often it is re-run
        await seed(redis, settings, second, "Tottenham", "West Ham", sharp=PINNACLE_NO_ARB, soft=SOFT)
        await client.put(f"/api/v1/twin/intel/{second}", json=calm)
        for _ in range(2):
            blocked = (await client.post("/api/v1/twin/vet", json={"leg_ids": [f"{second}|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
            p15 = blocked["pillars"][14]
            assert not blocked["is_vetted"] and p15["status"] == "FAIL" and m["rule"]["rule_code"] in p15["reason"], blocked["rejection_reasons"]
        prevented = (await client.get("/api/v1/twin/never-forget/preventions")).json()
        assert len(prevented) == 1 and prevented[0]["outcome"] is None and prevented[0]["stake_withheld_inr"] == blocked["stake_inr"]
        rules = (await client.get("/api/v1/twin/never-forget/rules", params={"status": "ACTIVE"})).json()
        assert rules[0]["times_triggered"] == 1

        # 4. that leg lost too: the veto is measured, and credited
        await final_score(sessions, second, "Tottenham", "West Ham", 1, 2)
        swept = await feedback_tracker.sweep(sessions, redis, settings, datetime.now(UTC))
        assert swept.never_forget["vetoes_resolved"] == 1
        stats = (await client.get("/api/v1/twin/never-forget/stats")).json()
        assert stats["rules"] == {"ACTIVE": 1, "EXPERIMENTAL": 0, "ARCHIVED": 0} and stats["total_mistakes_memorized"] == 1
        assert stats["mine"]["lost"] == 1 and stats["mine"]["won"] == 0 and stats["mine"]["stake_withheld_on_losers_inr"] == str(D(blocked["stake_inr"]).quantize(D("0.01")))
        profile = (await client.get("/api/v1/user/xp/profile")).json()
        assert profile["total_xp"] == 25 + 200 + 150 and profile["rank_title"] == "ROOKIE" and profile["losses_prevented_count"] == 1
        assert profile["developer_credit"] == "Amit Ashok Kumar Patnaik" and profile["next_rank"] == "QUANT_APPRENTICE"
        kinds = sorted(h["action_type"] for h in (await client.get("/api/v1/user/xp/history")).json())
        assert kinds == ["LOSS_PREVENTED", "MISTAKE_MEMORIZED", "SLIP_VETTED"]

        # 5. an administrator retires the lesson (the reason is kept), and brings it back
        rule_id = m["rule"]["id"]
        assert (await client.post(f"/api/v1/twin/never-forget/rules/{rule_id}/archive", json={"reason": "no"})).status_code == 422
        archived = (await client.post(f"/api/v1/twin/never-forget/rules/{rule_id}/archive", json={"reason": "the forecast feed was broken that week"})).json()
        assert archived["status"] == "ARCHIVED" and "the forecast feed was broken" in archived["status_reason"]
        assert (await client.post(f"/api/v1/twin/never-forget/rules/{rule_id}/archive", json={"reason": "twice over"})).status_code == 409
        free = (await client.post("/api/v1/twin/vet", json={"leg_ids": [f"{second}|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
        assert free["pillars"][14]["status"] == "PASS"
        assert (await client.post(f"/api/v1/twin/never-forget/rules/{rule_id}/activate", json={"reason": "the feed is fixed; guard again"})).json()["status"] == "ACTIVE"
        assert (await client.post(f"/api/v1/twin/never-forget/rules/{uuid.uuid4()}/activate", json={"reason": "not there"})).status_code == 404


@pytest.mark.asyncio
async def test_variance_is_a_shadow_lesson_and_a_won_bet_pays(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    calm = intel_ok(datetime.now(UTC)).model_dump(mode="json", exclude_none=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        for fixture, home, away in (("fx-a", "Arsenal", "Chelsea"), ("fx-b", "Leeds", "Burnley")):
            await seed(redis, settings, fixture, home, away, sharp=PINNACLE_NO_ARB, soft=SOFT)
            await client.put(f"/api/v1/twin/intel/{fixture}", json=calm)
            audit = (await client.post("/api/v1/twin/vet", json={"leg_ids": [f"{fixture}|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
            assert audit["is_vetted"], audit["rejection_reasons"]
            await client.post(f"/api/v1/twin/audits/{audit['id']}/ledger", json={"bookmaker": "1XBET", "stake_inr": "500", "placed_odds": "2.45", "watch": False})
        await final_score(sessions, "fx-a", "Arsenal", "Chelsea", 0, 0)  # lost, nothing to blame: variance
        await final_score(sessions, "fx-b", "Leeds", "Burnley", 2, 0)  # won
        report = await feedback_tracker.sweep(sessions, redis, settings, datetime.now(UTC))
        assert report.root_causes == {"VARIANCE_BAD_LUCK": 1} and report.never_forget["memorised"] == 1
        rule = (await client.get("/api/v1/twin/never-forget/rules")).json()[0]
        assert rule["status"] == "EXPERIMENTAL" and "variance bad luck loss" in rule["status_reason"]
        # an EXPERIMENTAL lesson never vetoes: the same situation still vets, with the shadow match reported
        await seed(redis, settings, "fx-c", "Fulham", "Brentford", sharp=PINNACLE_NO_ARB, soft=SOFT)
        await client.put("/api/v1/twin/intel/fx-c", json=calm)
        shadowed = (await client.post("/api/v1/twin/vet", json={"leg_ids": ["fx-c|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
        assert shadowed["is_vetted"] and shadowed["pillars"][14]["metrics"]["shadow_matches"][0]["rule_code"] == rule["rule_code"]
        profile = (await client.get("/api/v1/user/xp/profile")).json()
        # three slips vetted, one bet won, one lesson memorised
        assert profile["total_xp"] == 3 * 25 + 50 + 200 and profile["bets_won_count"] == 1 and profile["slips_vetted_count"] == 3
