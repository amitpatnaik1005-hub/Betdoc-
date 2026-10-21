"""Group 102: BETDOC SCORE PREDICTION (point b.16). Developed for Amit Ashok Kumar Patnaik.

The maths is checked against first principles (Dixon-Coles cells, settlement identities, a fit that reproduces its
targets, an exact chase), the cricket phase accounting against the innings distribution, and the /prediction contract
on SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set), with the stored-fixture path reading Group 101 rows.
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_db
from app.api.v1 import score_prediction as api
from app.core.config import get_settings
from app.domain.oracle.markets import MarketKind, MarketRef
from app.domain.oracle.match_model import MarketTargets, dixon_coles_matrix
from app.domain.prediction import score_predictor as sp
from app.domain.sports.context import MatchContext
from app.domain.sports.registry import SportAdapterRegistry
from app.models import Base
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.models.sports_config import LeagueRegistry, SportConfiguration
from app.services import pre_match_odds_service

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
TABLES = [SportConfiguration.__table__, LeagueRegistry.__table__, PreMatchOddsSnapshotModel.__table__]
SHAPE = {"heatmap_goals": 5, "top": 3}
S = get_settings()


def _priced(p: sp.FootballPrediction, market: str, selection: str) -> sp.PricedSelection:
    return next(m for m in p.markets if m.market == market and m.selection == selection)


# ================================================================ football (pure)
def test_heatmap_is_the_exact_matrix_corner_and_says_what_it_leaves_out() -> None:
    p = sp.football_from_xg(1.45, 1.15, **SHAPE)
    full = dixon_coles_matrix(1.45, 1.15, -0.13)
    assert len(p.heatmap) == 6 and all(len(r) == 6 for r in p.heatmap)
    assert p.heatmap[2][1] == pytest.approx(full[2, 1], abs=1e-6)  # not re-normalised to the 6x6 corner
    assert p.heatmap_mass == pytest.approx(full[:6, :6].sum(), abs=1e-5) and p.heatmap_mass < 1.0
    assert p.home_win + p.draw + p.away_win == pytest.approx(1.0, abs=1e-5)
    probs = [s.probability for s in p.top_scorelines]
    assert probs == sorted(probs, reverse=True) and len(probs) == 3
    best = np.unravel_index(np.argmax(full[:10, :10]), (10, 10))
    assert p.top_scorelines[0].label == f"{best[0]}-{best[1]}"


def test_dixon_coles_moves_mass_between_the_low_scores() -> None:
    """A negative rho lifts 0-0 and 1-1 and lowers 1-0 and 0-1 against independent Poisson."""
    dc, indep = sp.football_from_xg(1.0, 1.0, rho=-0.13, **SHAPE), sp.football_from_xg(1.0, 1.0, rho=0.0, **SHAPE)
    assert dc.heatmap[0][0] > indep.heatmap[0][0] and dc.heatmap[1][1] > indep.heatmap[1][1]
    assert dc.heatmap[1][0] < indep.heatmap[1][0] and dc.heatmap[0][1] < indep.heatmap[0][1]


def test_more_home_goals_mean_more_home_wins() -> None:
    assert sp.football_from_xg(2.5, 1.0, **SHAPE).home_win > sp.football_from_xg(1.0, 1.0, **SHAPE).home_win


def test_every_market_settles_cell_by_cell() -> None:
    p = sp.football_from_xg(1.6, 1.1, **SHAPE)
    h, d, a = p.home_win, p.draw, p.away_win
    q = _priced(p, "Asian Handicap -0.25", "HOME")  # half on -0.5, half on 0
    assert (q.win, q.half_loss, q.loss) == pytest.approx((h, d, a), abs=1e-5) and q.push == 0 and q.half_win == 0
    dnb = _priced(p, "Draw No Bet", "HOME")
    assert (dnb.win, dnb.push, dnb.loss) == pytest.approx((h, d, a), abs=1e-5)
    assert _priced(p, "Double Chance", "1X").win == pytest.approx(h + d, abs=1e-5)
    over = _priced(p, "Totals 2.5", "OVER")
    assert over.win == pytest.approx(p.over[2.5], abs=1e-5) and over.fair_odds == pytest.approx(1 / over.win, rel=1e-3)
    two = _priced(p, "Totals 2", "OVER")  # a whole line pushes on exactly two goals
    m = dixon_coles_matrix(1.6, 1.1, -0.13)
    exactly_two = m[0, 2] + m[1, 1] + m[2, 0]
    assert two.push == pytest.approx(exactly_two, abs=1e-5)
    # fair odds: the price where the expected return is the stake, half results and pushes counted
    for q in p.markets:
        if q.fair_odds is not None:
            o = q.fair_odds
            assert q.win * o + q.half_win * (o + 1) / 2 + q.push + q.half_loss / 2 == pytest.approx(1.0, abs=2e-3), q.market
    assert _priced(p, "BTTS", "YES").win == pytest.approx(p.btts_yes, abs=1e-5)


def test_the_market_source_reproduces_its_targets() -> None:
    exact = sp.football_from_market(MarketTargets(0.48, 0.27, 0.25), **SHAPE)  # two rates, two free 1X2 numbers: exact
    assert (exact.home_win, exact.draw, exact.away_win) == pytest.approx((0.48, 0.27, 0.25), abs=1e-3)
    p = sp.football_from_market(MarketTargets(0.48, 0.27, 0.25, {2.5: 0.52}), **SHAPE)  # a third target: least squares
    residuals = [p.home_win - 0.48, p.draw - 0.27, p.away_win - 0.25, p.over[2.5] - 0.52]
    assert max(abs(r) for r in residuals) < 1e-2
    assert p.fit_error == pytest.approx(sum(r * r for r in residuals), rel=0.05)  # the residual is reported, not hidden
    assert p.source == "market" and p.detail["targets"]["totals_over"] == {"2.5": 0.52}
    unnormalised = sp.football_from_market(MarketTargets(0.96, 0.54, 0.50), **SHAPE)  # scaled targets: same fit
    assert unnormalised.home_win == pytest.approx(sp.football_from_market(MarketTargets(0.48, 0.27, 0.25), **SHAPE).home_win, abs=1e-4)
    with pytest.raises(sp.PredictionInputError):
        sp.football_from_market(MarketTargets(0.0, 0.5, 0.5), **SHAPE)
    with pytest.raises(sp.PredictionInputError):
        sp.football_from_xg(0.0, 1.0, **SHAPE)


def test_the_stats_source_is_the_soccer_adapters_grid() -> None:
    adapter = SportAdapterRegistry.get_adapter("soccer")
    p = sp.football_from_stats(adapter, {"attack_rating": 1.2}, {"defense_rating": 1.1}, None, **SHAPE)
    _, grid, _ = adapter.model({"attack_rating": 1.2}, {"defense_rating": 1.1}, MatchContext.parse({}))
    assert p.heatmap[1][1] == pytest.approx(grid.matrix[1, 1] / grid.matrix.sum(), abs=1e-6)
    assert p.source == "stats" and p.detail["consensus"]["model_count"] >= 3


def test_stored_totals_are_devigged_per_book_and_quarter_lines_skipped() -> None:
    books = {
        "a": {"outcomes": [{"name": "Over", "price": 1.9, "point": 2.5}, {"name": "Under", "price": 1.9, "point": 2.5},
                           {"name": "Over", "price": 1.8, "point": 2.25}, {"name": "Under", "price": 2.0, "point": 2.25}]},
        "b": {"outcomes": [{"name": "Over", "price": 2.0, "point": 2.5}, {"name": "Under", "price": 1.8, "point": 2.5}]},
        "c": {"outcomes": [{"name": "Over", "price": 2.0, "point": 3.5}]},  # one side only: no implied probability
    }
    b_over = (1 / 2.0) / (1 / 2.0 + 1 / 1.8)
    assert sp.de_vig_totals(books) == pytest.approx({2.5: (0.5 + b_over) / 2})
    with pytest.raises(sp.PredictionInputError):
        sp.market_targets_from_store({"outcomes": {"home": {"probability": 0.6}, "away": {"probability": 0.4}}}, {})


# ================================================================ cricket (pure)
CRICKET = SportAdapterRegistry.get_adapter("cricket")


@pytest.mark.parametrize("fmt", ["t20", "odi"])
def test_phase_runs_add_up_to_the_innings_mean(fmt: str) -> None:
    setup = CRICKET.match_setup({"batting_rating": 112}, {}, MatchContext.parse({"format": fmt}))
    inn = CRICKET.simulate_innings_distribution(setup, "home", 1)
    assert set(inn.phase_runs) == {"powerplay", "middle", "death"}
    assert sum(inn.phase_runs.values()) == pytest.approx(inn.mean, rel=1e-9)


def test_innings_projection_and_the_chase() -> None:
    p = sp.predict_cricket(CRICKET, {}, {}, {"format": "t20"}, coverage=0.8, chase_target=170)
    lo, hi = p.first_innings.interval
    assert lo < p.first_innings.median < hi and p.first_innings.powerplay_median is not None
    assert p.chase.win + p.chase.tie + p.chase.lose == pytest.approx(1.0, abs=1e-6) and p.chase.source == "stated"
    assert p.chase.win_with_super_over == pytest.approx(p.chase.win + p.chase.tie / 2, abs=1e-6)
    easy = sp.predict_cricket(CRICKET, {}, {}, {"format": "t20"}, coverage=0.8, chase_target=140).chase.win
    hard = sp.predict_cricket(CRICKET, {}, {}, {"format": "t20"}, coverage=0.8, chase_target=200).chase.win
    assert easy > p.chase.win > hard
    model = sp.predict_cricket(CRICKET, {}, {}, {"format": "odi"}, coverage=0.8)
    assert model.chase.target is None and model.chase.source == "model" and model.chase.win_with_super_over is None and model.overs == 50


def test_dew_helps_the_side_chasing_under_lights() -> None:
    dry = sp.predict_cricket(CRICKET, {}, {}, {"format": "t20", "is_night_match": True}, coverage=0.8, chase_target=170)
    wet = sp.predict_cricket(CRICKET, {}, {}, {"format": "t20", "is_night_match": True, "dew_intensity": "heavy"}, coverage=0.8, chase_target=170)
    assert wet.chase.win > dry.chase.win and wet.conditions["dew_impact_applied"] is True and dry.conditions["dew_impact_applied"] is False
    assert wet.first_innings.median == dry.first_innings.median  # dew is the second innings' (the side bowling second)


def test_a_test_match_is_not_projected_here() -> None:
    with pytest.raises(sp.PredictionInputError, match="limited-overs"):
        sp.predict_cricket(CRICKET, {}, {}, {"format": "test"}, coverage=0.8)


# ================================================================ tennis, basketball, MMA (pure)
def test_other_sports_are_regrouped_from_their_adapters() -> None:
    t = sp.predict_tennis(SportAdapterRegistry.get_adapter("tennis"), {"elo": 2100}, {"elo": 1950}, None)
    assert sum(t["set_scores"].values()) == pytest.approx(1.0, abs=1e-3) and t["player_a_win"] > 0.5
    assert 0 < t["tiebreak_in_match"] < 1 and t["expected_total_games"] > 0
    b = sp.predict_basketball(SportAdapterRegistry.get_adapter("basketball"), {}, {}, None, coverage=0.8, spread_line=-2.5, total_line=227)
    assert b["spread"]["home_cover"] + b["spread"]["push"] + b["spread"]["away_cover"] == pytest.approx(1.0, abs=1e-5)
    assert b["total"]["push"] > 0  # a whole-number total can land exactly
    assert b["projected_home_points"] + b["projected_away_points"] == pytest.approx(b["projected_total"], abs=0.2)
    m = sp.predict_mma(SportAdapterRegistry.get_adapter("mma"), {}, {}, None)
    assert sum(v for v in m["method"].values() if v is not None) == pytest.approx(1.0, abs=1e-3)
    assert m["rounds_over"]["0.5"] >= m["rounds_over"]["1.5"] >= m["rounds_over"]["2.5"]


# ================================================================ the /prediction contract
@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    else:
        if not TEST_POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES))
        await engine.dispose()


def _odds_event(match_id: str, *, draw: bool = True) -> dict[str, Any]:
    kickoff = (datetime.now(UTC) + timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    h2h = [{"name": "Arsenal", "price": 2.05}, {"name": "Chelsea", "price": 3.6}] + ([{"name": "Draw", "price": 3.5}] if draw else [])
    return {"id": match_id, "commence_time": kickoff, "home_team": "Arsenal", "away_team": "Chelsea", "bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": h2h},
                                        {"key": "totals", "outcomes": [{"name": "Over", "price": 1.95, "point": 2.5}, {"name": "Under", "price": 1.9, "point": 2.5}]}]}]}


@pytest_asyncio.fixture
async def client(sessions) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/prediction")

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    async with sessions() as session:
        await pre_match_odds_service.store_board(session, [_odds_event("epl-1")], "soccer_epl", datetime.now(UTC), S)
        await pre_match_odds_service.store_board(session, [_odds_event("no-draw", draw=False)], "soccer_epl", datetime.now(UTC), S)
        await pre_match_odds_service.store_board(session, [_odds_event("ipl-1")], "cricket_ipl", datetime.now(UTC), S)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


URL = "/api/v1/prediction"


async def test_football_sources(client: AsyncClient) -> None:
    market = await client.post(f"{URL}/football", json={"source": "market", "market": {"home": 0.5, "draw": 0.27, "away": 0.23, "totals_over": {"2.5": 0.5}}})
    assert market.status_code == 200
    body = market.json()
    assert body["source"] == "market" and len(body["heatmap"]) == S.PREDICTION_HEATMAP_GOALS + 1 and len(body["top_scorelines"]) == S.PREDICTION_TOP_SCORELINES
    assert body["over"]["2.5"] + body["under"]["2.5"] == pytest.approx(1.0) and body["btts_yes"] + body["btts_no"] == pytest.approx(1.0)
    assert {"Match Odds", "Totals 2.5", "Asian Handicap -0.25", "BTTS", "Double Chance", "Draw No Bet"} <= {m["market"] for m in body["markets"]}
    xg = await client.post(f"{URL}/football", json={"source": "xg", "xg": {"home": 1.7, "away": 0.9}, "rho": -0.1})
    assert xg.status_code == 200 and xg.json()["rho"] == -0.1
    stats = await client.post(f"{URL}/football", json={"source": "stats", "home_stats": {"attack_rating": 1.2}})
    assert stats.status_code == 200 and stats.json()["source"] == "stats"


@pytest.mark.parametrize("bad", [
    {"source": "market"},
    {"source": "xg", "xg": {"home": 1, "away": 1}, "market": {"home": 0.4, "draw": 0.3, "away": 0.3}},
    {"source": "stats", "rho": -0.1},
    {"source": "xg", "xg": {"home": 0, "away": 1}},
    {"source": "market", "market": {"home": 0.4, "draw": 0.3, "away": 0.3, "totals_over": {"2.3": 0.5}}},
    {"source": "market", "market": {"home": 1.2, "draw": 0.3, "away": 0.3}},
])
async def test_football_rejects_ambiguous_or_impossible_inputs(client: AsyncClient, bad: dict[str, Any]) -> None:
    assert (await client.post(f"{URL}/football", json=bad)).status_code == 422


async def test_a_stored_fixture_is_predicted_from_its_own_board(client: AsyncClient) -> None:
    res = await client.get(f"{URL}/football/pre-match/epl-1")
    assert res.status_code == 200
    body = res.json()
    assert body["fixture"]["home_team"] == "Arsenal" and body["fixture"]["books"] == 1
    implied = [1 / 2.05, 1 / 3.5, 1 / 3.6]
    assert body["home_win"] == pytest.approx(implied[0] / sum(implied), abs=5e-3)  # the stored consensus, reproduced
    over = (1 / 1.95) / (1 / 1.95 + 1 / 1.9)
    assert body["detail"]["targets"]["totals_over"]["2.5"] == pytest.approx(over, abs=1e-6)
    assert (await client.get(f"{URL}/football/pre-match/ipl-1")).status_code == 422  # not football
    assert (await client.get(f"{URL}/football/pre-match/no-draw")).status_code == 409  # no three-way consensus
    assert (await client.get(f"{URL}/football/pre-match/none")).status_code == 404


async def test_cricket_and_the_other_sports(client: AsyncClient) -> None:
    res = await client.post(f"{URL}/cricket", json={"home_stats": {"batting_rating": 105}, "context": {"format": "t20"}, "chase_target": 175})
    assert res.status_code == 200
    body = res.json()
    assert body["chase"]["target"] == 175 and set(body["first_innings"]["phase_expected_runs"]) == {"powerplay", "middle", "death"}
    assert (await client.post(f"{URL}/cricket", json={"context": {"format": "test"}})).status_code == 422
    assert (await client.post(f"{URL}/cricket", json={"chase_target": 0})).status_code == 422
    assert (await client.post(f"{URL}/tennis", json={})).json()["prediction"]["best_of_sets"] == 3
    bball = await client.post(f"{URL}/basketball", json={"spread_line": -4.5, "total_line": 221.5})
    assert bball.status_code == 200 and "spread" in bball.json()["prediction"]
    assert (await client.post(f"{URL}/mma", json={})).json()["sport_key"] == "combat"


async def test_a_deactivated_sport_is_refused(client: AsyncClient, sessions) -> None:
    assert (await client.post(f"{URL}/tennis", json={})).status_code == 200  # seeds the catalogue
    async with sessions() as session:
        await session.execute(update(SportConfiguration).where(SportConfiguration.sport_key == "tennis").values(is_active=False))
        await session.commit()
    assert (await client.post(f"{URL}/tennis", json={})).status_code == 409


def test_prediction_is_mounted_for_signed_in_users_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path, verb in ((f"{URL}/football", "post"), (f"{URL}/football/pre-match/{{match_id}}", "get"), (f"{URL}/cricket", "post"),
                       (f"{URL}/tennis", "post"), (f"{URL}/basketball", "post"), (f"{URL}/mma", "post")):
        assert paths[path][verb].get("security"), path


def test_market_keys_match_the_oracle_board() -> None:
    """The board keys its legs by MarketRef.key; a score-backed leg is found by that exact key."""
    keys = {m.market for m in sp.football_from_xg(1.3, 1.2, **SHAPE).markets}
    assert MarketRef(MarketKind.TOTALS, 2.5).key in keys and MarketRef(MarketKind.ASIAN_HANDICAP, -0.75).key in keys
    assert math.isclose(len(sp.TOTAL_LINES), 21) and len(sp.HANDICAP_LINES) == 25
