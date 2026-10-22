"""Group 107: live smallcases (point b.8; ASHOKA's market, KUMBHA's sizing) and the Infinity / Flexi view. Developed for
Amit Ashok Kumar Patnaik.

Pure: each theme takes only what its rule allows (one leg per fixture, at most SMALLCASE_MAX_LEGS, at or over the
minimum EV) and is empty with its reason otherwise; KUMBHA sizes every leg and the basket stays under its ceiling;
the risk figures are the exact expectation, the product of the losses, and seeded (stable) simulated VaR / CVaR, with
the HHI, risk-parity and component-VaR functions of ``app.domain.risk``; the components view is the code's own
catalogue, every implementation imported, and claims a use only where the smallcases make one.

End to end, on SQLite and PostgreSQL with the live market in Redis: the European and underdog baskets fill from the
seeded Premier League fixtures and the cricket, in-play and multi-sport baskets are empty with their reasons; the scan
is shared and a rebalance replaces it; the slip as a smallcase leaves out the leg priced under fair and the second leg
on one fixture; the components and ticker answer; no Redis is a 503.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.v1 import smallcase as smallcase_api
from app.core.config import Settings, get_settings
from app.domain.cfo import growth_math as gm
from app.domain.smallcase import baskets as bk
from app.domain.smallcase import components as cmp
from app.domain.smallcase import portfolio as pf
from app.models import User
from app.models.tax_and_fx import CurrencyExchangeRate
from app.services.hive_registry import catalogue_by_key
from tests.test_backtest_and_parlay import TABLES, _sqlite_metadata, app_for, make_user, redis, settings  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market

S = get_settings()
D = Decimal
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
EXTRA = [CurrencyExchangeRate.__table__]  # KUMBHA's skill and latch read ModelWeightAudit and CFOAdvisoryLog, already in TABLES
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


def _p(leg_id: str, *, ev: float = 0.05, p: float = 0.5, price: float = 2.1, sport_key: str = "soccer_epl", sport: str | None = "Football",
       in_play: bool = False, kind: str = "MATCH_ODDS") -> bk.Pick:
    fixture = leg_id.split("|", 1)[0]
    return bk.Pick(leg_id, fixture, f"{fixture} home vs away", "1X2: Home", "Match Odds", kind, sport_key, sport, "League", "2026-10-22T14:00:00+00:00",
                   in_play, "1xbet", price, p, ev, {"1xbet": price})


POLICY = bk.ThemePolicy(max_legs=3, min_ev=0.02, safe_min_probability=0.55, underdog_min_odds=2.5, european_leagues=("soccer_epl", "soccer_spain_la_liga"))


# ================================================================ the themes (pure)
def test_each_theme_takes_only_what_its_rule_allows() -> None:
    pool = [
        _p("a|Match Odds|HOME", ev=0.04, p=0.62, price=1.7), _p("a|Match Odds|DRAW", ev=0.09, p=0.28, price=3.9),  # one fixture, two legs
        _p("b|Match Odds|HOME", ev=0.06, p=0.58, price=1.83, sport_key="soccer_spain_la_liga"),
        _p("c|Match Odds|AWAY", ev=0.12, p=0.31, price=3.6, sport_key="soccer_brazil_campeonato"),
        _p("d|Match Odds|HOME", ev=0.07, p=0.52, price=2.06, sport_key="cricket_ipl", sport="Cricket"),
        _p("e|Match Odds|HOME", ev=0.15, p=0.45, price=2.55, in_play=True),
        _p("f|Match Odds|AWAY", ev=0.01, p=0.70, price=1.45),  # under the minimum EV: in no basket
        _p("g|Match Odds|HOME", ev=0.05, p=0.40, price=2.62, sport_key="tennis_atp_paris", sport="Tennis"),
    ]
    take = {t.id: [p.leg_id for p in bk.choose(t, pool, POLICY)] for t in bk.THEMES}
    assert take["SAFE_COMPOUNDER"] == ["a|Match Odds|HOME", "b|Match Odds|HOME"]  # likeliest first, f is under the EV bar, e is in play
    assert take["CRICKET_POWERPLAY_EDGE"] == ["d|Match Odds|HOME"]
    assert take["EUROPEAN_FOOTBALL_FORTRESS"] == ["a|Match Odds|DRAW", "b|Match Odds|HOME"]  # one per fixture: a's best EV leg; c is Brazil
    assert take["HIGH_YIELD_UNDERDOG_BASKET"] == ["c|Match Odds|AWAY", "a|Match Odds|DRAW", "g|Match Odds|HOME"]  # at most max_legs, best EV first
    assert take["LIVE_MOMENTUM_ALPHA"] == ["e|Match Odds|HOME"]
    assert take["MULTI_SPORT_CHEFS_BASKET"] == ["c|Match Odds|AWAY", "d|Match Odds|HOME", "g|Match Odds|HOME"]  # the best leg of each sport
    assert all(len({leg.split("|")[0] for leg in legs}) == len(legs) for legs in take.values())
    one_sport = [p for p in pool if p.sport == "Football"]
    assert bk.choose(bk.THEMES_BY_ID["MULTI_SPORT_CHEFS_BASKET"], one_sport, POLICY) == []  # two sports at least
    assert all(t.empty and t.scope for t in bk.THEMES) and "powerplay" in bk.THEMES_BY_ID["CRICKET_POWERPLAY_EDGE"].scope.lower()
    assert "no momentum model runs" in bk.THEMES_BY_ID["LIVE_MOMENTUM_ALPHA"].scope
    assert bk.Pick.of(pool[0].as_dict()) == pool[0]


# ================================================================ sizing and risk (pure)
SIZING = gm.SizingPolicy.from_settings(S)


def test_kumbha_sizes_each_leg_and_the_basket_stays_under_its_ceiling() -> None:
    legs = [_p("a|Match Odds|HOME", ev=0.08, p=0.54, price=2.0), _p("b|Match Odds|HOME", ev=0.10, p=0.44, price=2.5)]
    sized = pf.size(legs, bankroll=D("100000"), drawdown=0.0, policy=SIZING, step=D("50"), basket_cap=1.0, skill=None, latched=False)
    for h, leg in zip(sized.holdings, legs, strict=True):
        assert h.full_kelly == pytest.approx(leg.ev / (leg.price - 1))  # the independent Kelly fraction
        expect = gm.size(h.full_kelly, leg.price, D("100000"), 0.0, SIZING, D("50"))
        assert h.fraction == pytest.approx(expect.fraction) and h.stake == expect.stake  # KUMBHA's own sizing, untouched under the ceiling
    capped = pf.size(legs, bankroll=D("100000"), drawdown=0.0, policy=SIZING, step=D("50"), basket_cap=0.004, skill=None, latched=False)
    assert capped.fraction == pytest.approx(0.004) and capped.scaled < 1.0 and capped.stake <= D("400")  # scaled alike under the ceiling
    assert [h.fraction / capped.fraction for h in capped.holdings] == pytest.approx([h.fraction / sized.fraction for h in sized.holdings])
    unfunded = pf.size(legs, bankroll=None, drawdown=0.0, policy=SIZING, step=D("50"), basket_cap=1.0, skill=None, latched=False)
    assert unfunded.stake == 0 and unfunded.fraction == pytest.approx(sized.fraction)  # the weights stand, no rupee figure
    halted = pf.size(legs, bankroll=D("100000"), drawdown=0.0, policy=SIZING, step=D("50"), basket_cap=1.0, skill=None, latched=True)
    assert halted.halted and halted.stake == 0 and halted.fraction == 0  # a latched drawdown halt stakes nothing


def test_the_risk_figures_are_exact_where_they_can_be_and_seeded_where_simulated() -> None:
    legs = [_p("a|Match Odds|HOME", ev=0.08, p=0.54, price=2.0), _p("b|Match Odds|HOME", ev=0.10, p=0.44, price=2.5), _p("c|Match Odds|HOME", ev=0.03, p=0.6, price=1.72)]
    sized = pf.size(legs, bankroll=D("100000"), drawdown=0.0, policy=SIZING, step=D("50"), basket_cap=1.0, skill=None, latched=False)
    r = pf.risk(sized.holdings, 20_000)
    w = np.array(r["weights"])
    assert w.sum() == pytest.approx(1.0) and r["expected_return"] == pytest.approx(float(sum(w[i] * (legs[i].fair_probability * legs[i].price - 1) for i in range(3))), abs=1e-5)
    assert r["p_lose_all"] == pytest.approx(0.46 * 0.56 * 0.4, abs=1e-6) and r["hhi"] == pytest.approx(float(np.sum(w**2)), abs=1e-5)
    assert 0 < r["p_profit"] < 1 and r["cvar_95"] >= r["var_95"] >= 0
    assert sum(r["risk_parity_weights"]) == pytest.approx(1.0, abs=1e-6) and sum(r["var_share"]) == pytest.approx(1.0, abs=1e-5) and len(r["var_share"]) == 3
    assert pf.risk(sized.holdings, 20_000) == r  # the same basket draws the same paths
    parlay = pf.as_parlay(legs)
    assert parlay == {"odds": pytest.approx(2.0 * 2.5 * 1.72), "win_probability": pytest.approx(0.54 * 0.44 * 0.6), "ev": pytest.approx(0.54 * 0.44 * 0.6 * 8.6 - 1)}
    assert pf.as_parlay(legs[:1]) is None and pf.risk([], 100) == {}


# ================================================================ the components (pure)
def test_the_components_are_the_codes_own_and_claims_follow_the_code() -> None:
    cov = cmp.coverage(["MATCH_ODDS", "TOTALS"], 2)
    by = cov["by_kind"]
    assert {k: (v["catalogued"], v["implemented"]) for k, v in by.items()} == {
        "BET_TYPE": (28, 28), "MATH_MODEL": (59, 59), "RISK_MODEL": (24, 24), "STOP_LOSS": (5, 5), "FORTRESS_PILLAR": (15, 15)}
    registry = catalogue_by_key()
    assert set(cmp.BASKET_USES) <= set(registry) and set(cmp.STRUCTURES) <= set(registry) and set(cmp.MARKET_KINDS.values()) <= set(registry)
    rows = {r["key"]: r for r in cov["components"]}
    assert rows["risk.kelly_portfolio"]["used"]["scope"] == "BASKET" and rows["bet.match_winner_1x2"]["used"]["scope"] == "HOLDING"
    assert rows["bet.over_under_goals"]["used"]["scope"] == "HOLDING" and rows["bet.btts"]["used"] is None  # held right now, or not
    assert rows["pillar.15"]["used"]["scope"] == "INSPECTION" and rows["stop.trailing"]["used"]["scope"] == "ACCOUNT"
    assert rows["math.lstm"]["used"] is None and rows["math.lstm"]["implemented"] is True  # real and catalogued; nothing is claimed for it
    assert cov["totals"]["used"] == 6 + 2 + 2 + 15 + 5 and cov["totals"]["blueprint"] == 130
    assert not cmp.importable("app.domain.risk.var:no_such_function") and not cmp.importable("no.such.module:x")


# ================================================================ end to end
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = app_for(sessions, redis, settings, user)
    app.include_router(smallcase_api.router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_the_baskets_fill_from_the_live_market_and_say_why_when_empty(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        board = (await client.get("/api/v1/smallcase/baskets", params={"bankroll_inr": "100000"})).json()
        by = {b["id"]: b for b in board["baskets"]}
        assert [b["id"] for b in board["baskets"]] == [t.id for t in bk.THEMES] and board["bankroll_inr"] == "100000"
        europe, dogs = by["EUROPEAN_FOOTBALL_FORTRESS"], by["HIGH_YIELD_UNDERDOG_BASKET"]
        assert europe["status"] == "LIVE" and {h["fixture_id"] for h in europe["holdings"]} <= {"fx-fair", "fx-good"}
        assert "fx-good" in {h["fixture_id"] for h in europe["holdings"]} and all(h["ev"] >= settings.SMALLCASE_MIN_EV for h in europe["holdings"])
        assert all(h["book"] in ("parimatch", "1xbet", "stake") for h in europe["holdings"])  # retail prices only
        assert all(Decimal(h["stake_inr"]) > 0 for h in europe["holdings"]) and europe["fraction"] <= settings.SMALLCASE_MAX_BASKET_FRACTION + 1e-9
        assert Decimal(europe["expected_profit_inr"]) == (Decimal(europe["stake_inr"]) * Decimal(str(europe["risk"]["expected_return"]))).quantize(Decimal("0.01"))
        assert all(Decimal(h["stop_loss"]["floor_inr"]) < Decimal(h["stake_inr"]) for h in europe["holdings"]) and europe["stake_usd"] is None  # no fresh FX rate
        assert dogs["status"] == "LIVE" and all(h["price"] >= settings.SMALLCASE_UNDERDOG_MIN_ODDS for h in dogs["holdings"])
        shared = {h["leg_id"] for h in dogs["holdings"]} & {h["leg_id"] for h in europe["holdings"]}
        assert all("HIGH_YIELD_UNDERDOG_BASKET" in h["also_in"] for h in europe["holdings"] if h["leg_id"] in shared)
        for empty in ("CRICKET_POWERPLAY_EDGE", "LIVE_MOMENTUM_ALPHA", "MULTI_SPORT_CHEFS_BASKET"):
            assert by[empty]["status"] == "EMPTY" and by[empty]["holdings"] == [] and by[empty]["empty_reason"] == bk.THEMES_BY_ID[empty].empty
        assert "fx-bad|Match Odds|HOME" not in {h["leg_id"] for b in board["baskets"] for h in b["holdings"]}  # priced under fair at every retail book

        cached = json.loads(await redis.get(f"{settings.ARYABHATA_PREFIX}:smallcase:scan"))
        assert cached["as_of"] == board["as_of"] and 0 < await redis.ttl(f"{settings.ARYABHATA_PREFIX}:smallcase:scan") <= settings.SMALLCASE_SCAN_SECONDS
        again = (await client.get("/api/v1/smallcase/baskets")).json()
        assert again["as_of"] == board["as_of"]  # one shared scan
        fresh = (await client.post("/api/v1/smallcase/baskets/european_football_fortress/rebalance")).json()
        assert fresh["basket"]["id"] == "EUROPEAN_FOOTBALL_FORTRESS" and fresh["as_of"] > board["as_of"]  # rebuilt from a fresh read
        assert (await client.get("/api/v1/smallcase/baskets/NOPE")).status_code == 404

        ticker = (await client.get("/api/v1/smallcase/ticker")).json()["baskets"]
        assert [t["id"] for t in ticker] == [t.id for t in bk.THEMES] and next(t for t in ticker if t["id"] == "LIVE_MOMENTUM_ALPHA")["legs"] == 0
        comps = (await client.get("/api/v1/smallcase/components")).json()
        assert comps["totals"]["catalogued"] == 131 and comps["totals"]["implemented"] == 131 and comps["baskets_live"] >= 2
        assert next(r for r in comps["components"] if r["key"] == "bet.match_winner_1x2")["used"]["scope"] == "HOLDING"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, None, settings, user)), base_url="http://test") as client:
        assert (await client.get("/api/v1/smallcase/baskets")).status_code == 503


@pytest.mark.asyncio
async def test_the_slip_as_a_smallcase_leaves_out_what_kumbha_would_not_stake(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        legs = ["fx-fair|Match Odds|HOME", "fx-bad|Match Odds|HOME", "fx-good|Match Odds|HOME", "fx-good|Match Odds|AWAY"]
        r = (await client.post("/api/v1/smallcase/rebalance-slip", json={"leg_ids": legs, "skin": "one_xbet", "bankroll_inr": "100000"})).json()
        reasons = {e["leg_id"]: e["reason"] for e in r["excluded"]}
        assert reasons["fx-bad|Match Odds|HOME"] == "UNDER_FAIR" and reasons["fx-good|Match Odds|AWAY"] in ("UNDER_FAIR", "SAME_FIXTURE")
        held = [h["leg_id"] for h in r["holdings"]]
        assert set(held) == {"fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"} and all(h["book"] == "1xbet" for h in r["holdings"])
        priced = [h["price"] for h in r["holdings"]]
        assert r["book_label"] == "1xBet" and r["parlay"]["odds"] == pytest.approx(float(np.prod(priced)), rel=1e-4)  # the held legs as one multiple
        assert r["as_built"]["odds"] > r["parlay"]["odds"]  # the slip as built carries the left-out legs' prices too
        assert r["risk"]["p_profit"] > 0 and Decimal(r["stake_inr"]) > 0
        gone = (await client.post("/api/v1/smallcase/rebalance-slip", json={"leg_ids": ["fx-gone|Match Odds|HOME"], "skin": "betdoc"})).json()
        assert gone["holdings"] == [] and gone["excluded"][0]["reason"] == "NOT_QUOTED"
        assert (await client.post("/api/v1/smallcase/rebalance-slip", json={"leg_ids": legs, "skin": "betway"})).status_code == 422


# ================================================================ routes and the front end
def test_the_routes_are_signed_in_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path, method in (("/api/v1/smallcase/baskets", "get"), ("/api/v1/smallcase/baskets/{basket_id}", "get"), ("/api/v1/smallcase/baskets/{basket_id}/rebalance", "post"),
                         ("/api/v1/smallcase/ticker", "get"), ("/api/v1/smallcase/rebalance-slip", "post"), ("/api/v1/smallcase/components", "get")):
        assert paths[path][method].get("security"), path


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_smallcases_and_the_infinity_view_are_wired() -> None:
    oracle = (FRONTEND / "pages" / "TheOracle.tsx").read_text(encoding="utf-8")
    assert "<LiveSmallcaseTickerBar" in oracle and "<InfinityFlexiViewToggle" in oracle
    assert "<SmallcaseSlipRebalancer" in (FRONTEND / "components" / "builder" / "BetBuilder.tsx").read_text(encoding="utf-8")
    assert "<LiveSmallcaseTickerBar" in (FRONTEND / "pages" / "TheVault.tsx").read_text(encoding="utf-8")
    store = (FRONTEND / "store" / "useUIStore.ts").read_text(encoding="utf-8")
    assert "isInfinityView" in store and "toggleInfinityView" in store
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    assert re.search(r"hidden = .*isInfinityView", app) and "Exit Infinity" in app
    assert "isInfinityView" in (FRONTEND / "ui" / "kit.tsx").read_text(encoding="utf-8")  # the page drops its max width
    sources = [(FRONTEND / "lib" / "smallcase.ts").read_text(encoding="utf-8"), *(p.read_text(encoding="utf-8") for p in (FRONTEND / "components" / "smallcase").glob("*.tsx"))]
    for source in sources:
        for fake in ("Arsenal v Everton", "India v Australia", "130/130", "lucide-react"):
            assert fake not in source, fake
        assert not re.search(r"/\s*84(\.0+)?\b", source)
