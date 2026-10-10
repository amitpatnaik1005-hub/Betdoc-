"""Group 74: the model recalibration engine. Brier skill, the lifecycle, pillar 1's weights.

The brief's proofs on the real code paths:

* the half-life decay (a 30-day-old prediction counts half; lambda = ln 2 / 30 = 0.023105 per day);
* Murphy's decomposition, exact when forecasts are constant within each bin, with UNC = o(1 - o) on 0/1;
* Brier skill against the de-vigged close, on the legs both priced only;
* the four states, each from its own evidence; a winner alpha boosted above 1, a coin-flipper benched at 0,
  an under-performer on probation in its band, a two-prediction newcomer shrunk toward 1;
* the softmax over the established models, the bands, the shrinkage formula, administrators' pins;
* the atomic Redis publish pillar 1 reads, and pillar 1 taking the benched model's veto away.

Then the service and the API: runs and audits recorded, drift paged, the lock, a run with no data
leaving the weights in force, overrides that survive later runs when pinned, the reset, the loss trigger.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import pickle
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from app.api.v1 import model_calibration as calibration_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import calibration as cal
from app.domain.oracle import fortress
from app.models import User
from app.models.control_panel import SystemSettingsModel
from app.models.feedback import REFERENCE_PREDICTORS, ModelPredictionFeedback, SettlementRootCauseAudit
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit, RecalibrationTrigger
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, decode
from app.services.twin import model_calibrator
from app.services.twin.intel import model_weights, weight_pins_key, weights_key, weights_meta_key

D = Decimal
TABLES = [
    User.__table__, SystemSettingsModel.__table__, UserPlacedBet.__table__, UserPlacedLeg.__table__, ModelPredictionFeedback.__table__,
    SettlementRootCauseAudit.__table__, ModelRecalibrationRun.__table__, ModelWeightAudit.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-calibration"
NOW = datetime.now(UTC).replace(microsecond=0)


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
    return get_settings().model_copy(update={"TWIN_PREFIX": "test_twin_cal"})


def policy(settings: Settings) -> cal.CalibrationPolicy:
    return cal.CalibrationPolicy.from_settings(settings, REFERENCE_PREDICTORS)


def obs(model: str, legs: Sequence[int], p: float, y: float, *, clv: float | None = 0.0, age: float = 5.0) -> list[cal.Observation]:
    return [cal.Observation(model, f"leg-{i}", p, y, (p - y) ** 2, clv, age) for i in legs]


def fleet() -> list[cal.Observation]:
    """Thirty settled legs the close (Brier 0.16) priced; five models over them, a coin-flipper on legs of its own."""
    legs = range(30)
    return [
        *obs("closing_sharp", legs, 0.6, 1.0),  # the benchmark: 0.16
        *obs("ensemble", legs, 0.6, 1.0),  # a reference: measured, never weighted
        *obs("poisson", legs, 0.8, 1.0, clv=4.5),  # 0.04, BSS +0.75, CLV +4.5%: alpha
        *obs("steady", legs, 0.6, 1.0, clv=0.0),  # 0.16, BSS 0: active
        *obs("drifting", legs, 0.58, 1.0, clv=0.0),  # 0.1764, BSS -0.1025: probation
        *obs("failing", range(100, 130), 0.9, 0.0, clv=-5.0),  # 0.81: benched
        *obs("sparse", range(2), 0.99, 1.0, clv=10.0),  # two lucky predictions
    ]


# ================================================================ the mathematics
def test_the_half_life_decay_and_the_decayed_brier() -> None:
    assert math.log(2) / 30 == pytest.approx(0.023105, abs=1e-6)
    assert cal.decay_weight(0, 30) == 1.0 and cal.decay_weight(30, 30) == pytest.approx(0.5) and cal.decay_weight(60, 30) == pytest.approx(0.25)
    rows = [cal.Observation("m", "a", 0.5, 1, 0.25, None, 0.0), cal.Observation("m", "b", 0.5, 1, 0.01, None, 30.0)]
    assert cal.decayed_brier(rows, 30) == pytest.approx((0.25 * 1 + 0.01 * 0.5) / 1.5)  # the fresh one counts twice the old one


def test_murphy_decomposition_is_exact_on_constant_bins() -> None:
    pairs = [(0.2, 0.0)] * 8 + [(0.2, 1.0)] * 2 + [(0.7, 1.0)] * 6 + [(0.7, 0.0)] * 4
    m = cal.murphy_decomposition(pairs, 10)
    assert m is not None
    base = 8 / 20
    assert m.uncertainty == pytest.approx(base * (1 - base))  # o(1 - o) on 0/1 outcomes
    assert m.reliability == pytest.approx(0.5 * (0.2 - 0.2) ** 2 + 0.5 * (0.7 - 0.6) ** 2)
    assert m.resolution == pytest.approx(0.5 * (0.2 - base) ** 2 + 0.5 * (0.6 - base) ** 2)
    assert m.brier == pytest.approx(m.reliability - m.resolution + m.uncertainty) and m.residual == pytest.approx(0.0, abs=1e-12)
    half = cal.murphy_decomposition([(0.6, 0.75), (0.6, 0.25)], 10)
    assert half is not None and half.uncertainty == pytest.approx(0.0625)  # half results: the variance of the outcomes
    assert cal.murphy_decomposition([], 10) is None


def test_the_four_states_and_their_weights(settings: Settings) -> None:
    result = cal.evaluate(fleet(), policy(settings))
    v = {x.model: x for x in result.verdicts}
    assert set(v) == {"poisson", "steady", "drifting", "failing", "sparse"}  # closing_sharp and ensemble are references
    assert result.benchmark_brier == pytest.approx(0.16) and result.references == {"closing_sharp": 30, "ensemble": 30}
    assert v["poisson"].status is cal.Status.ALPHA_BOOSTED and v["poisson"].bss == pytest.approx(0.75) and "alpha boost" in v["poisson"].reason
    assert v["steady"].status is cal.Status.ACTIVE and v["steady"].bss == pytest.approx(0.0)
    assert v["drifting"].status is cal.Status.PROBATION and v["drifting"].bss == pytest.approx(1 - 0.1764 / 0.16) and "probation" in v["drifting"].reason
    assert v["failing"].status is cal.Status.BENCHED and v["failing"].paired == 0 and v["failing"].bss is None and "coin flip" in v["failing"].reason
    assert v["sparse"].status is cal.Status.ACTIVE and "under 25" in v["sparse"].reason
    w = result.weights
    # the established softmax: poisson's logit (-0.04/0.1 + 2 x 4.5) dwarfs the others', so the bands decide
    assert w["poisson"] == 2.5 and w["steady"] == 0.8 and w["drifting"] == 0.2 and w["failing"] == 0.0
    # the newcomer is scored against the established pool, capped at 2.5, shrunk with N0 = 20
    lam = 2 / (2 + 20)
    assert w["sparse"] == pytest.approx(round(lam * 2.5 + (1 - lam), 6)) and 0.8 <= w["sparse"] <= 1.5
    snap = v["drifting"].snapshot()
    assert snap["murphy"]["uncertainty"] == 0.0 and snap["paired"] == 30 and snap["bss_counts"] is True


def test_the_softmax_averages_one_inside_the_bands(settings: Settings) -> None:
    wide = dataclasses.replace(policy(settings), bands={})  # no clamping
    close = [*obs("closing_sharp", range(40), 0.6, 1.0), *obs("a", range(40), 0.62, 1.0, clv=0.5), *obs("b", range(40), 0.6, 1.0, clv=0.2), *obs("c", range(40), 0.59, 1.0, clv=0.0)]
    weights = cal.evaluate(close, wide).weights
    scores = {"a": -(0.38 ** 2) / 0.1 + 2 * 0.5, "b": -(0.4 ** 2) / 0.1 + 2 * 0.2, "c": -(0.41 ** 2) / 0.1}
    top = max(scores.values())
    total = sum(math.exp(s - top) for s in scores.values())
    assert weights == pytest.approx({k: round(math.exp(s - top) / total * 3, 6) for k, s in scores.items()})
    assert sum(weights.values()) == pytest.approx(3.0, abs=1e-5)  # mean 1
    hot = dataclasses.replace(wide, temperature=0.01)
    sharper = cal.evaluate(close, hot).weights
    assert max(sharper.values()) - min(sharper.values()) > max(weights.values()) - min(weights.values())  # a lower temperature separates more


def test_clv_benches_a_long_record_and_pins_win(settings: Settings) -> None:
    bleeding = [*obs("closing_sharp", range(45), 0.6, 1.0), *obs("bleeder", range(45), 0.62, 1.0, clv=-3.5)]
    v = cal.evaluate(bleeding, policy(settings)).verdicts[0]
    assert v.status is cal.Status.BENCHED and "CLV -3.50% over 45" in v.reason and v.weight == 0.0
    short = cal.evaluate(bleeding[:30] + bleeding[45:75], policy(settings)).verdicts[0]
    assert short.status is cal.Status.PROBATION  # 30 predictions: under the 40 the CLV bench needs, but under -1.5%
    pinned = cal.evaluate(fleet(), policy(settings), pins={"failing": 0.9, "elo": 0.5})
    by = {x.model: x for x in pinned.verdicts}
    assert by["failing"].weight == 0.9 and by["failing"].pinned and "pinned by an administrator at 0.9" in by["failing"].reason
    assert pinned.weights["elo"] == 0.5  # a pin on a model with no settled predictions still holds


def test_promotions_and_demotions_count_moves_through_the_lifecycle(settings: Settings) -> None:
    first = cal.evaluate(fleet(), policy(settings))
    assert (first.promoted, first.demoted) == (1, 2)  # from ACTIVE: poisson up; drifting, failing down
    again = cal.evaluate(fleet(), policy(settings), previous_status={v.model: v.status.value for v in first.verdicts})
    assert (again.promoted, again.demoted) == (0, 0)
    recovered = cal.evaluate(fleet(), policy(settings), previous_status={"steady": "BENCHED", "poisson": "ALPHA_BOOSTED", "drifting": "PROBATION", "failing": "BENCHED"})
    assert (recovered.promoted, recovered.demoted) == (1, 0)


def test_a_benched_model_loses_its_veto_in_pillar_one(settings: Settings) -> None:
    from tests.test_ultra_vetting import inputs, leg  # noqa: PLC0415 - the fortress's own fixtures

    rule = dataclasses.replace(fortress.FortressPolicy.from_settings(settings), min_models=2)
    dissent = [leg(p=0.46, market_p=0.40, retail=2.45)]  # the market's own model prices it at -2%
    voting = fortress.pillar_1(inputs(dissent, weights={"market": 1.0}), rule, {})[0]
    assert voting.status is fortress.Status.FAIL and voting.reason.endswith("EV <= 0 under market (veto)")
    benched = fortress.pillar_1(inputs(dissent, weights={"market": 0.0}), rule, {})[0]
    assert benched.status is fortress.Status.PASS and benched.metrics["legs"][0]["benched"] == ["market"]


def test_the_weekly_run_is_on_the_beat_schedule() -> None:
    from app.core.celery_app import ZonedCrontab, celery_app  # noqa: PLC0415

    entry = celery_app.conf.beat_schedule["model-recalibration"]
    s = get_settings()
    assert entry["task"] == "calibration.recalibrate" and "app.workers.calibration_tasks" in celery_app.conf.include
    assert entry["schedule"]._orig_day_of_week == s.TWIN_RECALIBRATION_DAY_OF_WEEK and entry["schedule"].zone == s.ORACLE_TIMEZONE
    weekly = pickle.loads(pickle.dumps(ZonedCrontab(minute=0, hour=0, day_of_week="sun", zone="Asia/Kolkata")))  # beat pickles its schedule
    assert weekly._orig_day_of_week == "sun" and weekly.zone == "Asia/Kolkata"
    assert "feedback-recalibrate" not in celery_app.conf.beat_schedule  # one publisher of pillar 1's weights


# ================================================================ the service
async def seed(sessions: async_sessionmaker[AsyncSession], rows: Sequence[cal.Observation], *, now: datetime = NOW) -> None:
    """The feedback rows behind ``rows`` (a user, a bet and one leg per distinct leg id, for the foreign keys)."""
    async with sessions() as session:
        user = User(username=f"cal_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.flush()
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("100"), placed_at=now, status="WON")
        session.add(bet)
        leg_ids: dict[str, uuid.UUID] = {}
        for i, leg in enumerate(sorted({r.leg_id for r in rows})):
            leg_ids[leg] = uuid.uuid4()
            session.add(UserPlacedLeg(id=leg_ids[leg], bet_id=bet.id, position=i, fixture_id=leg, home="H", away="A", market="Match Odds", selection="HOME", odds=D("2"), result="WON"))
        await session.flush()
        for r in rows:
            session.add(ModelPredictionFeedback(
                id=uuid.uuid4(), bet_id=bet.id, leg_id=leg_ids[r.leg_id], fixture_id=r.leg_id, market="Match Odds", selection="HOME", model_name=r.model,
                predicted_prob=r.predicted, actual_outcome=r.outcome, brier_score=r.brier, log_loss=0.5, clv_pct=r.clv_pct, details={},
                created_at=now - timedelta(days=r.age_days),
            ))
        await session.commit()


async def stream(redis: Redis, settings: Settings) -> list[SentinelAlert]:
    return [decode(fields["a"]) for _, fields in await redis.xrange(SentinelKeys(settings).stream)]


@pytest.mark.asyncio
async def test_a_run_records_publishes_atomically_and_pages_drift(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    # no settled predictions: a run is recorded, nothing is published, the weights in force stay
    await redis.hset(weights_key(settings), mapping={"poisson": "1.4"})
    empty = await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    assert not empty.published and "weights in force stay" in (empty.note or "") and await model_weights(redis, settings) == {"poisson": 1.4}

    await seed(sessions, fleet())
    run = await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    expected = cal.evaluate(fleet(), policy(settings), previous_weights={"poisson": 1.4}).weights
    assert run.published and run.published_weights == pytest.approx(expected) and run.models_evaluated == 5 and (run.models_promoted, run.models_demoted) == (1, 2)
    assert await model_weights(redis, settings) == pytest.approx(expected)  # the hash pillar 1 reads, replaced whole
    meta = json.loads(await redis.get(weights_meta_key(settings)))
    assert meta["run_id"] == str(run.id) and meta["status"]["failing"] == "BENCHED" and meta["developer_credit"] == "Amit Ashok Kumar Patnaik"
    async with sessions() as session:
        audits = {a.model_name: a for a in (await session.execute(select(ModelWeightAudit).where(ModelWeightAudit.run_id == run.id))).scalars()}
        assert set(audits) == {"poisson", "steady", "drifting", "failing", "sparse"}
        assert audits["poisson"].previous_weight == 1.4 and audits["poisson"].brier_skill_score == pytest.approx(0.75) and audits["poisson"].paired_count == 30
        assert audits["failing"].new_weight == 0.0 and audits["failing"].status == "BENCHED" and audits["drifting"].reliability is not None
        stored = await session.get(ModelRecalibrationRun, run.id)
        assert stored.benchmark_model == "closing_sharp" and stored.benchmark_brier == pytest.approx(0.16) and stored.parameters["temperature"] == 0.1
    alerts = await stream(redis, settings)
    drift = [a for a in alerts if a.kind is AlertKind.MODEL_DRIFT]
    assert len(drift) == 1 and drift[0].severity.value == "WARNING" and "drifting probation" in drift[0].title and "failing benched" in drift[0].title
    assert len([a for a in alerts if a.kind is AlertKind.MODEL_RECALIBRATED]) == 2

    # the same evidence again: the states hold, nothing drifts anew
    second = await model_calibrator.recalibrate(sessions, redis, settings, NOW + timedelta(minutes=1), RecalibrationTrigger.SCHEDULED)
    assert (second.models_promoted, second.models_demoted) == (0, 0)
    assert len([a for a in await stream(redis, settings) if a.kind is AlertKind.MODEL_DRIFT]) == 1
    async with sessions() as session:
        latest = await model_calibrator.latest_audits(session)
        assert latest["failing"].previous_status == "BENCHED" and latest["failing"].run_id == second.id


@pytest.mark.asyncio
async def test_one_run_at_a_time(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    await redis.set(model_calibrator.lock_key(settings), "someone-else", ex=30)
    with pytest.raises(model_calibrator.RecalibrationBusy):
        await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    assert await redis.get(model_calibrator.lock_key(settings)) == "someone-else"  # never released by a run that did not take it
    await redis.delete(model_calibrator.lock_key(settings))
    await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    assert await redis.get(model_calibrator.lock_key(settings)) is None


@pytest.mark.asyncio
async def test_pins_survive_runs_and_the_reset_clears_everything(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    await seed(sessions, fleet())
    clock = iter(NOW + timedelta(minutes=i) for i in range(20))  # one run after another, as they happen
    await model_calibrator.recalibrate(sessions, redis, settings, next(clock), RecalibrationTrigger.SCHEDULED)
    loose = await model_calibrator.override(sessions, redis, settings, next(clock), model="steady", weight=1.7, reason="hand tuned for a test", pin=False, admin_id=None)
    assert loose.trigger_type == "MANUAL_OVERRIDE" and (await model_weights(redis, settings))["steady"] == 1.7
    await model_calibrator.recalibrate(sessions, redis, settings, next(clock), RecalibrationTrigger.SCHEDULED)
    assert (await model_weights(redis, settings))["steady"] == 0.8  # not pinned: the next run recomputes it
    await model_calibrator.override(sessions, redis, settings, next(clock), model="failing", weight=0.6, reason="retrained, give it a voice", pin=True, admin_id=None)
    after = await model_calibrator.recalibrate(sessions, redis, settings, next(clock), RecalibrationTrigger.SCHEDULED)
    assert after.published_weights["failing"] == 0.6 and await redis.hgetall(weight_pins_key(settings)) == {"failing": "0.6"}
    async with sessions() as session:
        pinned = (await model_calibrator.latest_audits(session))["failing"]
        assert pinned.status == "BENCHED" and "pinned by an administrator at 0.6" in pinned.status_reason  # its record is still the record
    benched = await model_calibrator.override(sessions, redis, settings, next(clock), model="poisson", weight=0.0, reason="feed outage, bench it", pin=False, admin_id=None)
    async with sessions() as session:
        assert (await session.execute(select(ModelWeightAudit.status).where(ModelWeightAudit.run_id == benched.id))).scalar_one() == "BENCHED"
    reset = await model_calibrator.reset(sessions, redis, settings, next(clock), reason="emergency: equal weights", admin_id=None)
    assert reset.trigger_type == "EMERGENCY_RESET" and await model_weights(redis, settings) == {}
    assert not await redis.exists(weight_pins_key(settings)) and not await redis.exists(weights_meta_key(settings))


@pytest.mark.asyncio
async def test_losses_blamed_on_the_models_trigger_a_run_once_per_cooldown(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    await seed(sessions, fleet())
    assert await model_calibrator.maybe_loss_trigger(sessions, redis, settings, NOW) is None  # no blamed losses
    async with sessions() as session:
        bet_id = (await session.execute(select(UserPlacedBet.id))).scalar_one()
        for i in range(settings.TWIN_RECALIBRATION_LOSS_TRIGGER_COUNT):
            session.add(SettlementRootCauseAudit(id=uuid.uuid4(), bet_id=bet_id, root_cause_tag="MODEL_UNDERESTIMATION", explanation="test", evidence={},
                                                 created_at=NOW - timedelta(hours=i)))
        await session.commit()
    run = await model_calibrator.maybe_loss_trigger(sessions, redis, settings, NOW)
    assert run is not None and run.trigger_type == "LOSS_THRESHOLD_TRIGGER" and run.published and "3 losses blamed" in (run.note or "")
    assert await model_calibrator.maybe_loss_trigger(sessions, redis, settings, NOW) is None  # the cooldown holds


# ================================================================ the API
def calibration_app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(calibration_api.router, prefix="/api/v1")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


@pytest.mark.asyncio
async def test_the_api(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    await seed(sessions, fleet())
    admin = User(id=uuid.uuid4(), username="admin", hashed_password="x", role="ADMIN")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=calibration_app(sessions, redis, settings, admin)), base_url="http://test") as client:
        before = (await client.get("/api/v1/twin/calibration/weights")).json()
        assert before["equal_weights"] is True and before["models"] == [] and before["benchmark"] == "closing_sharp" and before["developer_credit"] == "Amit Ashok Kumar Patnaik"
        run = (await client.post("/api/v1/twin/calibration/recalibrate")).json()
        assert run["trigger_type"] == "ON_DEMAND_ADMIN" and run["published"] and {a["model_name"] for a in run["audits"]} == {"poisson", "steady", "drifting", "failing", "sparse"}
        assert run["parameters"]["references"] == {"closing_sharp": 30, "ensemble": 30}
        now = (await client.get("/api/v1/twin/calibration/weights")).json()
        models = {m["model_name"]: m for m in now["models"]}
        assert models["poisson"]["status"] == "ALPHA_BOOSTED" and models["failing"]["weight_in_force"] == 0.0 and now["last_run"]["run_id"] == run["run_id"]
        history = (await client.get("/api/v1/twin/calibration/history", params={"model_name": "drifting"})).json()
        assert len(history) == 1 and history[0]["status"] == "PROBATION" and history[0]["new_weight"] == 0.2
        detail = (await client.get(f"/api/v1/twin/calibration/runs/{run['run_id']}")).json()
        assert len(detail["audits"]) == 5 and detail["benchmark_brier"] == pytest.approx(0.16)
        assert (await client.get(f"/api/v1/twin/calibration/runs/{uuid.uuid4()}")).status_code == 404
        assert [r["run_id"] for r in (await client.get("/api/v1/twin/calibration/runs")).json()] == [run["run_id"]]

        refused = await client.post("/api/v1/twin/calibration/override", json={"model_name": "closing_sharp", "weight": 1.0, "reason": "should not work"})
        assert refused.status_code == 422 and refused.json()["detail"]["reason"] == "REFERENCE_PREDICTOR"
        assert (await client.post("/api/v1/twin/calibration/override", json={"model_name": "poisson", "weight": 9.0, "reason": "far too high"})).json()["detail"]["reason"] == "WEIGHT_TOO_HIGH"
        assert (await client.post("/api/v1/twin/calibration/override", json={"model_name": "Bad Name", "weight": 1.0, "reason": "bad name here"})).status_code == 422
        pinned = (await client.post("/api/v1/twin/calibration/override", json={"model_name": "steady", "weight": 1.1, "reason": "trust it this week", "pin": True})).json()
        assert pinned["pinned"] is True and pinned["trigger_type"] == "MANUAL_OVERRIDE"
        assert (await client.get("/api/v1/twin/calibration/weights")).json()["pins"] == {"steady": 1.1}

        await redis.set(model_calibrator.lock_key(settings), "busy", ex=30)
        busy = await client.post("/api/v1/twin/calibration/recalibrate")
        assert busy.status_code == 409 and busy.json()["detail"]["reason"] == "RECALIBRATION_RUNNING"
        await redis.delete(model_calibrator.lock_key(settings))

        reset = (await client.post("/api/v1/twin/calibration/reset", json={"reason": "back to equal weights"})).json()
        assert reset["trigger_type"] == "EMERGENCY_RESET" and "equally" in reset["message"]
        assert (await client.get("/api/v1/twin/calibration/weights")).json()["equal_weights"] is True
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ModelRecalibrationRun)) == 3  # the run, the override, the reset

    no_redis = calibration_app(sessions, None, settings, admin)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=no_redis), base_url="http://test") as client:
        assert (await client.post("/api/v1/twin/calibration/recalibrate")).status_code == 503
