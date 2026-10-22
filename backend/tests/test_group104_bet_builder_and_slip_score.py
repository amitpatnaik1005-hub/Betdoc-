"""Group 104: the Bet Builder's Slip Score, 1-click auto-fix and Chef's Table (point b.5). Developed for Amit Ashok
Kumar Patnaik.

The rules are pure and checked directly (badges, the score's arithmetic and cap, the offender, the plates, the
scoreline link and stress test against the fitted matrix). End to end, on SQLite and PostgreSQL with the live market
in Redis (the workbench tests' fixtures): a trial fortress run records nothing, the inspection carries the badges and
the scoreline sync, the auto-fix swaps a negative-EV leg for a quoted alternative and records only the result, and the
Chef's Table serves recipe cards from Ashoka's vetted slips.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user, get_db
from app.api.v1 import oracle as oracle_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.builder import recipe
from app.domain.manual_parlay import cognitive_parlay_rater as rater
from app.domain.oracle.markets import MarketKind, MarketRef
from app.domain.oracle.match_model import dixon_coles_matrix
from app.models.digital_twin import TwinVettingAudit
from app.services import manual_parlay
from app.services.twin import vetting
from tests.test_backtest_and_parlay import app_for, make_user, pillars, redis, sessions, settings  # noqa: F401 - fixtures
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, intel_ok, seed

S = get_settings()


def _policy() -> tuple[rater.RaterPolicy, rater.BadgePolicy]:
    return rater.RaterPolicy.from_settings(S), rater.BadgePolicy.from_settings(S)


# ================================================================ the Slip Score (pure)
def test_the_tiers_and_the_cap_are_the_game_tiers() -> None:
    policy, _ = _policy()
    assert [name for name, _ in policy.tiers] == ["DIAMOND", "GOLD", "SILVER", "BRONZE"] and policy.fail_cap == 54.0
    assert [rater.tier_for(s, policy) for s in (100, 90, 89.99, 75, 74.99, 55, 54.99, 0)] == ["DIAMOND", "DIAMOND", "GOLD", "GOLD", "SILVER", "SILVER", "BRONZE", "BRONZE"]


def _evidence(**over: Any) -> rater.SlipEvidence:
    base: dict[str, Any] = {"fixtures": ["f1", "f2"], "margins": [0.03, 0.035], "score_sync": [True, True], "joint_ev": 0.09}
    return rater.SlipEvidence(**{**base, **over})


def test_badges_are_earned_from_what_was_measured() -> None:
    _, bp = _policy()
    clean = pillars(["PASS"] * 15)
    codes = lambda ev, p=clean: [b.code for b in rater.badges(p, ev, bp)]  # noqa: E731
    assert codes(_evidence()) == ["VIG_CRUSHER", "ZERO_CORRELATION", "SCORE_SYNCED", "EV_KING"]
    assert codes(_evidence(margins=[0.08, 0.09])) == ["HEAVY_JUICE", "ZERO_CORRELATION", "SCORE_SYNCED", "EV_KING"]
    assert codes(_evidence(margins=[0.05, 0.06])) == ["ZERO_CORRELATION", "SCORE_SYNCED", "EV_KING"]  # between the bars: neither
    assert "VIG_CRUSHER" not in codes(_evidence(margins=[0.02, None]))  # an unmeasured margin earns nothing either way
    assert codes(_evidence(fixtures=["f1", "f1"]))[:2] == ["VIG_CRUSHER", "SAME_MATCH_TAX"]
    assert "ZERO_CORRELATION" not in codes(_evidence(fixtures=["f1"], margins=[0.03], score_sync=[True]))  # a single has nothing to decorrelate
    failed_independence = pillars(["PASS"] * 11 + ["FAIL"] + ["PASS"] * 3)
    assert "ZERO_CORRELATION" not in codes(_evidence(), failed_independence)
    assert "SCORE_SYNCED" not in codes(_evidence(score_sync=[True, None]))  # a leg with no scoreline model cannot be synced
    assert "SCORE_SYNCED" not in codes(_evidence(score_sync=[True, False]))
    assert "EV_KING" not in codes(_evidence(joint_ev=0.069)) and "EV_KING" not in codes(_evidence(joint_ev=None))
    labels = {b.code: (b.label, b.points) for b in rater.badges(clean, _evidence(fixtures=["f1", "f1"], margins=[0.08, 0.09]), bp)}
    assert labels["SAME_MATCH_TAX"] == ("⚠️ Same-Match Tax", -18.0) and labels["HEAVY_JUICE"] == ("🪤 Heavy Juice Trap", -12.0)


def test_the_slip_score_adds_badges_clamps_and_keeps_a_veto_under_the_cap() -> None:
    policy, bp = _policy()
    good = rater.rate(pillars(["PASS"] * 10 + ["UNVERIFIED"] * 5), policy)  # 75 from the fortress
    scored = rater.slip_score(good, rater.badges(pillars(["PASS"] * 10 + ["UNVERIFIED"] * 5), _evidence(), bp), policy)
    assert scored.fortress_score == 75.0 and scored.score == 100.0 and scored.tier == "DIAMOND"  # 75 + 5 + 8 + 10 + 10 = 108, clamped
    plain = rater.slip_score(good, [], policy)
    assert (plain.score, plain.tier, plain.badges) == (75.0, "GOLD", ())
    trap = rater.slip_score(rater.rate(pillars(["PASS"] * 15), policy), rater.badges(pillars(["PASS"] * 15), _evidence(fixtures=["f1", "f1"], margins=[0.08, 0.09], score_sync=[None, None], joint_ev=0.01), bp), policy)
    assert trap.score == 70.0 and trap.tier == "SILVER"  # 100 - 18 - 12
    vetoed_pillars = pillars(["PASS"] * 14 + ["FAIL"])
    vetoed = rater.slip_score(rater.rate(vetoed_pillars, policy), rater.badges(vetoed_pillars, _evidence(), bp), policy)
    assert vetoed.score == 54.0 and vetoed.tier == "BRONZE" and vetoed.capped  # badges never lift a vetoed slip over the cap
    floor = rater.slip_score(rater.rate(pillars(["FAIL"] * 15), policy), rater.badges(pillars(["FAIL"] * 15), _evidence(fixtures=["a", "a"], margins=[0.2, 0.2]), bp), policy)
    assert floor.score == 0.0
    # pillar 15 (Never-Forget) vetoed, independence passed: the shield is still earned, and still cannot lift the cap
    assert {b["code"] for b in vetoed.as_dict()["badges"]} == {"VIG_CRUSHER", "ZERO_CORRELATION", "SCORE_SYNCED", "EV_KING"}
    assert vetoed.as_dict()["fortress_score"] == 54.0


# ================================================================ the auto-fix and the plates (pure)
def test_the_offender_is_the_leg_that_costs_the_slip() -> None:
    legs = ["f1|Match Odds|HOME", "f2|Match Odds|AWAY", "f1|Totals 2.5|OVER"]
    evs = {legs[0]: 0.04, legs[1]: 0.06, legs[2]: 0.01}
    assert recipe.choose_offender(legs, evs, {}, set(), False) == (legs[2], "a second leg on the same fixture (Same-Match Tax)")
    two = legs[:2]
    assert recipe.choose_offender(two, {legs[0]: -0.03, legs[1]: 0.06}, {}, set(), False)[0] == legs[0]  # type: ignore[index]
    assert recipe.choose_offender(two, {legs[0]: None, legs[1]: 0.06}, {}, set(), False) == (legs[0], "no measurable EV")
    assert recipe.choose_offender(two, evs, {legs[0]: 0.09, legs[1]: 0.05}, {"HEAVY_JUICE"}, False)[0] == legs[0]  # type: ignore[index]
    assert recipe.choose_offender(two, evs, {}, set(), True) == (legs[0], "the weakest leg of a slip a pillar vetoed")
    assert recipe.choose_offender(two, evs, {}, set(), False) is None  # nothing costs this slip


def _rated(key: str, legs: int, score: float, ev: float, p: float, odds: float) -> recipe.Rated:
    return recipe.Rated(key=key, legs=legs, score=score, joint_ev=ev, joint_probability=p, odds=odds, payload={})


def test_the_three_plates_each_serve_a_different_slip() -> None:
    table = [_rated("single", 1, 99, 0.12, 0.60, 1.9), _rated("double", 2, 88, 0.10, 0.31, 3.6), _rated("treble", 3, 80, 0.15, 0.18, 6.4),
             _rated("safe-double", 2, 70, 0.08, 0.45, 2.4)]
    plates = recipe.serve(table)
    assert [(code, r.key) for code, _, r in plates] == [("PARLAY_OF_THE_DAY", "double"), ("SAFE_FORTRESS", "single"), ("HIGH_YIELD", "treble")]
    assert [title for _, title, _ in plates] == ["#1 Handcrafted parlay of the day", "#2 Safe fortress", "#3 High-yield special"]
    assert [r.key for _, _, r in recipe.serve([_rated("only", 1, 60, 0.07, 0.5, 2.0)])] == ["only"]  # no multiple: the best slip still leads
    no_yield = recipe.serve([_rated("a", 2, 90, 0.1, 0.3, 3.0), _rated("b", 2, 80, 0.1, 0.4, 2.5), _rated("c", 2, 70, -0.01, 0.2, 9.0)])
    assert [r.key for _, _, r in no_yield] == ["a", "b"]  # a negative joint EV is never served, however long its odds
    vetoed = recipe.Rated(key="v", legs=2, score=99, joint_ev=0.3, joint_probability=0.9, odds=2.0, payload={}, vetoed=True)
    assert [r.key for _, _, r in recipe.serve([vetoed, _rated("ok", 2, 60, 0.05, 0.3, 3.0)])] == ["ok"]  # a vetoed slip is never served
    assert recipe.serve([]) == []


# ================================================================ a leg against its scorelines (pure)
def test_scoreline_link_and_stress_test_read_the_matrix() -> None:
    m = dixon_coles_matrix(1.7, 1.0)
    ref = MarketRef(MarketKind.TOTALS, 2.5)
    link = recipe.scoreline_link(m, ref, "OVER")
    assert link is not None and link["likeliest_score"] == "1-1" and link["result_at_likeliest"] == "LOST" and link["synced"] is False
    goals = np.arange(11)
    total = np.add.outer(goals, goals)
    assert link["leg_probability"] == pytest.approx(float(m[total > 2.5].sum()), abs=1e-6)
    home = recipe.scoreline_link(m, MarketRef(MarketKind.MATCH_ODDS), "HOME")
    assert home is not None and home["result_at_likeliest"] == "LOST"  # level at the likeliest score
    stress = {s["code"]: s for s in recipe.stress_test(m, ref, "OVER")}
    assert stress["LOW_SCORING"]["leg_pays"] == 0.0 and stress["OPEN"]["leg_pays"] == pytest.approx(1.0)  # 0-1 goals never go over; 4+ always do
    one_goal = np.abs(np.subtract.outer(goals, goals)) <= 1
    expected = float(m[one_goal & (total > 2.5)].sum() / m[one_goal].sum())
    assert stress["ONE_GOAL"]["leg_pays"] == pytest.approx(expected, abs=1e-6) and stress["ONE_GOAL"]["probability"] == pytest.approx(float(m[one_goal].sum()), abs=1e-6)
    assert recipe.scoreline_link(None, ref, "OVER") is None and recipe.stress_test(None, ref, "OVER") == []


# ================================================================ end to end (live market in Redis)


async def _market(redis: Redis, settings: Settings, client: httpx.AsyncClient) -> None:
    """Three fixtures: one priced fairly, one where the retail HOME price is far under fair, one where it is far over."""
    # The consensus is the median de-vigged book, so books near the sharp price make an over-priced retail line real value
    await seed(redis, settings, "fx-fair", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)  # Group 77's vetted single at 1xBet 2.45
    await seed(redis, settings, "fx-bad", "Everton", "Fulham", sharp={"HOME": 2.00, "DRAW": 3.50, "AWAY": 3.90},
               soft={"williamhill": {"HOME": 1.95, "DRAW": 3.60, "AWAY": 4.00}, "onexbet": {"HOME": 1.62, "DRAW": 3.40, "AWAY": 3.80},
                     "parimatch": {"HOME": 1.60, "DRAW": 3.40, "AWAY": 3.80}})
    await seed(redis, settings, "fx-good", "Brighton", "Wolves", sharp={"HOME": 2.20, "DRAW": 3.30, "AWAY": 3.30},
               soft={"williamhill": {"HOME": 2.10, "DRAW": 3.40, "AWAY": 3.50}, "betway": {"HOME": 2.12, "DRAW": 3.40, "AWAY": 3.45},
                     "onexbet": {"HOME": 2.62, "DRAW": 3.10, "AWAY": 3.00}, "parimatch": {"HOME": 2.58, "DRAW": 3.10, "AWAY": 3.00}})
    now = datetime.now(UTC)
    for fixture in ("fx-fair", "fx-bad", "fx-good"):
        assert (await client.put(f"/api/v1/twin/intel/{fixture}", json=intel_ok(now).model_dump(mode="json", exclude_none=True))).status_code == 200


async def _audits(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int((await session.execute(select(func.count()).select_from(TwinVettingAudit))).scalar_one())


@pytest.mark.asyncio
async def test_a_trial_records_nothing_and_the_inspection_carries_badges(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        legs = ["fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"]
        run = await manual_parlay.trial(sessions, redis, settings, user.id, leg_ids=legs, kind="DOUBLE", book="1xbet", bankroll=None, now=datetime.now(UTC))
        assert await _audits(sessions) == 0 and len(run.rating.breakdown) == 15 and run.rating.fortress_score is not None
        inspected = (await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": legs, "kind": "DOUBLE", "skin": "one_xbet", "bankroll_inr": "100000"})).json()
        assert await _audits(sessions) == 1
        rating = inspected["rating"]
        codes = {b["code"] for b in rating["badges"]}
        assert "ZERO_CORRELATION" in codes and "SAME_MATCH_TAX" not in codes
        assert rating["score"] == pytest.approx(min(100.0, rating["fortress_score"] + sum(b["points"] for b in rating["badges"])), abs=0.01) or rating["capped"]
        sync = inspected["score_sync"]
        assert set(sync) == set(legs) and all(re_score(s["likeliest_score"]) for s in sync.values())  # football: fitted scorelines
        same = (await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": ["fx-fair|Match Odds|HOME", "fx-fair|Match Odds|AWAY"], "kind": "DOUBLE",
                                                                         "skin": "one_xbet", "bankroll_inr": "100000"})).json()
        assert "SAME_MATCH_TAX" in {b["code"] for b in same["rating"]["badges"]} and same["rating"]["tier"] == "BRONZE"


def re_score(score: str) -> bool:
    import re  # noqa: PLC0415

    return re.fullmatch(r"\d+-\d+", score) is not None


@pytest.mark.asyncio
async def test_auto_fix_swaps_the_negative_leg_and_records_only_the_result(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        body = {"leg_ids": ["fx-fair|Match Odds|HOME", "fx-bad|Match Odds|HOME"], "kind": "DOUBLE", "skin": "one_xbet", "bankroll_inr": "100000"}
        fixed = (await client.post("/api/v1/manual-parlay/auto-fix", json=body)).json()
        assert fixed["replaced"]["leg_id"] == "fx-bad|Match Odds|HOME" and fixed["replaced"]["reason"].startswith("negative EV")
        assert fixed["tried"] and all(not t["leg_id"].startswith(("fx-fair|", "fx-bad|")) for t in fixed["tried"])  # never the slip's own fixtures
        assert fixed["improved"] is True and fixed["after"]["score"] > fixed["before"]["score"]
        assert fixed["replacement"]["leg_id"].startswith("fx-good|") and fixed["replacement"]["ev"] > 0
        assert fixed["inspection"]["audit"]["leg_ids"] == ["fx-fair|Match Odds|HOME", fixed["replacement"]["leg_id"]]
        assert await _audits(sessions) == 1  # the trials left nothing; only the kept slip's inspection is recorded
        healthy = (await client.post("/api/v1/manual-parlay/auto-fix", json={**body, "leg_ids": ["fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"]})).json()
        assert healthy["improved"] is False and healthy["reason"] in ("NOTHING_TO_FIX", "NO_BETTER_LEG")
        assert (await client.post("/api/v1/manual-parlay/auto-fix", json={**body, "skin": "betway"})).status_code == 422


@pytest.mark.asyncio
async def test_the_chefs_table_serves_recipe_cards_from_vetted_slips(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
    app = FastAPI()
    app.include_router(oracle_api.router, prefix="/api/v1/oracle")
    app.state.redis = redis

    async def db():  # noqa: ANN202
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        table = (await client.get("/api/v1/oracle/chef-table", params={"bankroll_inr": "100000"})).json()
        assert table["plates"], table
        first = table["plates"][0]
        assert first["title"].startswith("#1") and first["rating"]["tier"] in ("DIAMOND", "GOLD", "SILVER", "BRONZE")
        # fx-bad HOME is +EV at Pinnacle (Ashoka may count it) but far under fair at the retail books the table rates at
        assert all(leg["leg_id"] != "fx-bad|Match Odds|HOME" for p in table["plates"] for leg in p["legs"])
        assert all(p["joint_ev"] > 0 and not any(r["status"] == "FAIL" for r in p["rating"]["breakdown"]) for p in table["plates"])
        card = first["recipe"][0]
        assert card["why"]["edge"] == pytest.approx(card["why"]["fair_probability"] * card["price"] - 1, abs=1e-6)
        assert re_score(card["scoreline"]["likeliest_score"]) and [s["code"] for s in card["stress_test"]] == ["LOW_SCORING", "ONE_GOAL", "OPEN"]
        assert table["thresholds"]["paths"] == settings.ASHOKA_MC_PATHS and isinstance(table["rejected"], dict)
        again = (await client.get("/api/v1/oracle/chef-table", params={"bankroll_inr": "100000"})).json()
        assert again["generated_at"] == table["generated_at"]  # served from the per-user cache
        assert await _audits(sessions) == 0  # the table re-rates slips as trials: nothing recorded
        app.state.redis = None
        assert (await client.get("/api/v1/oracle/chef-table")).status_code == 503


def test_the_new_routes_are_signed_in_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    assert paths["/api/v1/oracle/chef-table"]["get"].get("security") and paths["/api/v1/manual-parlay/auto-fix"]["post"].get("security")


def test_vetting_splits_assess_from_vet() -> None:
    import inspect as pyinspect  # noqa: PLC0415

    source = pyinspect.getsource(vetting.vet)
    assert "assess(" in source and "session.add(audit)" in source and "session.add" not in pyinspect.getsource(vetting.assess)
