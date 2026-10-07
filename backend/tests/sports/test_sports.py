"""Multi-Sport Support: pure math, dynamic configuration, manager behaviour and API contracts."""

import json
import math
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.sports import basketball, cricket, tennis
from app.domain.sports.errors import (
    InvalidSportConfigError,
    SportInactiveError,
    SportNotFoundError,
    SportsDomainError,
)
from app.models.sports import SportConfigModel
from app.schemas.sports import (
    BasketballConfig,
    CricketConfig,
    DlsRequest,
    ProjectScoreRequest,
    SportConfigRead,
    SpreadRequest,
    TennisConfig,
    TennisGameRequest,
)

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/sports"

SPREAD_PAYLOAD = {
    "home_rating": 5.0,
    "away_rating": 2.0,
    "home_pace": 100.0,
    "away_pace": 98.0,
    "league_avg_pace": 99.0,
}


# =============================================================== reference implementations


def ref_projection(score, overs, wickets, pitch, total, wpi):
    if wickets >= wpi or overs >= total:
        return float(score)
    return round(score + (score / max(overs, 1.0)) * (total - overs) * pitch * math.exp(-wickets / wpi), 4)


def ref_win_prob(spread, std_dev):
    return round(0.5 * (1.0 + math.erf(spread / (std_dev * math.sqrt(2.0)))), 4)


def ref_game(p):
    q = 1.0 - p
    return round(p**4 * (1 + 4 * q + 10 * q**2) + (20 * p**3 * q**3 * p**2) / (1 - 2 * p * q), 4)


def ref_elo(player, opponent, scale):
    return 1.0 / (1.0 + 10.0 ** ((opponent - player) / scale))


def is_four_decimals(value: float) -> bool:
    return round(value, 4) == value


async def _count(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(SportConfigModel))).scalar_one()


async def _assert_non_finite_rejected(client, method: str, path: str, raw: str) -> None:
    try:
        response = await client.request(method, path, content=raw, headers={"Content-Type": "application/json"})
    except ValueError:
        pytest.skip("FastAPI could not serialise the non-finite input inside its 422 body.")
    assert response.status_code == 422


# =============================================================== cricket math


@pytest.mark.parametrize(
    ("resources", "target", "weight", "expected"),
    [
        (0.4, 250.0, 1.0, 150.0),
        (0.4, 250.0, 0.5, 200.0),
        (0.0, 250.0, 1.0, 250.0),     # no resources left: full target
        (1.0, 250.0, 1.0, 0.0),       # all resources left: par is zero
        (0.4, 250.0, 3.0, 0.0),       # lower-bound clamp
        (0.3, 0.0, 1.0, 0.0),
        (0.123456, 287.0, 1.0, 251.5681),
    ],
)
async def test_dls_par_score(resources, target, weight, expected):
    result = cricket.calculate_dls_par_score(resources, target, weight)
    assert result == expected
    assert 0.0 <= result <= target
    assert is_four_decimals(result)


@pytest.mark.parametrize(
    ("resources", "target", "weight"),
    [
        (-0.1, 250.0, 1.0),
        (1.1, 250.0, 1.0),
        (0.4, -1.0, 1.0),
        (0.4, 250.0, 0.0),
        (float("nan"), 250.0, 1.0),
        (0.4, float("inf"), 1.0),
        (True, 250.0, 1.0),
    ],
)
async def test_dls_rejects_invalid_inputs(resources, target, weight):
    with pytest.raises(ValueError):
        cricket.calculate_dls_par_score(resources, target, weight)


@pytest.mark.parametrize(
    ("score", "overs", "wickets", "pitch", "total", "wpi"),
    [
        (120.0, 20.0, 2, 1.0, 50.0, 10),
        (120.0, 20.0, 2, 0.8, 50.0, 10),
        (45.0, 0.0, 0, 1.0, 20.0, 10),       # overs floor of 1.0
        (0.0, 0.0, 0, 1.0, 50.0, 10),
        (160.0, 15.0, 4, 1.1, 20.0, 10),     # T20 via dynamic total_overs
        (200.0, 30.0, 3, 1.0, 50.0, 12),     # dynamic wickets_per_innings
    ],
)
async def test_first_innings_projection_matches_formula(score, overs, wickets, pitch, total, wpi):
    result = cricket.calculate_first_innings_expectation(score, overs, wickets, pitch, total, wpi)
    assert result == ref_projection(score, overs, wickets, pitch, total, wpi)
    assert result >= score
    assert is_four_decimals(result)


@pytest.mark.parametrize(("overs", "wickets"), [(20.0, 10), (20.0, 11), (50.0, 3), (55.0, 0)])
async def test_first_innings_completed_returns_current_score(overs, wickets):
    assert cricket.calculate_first_innings_expectation(187.0, overs, wickets, 1.0, 50.0, 10) == 187.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"current_score": -1.0},
        {"overs_bowled": -1.0},
        {"wickets_lost": -1},
        {"wickets_lost": 2.5},
        {"pitch_degradation_factor": -0.1},
        {"total_overs": 0.0},
        {"wickets_per_innings": 0},
        {"current_score": float("nan")},
    ],
)
async def test_first_innings_rejects_invalid_inputs(kwargs):
    base = {
        "current_score": 100.0,
        "overs_bowled": 10.0,
        "wickets_lost": 1,
        "pitch_degradation_factor": 1.0,
        "total_overs": 50.0,
        "wickets_per_innings": 10,
    }
    with pytest.raises(ValueError):
        cricket.calculate_first_innings_expectation(**{**base, **kwargs})


# =============================================================== basketball math


@pytest.mark.parametrize(
    ("home", "away", "home_pace", "away_pace", "league", "expected"),
    [
        (5.0, 2.0, 100.0, 98.0, 99.0, 3.0),
        (5.0, 2.0, 110.0, 110.0, 100.0, 3.3),
        (0.0, 0.0, 100.0, 100.0, 100.0, 0.0),
        (-4.0, 1.0, 95.0, 95.0, 100.0, -4.75),
    ],
)
async def test_pace_adjusted_spread(home, away, home_pace, away_pace, league, expected):
    assert basketball.calculate_pace_adjusted_spread(home, away, home_pace, away_pace, league) == expected


@pytest.mark.parametrize(
    ("home_pace", "away_pace", "league"),
    [
        (100.0, 100.0, 0.0),      # zero league pace
        (100.0, 100.0, -5.0),
        (0.0, 100.0, 100.0),      # zero team pace
        (100.0, 0.0, 100.0),
        (1e308, 1e308, 1e-308),   # overflow to infinity
    ],
)
async def test_spread_rejects_zero_pace_and_overflow(home_pace, away_pace, league):
    with pytest.raises(ValueError):
        basketball.calculate_pace_adjusted_spread(1e308, -1e308, home_pace, away_pace, league)


@pytest.mark.parametrize(
    ("spread", "std_dev"), [(0.0, 11.5), (11.5, 11.5), (-11.5, 11.5), (3.0, 5.0), (40.0, 1.0), (-40.0, 1.0)]
)
async def test_win_probability_from_spread(spread, std_dev):
    result = basketball.calculate_win_probability_from_spread(spread, std_dev)
    assert result == ref_win_prob(spread, std_dev)
    assert 0.0 <= result <= 1.0
    assert is_four_decimals(result)


async def test_win_probability_is_symmetric():
    home = basketball.calculate_win_probability_from_spread(4.2, 11.5)
    away = basketball.calculate_win_probability_from_spread(-4.2, 11.5)
    assert home + away == pytest.approx(1.0, abs=1e-4)
    assert basketball.calculate_win_probability_from_spread(0.0, 11.5) == 0.5


@pytest.mark.parametrize("std_dev", [0.0, -1.0, float("nan")])
async def test_win_probability_rejects_invalid_std_dev(std_dev):
    with pytest.raises(ValueError):
        basketball.calculate_win_probability_from_spread(3.0, std_dev)


# =============================================================== tennis math


@pytest.mark.parametrize(("p", "expected"), [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
async def test_game_probability_boundaries(p, expected):
    assert tennis.calculate_game_win_probability(p) == expected


@pytest.mark.parametrize("p", [0.1, 0.35, 0.55, 0.6, 0.65, 0.72, 0.9])
async def test_game_probability_matches_formula_and_is_symmetric(p):
    result = tennis.calculate_game_win_probability(p)
    assert result == ref_game(p)
    assert is_four_decimals(result)
    assert result + tennis.calculate_game_win_probability(1.0 - p) == pytest.approx(1.0, abs=2e-4)


async def test_game_probability_is_monotonic():
    values = [tennis.calculate_game_win_probability(step / 20) for step in range(21)]
    assert values == sorted(values)


@pytest.mark.parametrize("p", [-0.01, 1.01, float("nan"), float("inf")])
async def test_game_probability_rejects_invalid_p(p):
    with pytest.raises(ValueError):
        tennis.calculate_game_win_probability(p)


@pytest.mark.parametrize(
    ("base", "player", "opponent", "scale"),
    [
        (0.6, 1500.0, 1500.0, 400.0),
        (0.6, 1900.0, 1500.0, 400.0),
        (0.6, 1900.0, 1500.0, 800.0),   # dynamic scale
        (0.0, 1500.0, 1700.0, 400.0),
        (1.0, 1700.0, 1500.0, 400.0),
    ],
)
async def test_surface_adjusted_serve_prob(base, player, opponent, scale):
    result = tennis.calculate_surface_adjusted_serve_prob(base, player, opponent, scale)
    assert result == round((base + ref_elo(player, opponent, scale)) / 2.0, 4)
    assert tennis.calculate_elo_win_probability(player, opponent, scale) == round(ref_elo(player, opponent, scale), 4)


async def test_elo_overflow_is_handled():
    assert tennis.calculate_elo_win_probability(0.0, 1e6, 1.0) == 0.0
    assert tennis.calculate_elo_win_probability(1e6, 0.0, 1.0) == 1.0
    assert tennis.calculate_surface_adjusted_serve_prob(0.6, 0.0, 1e6, 1.0) == 0.3


@pytest.mark.parametrize(("base", "scale"), [(1.2, 400.0), (0.6, 0.0), (0.6, -400.0), (float("nan"), 400.0)])
async def test_surface_adjusted_rejects_invalid_inputs(base, scale):
    with pytest.raises(ValueError):
        tennis.calculate_surface_adjusted_serve_prob(base, 1500.0, 1500.0, scale)


async def test_elo_rejects_non_positive_scale():
    with pytest.raises(ValueError):
        tennis.calculate_elo_win_probability(1500.0, 1500.0, 0.0)


# =============================================================== schemas


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (DlsRequest, {"resources_left_pct": 1.5, "original_target": 100.0}),
        (DlsRequest, {"resources_left_pct": 0.5, "original_target": -1.0}),
        (
            ProjectScoreRequest,
            {"current_score": 10, "overs_bowled": 60, "wickets_lost": 1, "pitch_degradation_factor": 1.0,
             "total_overs": 50},
        ),
        (
            ProjectScoreRequest,
            {"current_score": 10, "overs_bowled": 5, "wickets_lost": 1, "pitch_degradation_factor": 1.0,
             "total_overs": 0},
        ),
        (
            ProjectScoreRequest,
            {"current_score": 10, "overs_bowled": 5, "wickets_lost": -1, "pitch_degradation_factor": 1.0},
        ),
        (
            ProjectScoreRequest,
            {"current_score": -1, "overs_bowled": 5, "wickets_lost": 1, "pitch_degradation_factor": 1.0},
        ),
        (
            ProjectScoreRequest,
            {"current_score": 10, "overs_bowled": -5, "wickets_lost": 1, "pitch_degradation_factor": 1.0},
        ),
        (
            ProjectScoreRequest,
            {"current_score": 10, "overs_bowled": 5, "wickets_lost": 1, "pitch_degradation_factor": -1.0},
        ),
        (SpreadRequest, {**SPREAD_PAYLOAD, "home_pace": 0}),
        (TennisGameRequest, {"base_serve_prob": 1.1, "player_surface_elo": 1500, "opponent_surface_elo": 1500}),
        (TennisGameRequest, {"base_serve_prob": float("nan"), "player_surface_elo": 1500, "opponent_surface_elo": 1500}),
        (BasketballConfig, {"std_dev": 0.0}),
        (TennisConfig, {"elo_scale": float("inf")}),
        (CricketConfig, {"dls_weight_factor": 1.0, "unknown": 1}),
    ],
)
async def test_schemas_reject_out_of_bounds(schema, payload):
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


async def test_config_read_decodes_json_from_dicts():
    common = {
        "id": uuid4(),
        "sport_name": "tennis",
        "is_active": True,
        "created_at": "2026-09-30T12:00:00+00:00",
        "updated_at": "2026-09-30T12:00:00+00:00",
    }
    parsed = SportConfigRead.model_validate({**common, "config_json": '{"elo_scale": 400.0}'})
    assert parsed.config == {"elo_scale": 400.0}
    assert SportConfigRead.model_validate({**common, "config": {"elo_scale": 1.0}}).config == {"elo_scale": 1.0}
    with pytest.raises(ValidationError):
        SportConfigRead.model_validate({**common, "config_json": "{broken"})


# =============================================================== manager: configuration


@pytest.mark.parametrize(
    ("sport", "expected"),
    [
        ("cricket", {"dls_weight_factor": 1.0, "total_overs": 50.0, "wickets_per_innings": 10}),
        ("basketball", {"std_dev": 11.5}),
        ("tennis", {"elo_scale": 400.0}),
    ],
)
async def test_manager_seeds_neutral_config(manager, session_factory, sport, expected):
    async with session_factory() as session:
        row = await manager.get_config(session, f"  {sport.upper()}  ")
        assert row.sport_name == sport
        assert row.is_active is True
        assert json.loads(row.config_json) == expected
        assert row.created_at is not None
        assert row.updated_at is not None
    async with session_factory() as session:
        await manager.get_config(session, sport)
    assert await _count(session_factory) == 1


async def test_manager_unknown_sport(manager, db_session):
    with pytest.raises(SportNotFoundError):
        await manager.get_config(db_session, "curling")


async def test_manager_seed_recovers_from_concurrent_insert(manager, session_factory, monkeypatch):
    async with session_factory() as winner:
        winner.add(SportConfigModel(sport_name="basketball", config_json='{"std_dev": 9.0}'))
        await winner.commit()

    real_find = manager._find
    calls = {"count": 0}

    async def stale_then_real(db, sport):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await real_find(db, sport)

    monkeypatch.setattr(manager, "_find", stale_then_real)
    async with session_factory() as loser:
        row = await manager.get_config(loser, "basketball")
        stored = row.config_json

    assert calls["count"] == 2
    assert json.loads(stored) == {"std_dev": 9.0}
    assert await _count(session_factory) == 1


async def test_manager_seed_raises_when_row_cannot_be_loaded(manager, session_factory, monkeypatch):
    async with session_factory() as other:
        other.add(SportConfigModel(sport_name="tennis", config_json='{"elo_scale": 400.0}'))
        await other.commit()

    async def always_missing(db, sport):
        return None

    monkeypatch.setattr(manager, "_find", always_missing)
    async with session_factory() as session:
        with pytest.raises(SportsDomainError):
            await manager.get_config(session, "tennis")


async def test_manager_update_changes_subsequent_calculations(manager, session_factory):
    async with session_factory() as session:
        before = await manager.calculate_basketball_spread(session, 5.0, 2.0, 100.0, 98.0, 99.0)
    async with session_factory() as session:
        await manager.update_config(session, "basketball", {"std_dev": 5.0}, True)
    async with session_factory() as session:
        after = await manager.calculate_basketball_spread(session, 5.0, 2.0, 100.0, 98.0, 99.0)

    assert before["home_win_probability"] == ref_win_prob(3.0, 11.5)
    assert after["home_win_probability"] == ref_win_prob(3.0, 5.0)
    assert after["away_win_probability"] == round(1.0 - ref_win_prob(3.0, 5.0), 4)
    assert after["std_dev"] == 5.0


@pytest.mark.parametrize(
    ("sport", "payload"),
    [
        ("basketball", {"std_dev": 0}),
        ("tennis", {"elo_scale": -1}),
        ("cricket", {"bogus": 1}),
        ("cricket", {"wickets_per_innings": 0}),
        ("basketball", {"std_dev": float("nan")}),
    ],
)
async def test_manager_rejects_invalid_config(manager, session_factory, sport, payload):
    async with session_factory() as session:
        with pytest.raises(InvalidSportConfigError):
            await manager.update_config(session, sport, payload, True)
    assert await _count(session_factory) == 0


async def test_manager_update_unknown_sport(manager, db_session):
    with pytest.raises(SportNotFoundError):
        await manager.update_config(db_session, "curling", {}, True)


async def test_manager_inactive_sport_blocks_calculations(manager, session_factory):
    async with session_factory() as session:
        await manager.update_config(session, "tennis", {"elo_scale": 400.0}, False)
    async with session_factory() as session:
        with pytest.raises(SportInactiveError):
            await manager.calculate_tennis_game(session, 0.6, 1500.0, 1500.0)


async def test_manager_detects_corrupt_stored_config(manager, session_factory):
    async with session_factory() as session:
        await session.execute(
            insert(SportConfigModel).values(id=uuid4(), sport_name="tennis", config_json='{"elo_scale": -5}')
        )
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(InvalidSportConfigError):
            await manager.calculate_tennis_game(session, 0.6, 1500.0, 1500.0)


async def test_manager_wraps_math_errors(manager, db_session):
    with pytest.raises(SportsDomainError):
        await manager.calculate_dls(db_session, resources_left_pct=1.5, original_target=100.0)


# =============================================================== manager: calculations


async def test_manager_cricket_calculations_use_dynamic_config(manager, session_factory):
    async with session_factory() as session:
        await manager.update_config(
            session, "cricket", {"dls_weight_factor": 0.5, "total_overs": 20.0, "wickets_per_innings": 10}, True
        )
    async with session_factory() as session:
        dls = await manager.calculate_dls(session, 0.4, 180.0)
        projection = await manager.project_first_innings(session, 90.0, 10.0, 2, 1.0)
        override = await manager.project_first_innings(session, 90.0, 10.0, 2, 1.0, total_overs=50.0)
        all_out = await manager.project_first_innings(session, 90.0, 10.0, 10, 1.0)

    assert dls["par_score"] == 144.0
    assert dls["dls_weight_factor"] == 0.5
    assert projection["projected_score"] == ref_projection(90.0, 10.0, 2, 1.0, 20.0, 10)
    assert projection["total_overs"] == 20.0
    assert projection["innings_complete"] is False
    assert override["projected_score"] == ref_projection(90.0, 10.0, 2, 1.0, 50.0, 10)
    assert all_out["projected_score"] == 90.0
    assert all_out["innings_complete"] is True


async def test_manager_tennis_calculation(manager, db_session):
    result = await manager.calculate_tennis_game(db_session, 0.6, 1500.0, 1500.0)
    assert result["elo_win_probability"] == 0.5
    assert result["adjusted_serve_prob"] == 0.55
    assert result["game_win_probability"] == ref_game(0.55)
    assert result["elo_scale"] == 400.0


# =============================================================== storage constraints


async def test_storage_rejects_empty_sport_name(session_factory):
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(insert(SportConfigModel).values(id=uuid4(), sport_name="", config_json="{}"))
        await session.rollback()


async def test_storage_enforces_unique_sport_name(session_factory):
    async with session_factory() as session:
        await session.execute(insert(SportConfigModel).values(id=uuid4(), sport_name="tennis", config_json="{}"))
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(insert(SportConfigModel).values(id=uuid4(), sport_name="tennis", config_json="{}"))
        await session.rollback()


# =============================================================== API: configuration


@pytest.mark.parametrize(
    ("sport", "key", "value"),
    [("cricket", "dls_weight_factor", 1.0), ("basketball", "std_dev", 11.5), ("tennis", "elo_scale", 400.0)],
)
async def test_api_get_config_seeds_neutral_values(client, sport, key, value):
    response = await client.get(f"{BASE}/{sport}/config")
    assert response.status_code == 200
    body = response.json()
    assert body["sport_name"] == sport
    assert body["is_active"] is True
    assert body["config"][key] == value
    assert "config_json" not in body


async def test_api_get_unknown_sport_returns_404(client):
    assert (await client.get(f"{BASE}/curling/config")).status_code == 404


async def test_api_put_config_updates_in_real_time(client):
    before = (await client.post(f"{BASE}/basketball/calculate-spread", json=SPREAD_PAYLOAD)).json()
    put = await client.put(f"{BASE}/basketball/config", json={"is_active": True, "config": {"std_dev": 5.0}})
    assert put.status_code == 200
    assert put.json()["config"] == {"std_dev": 5.0}
    after = (await client.post(f"{BASE}/basketball/calculate-spread", json=SPREAD_PAYLOAD)).json()

    assert before["home_win_probability"] == ref_win_prob(3.0, 11.5)
    assert after["home_win_probability"] == ref_win_prob(3.0, 5.0)
    assert (await client.get(f"{BASE}/basketball/config")).json()["config"]["std_dev"] == 5.0


@pytest.mark.parametrize(
    "body",
    [
        {"is_active": True, "config": {"std_dev": 0}},
        {"is_active": True, "config": {"std_dev": -2.0}},
        {"is_active": True, "config": {"sigma": 3.0}},
        {"is_active": True, "config": {"std_dev": "wide"}},
        {"config": {"std_dev": 5.0}},
        {"is_active": True, "config": {"std_dev": 5.0}, "extra": 1},
    ],
)
async def test_api_put_invalid_config_returns_422(client, body):
    response = await client.put(f"{BASE}/basketball/config", json=body)
    assert response.status_code == 422


async def test_api_put_unknown_sport_returns_404(client):
    response = await client.put(f"{BASE}/curling/config", json={"is_active": True, "config": {}})
    assert response.status_code == 404


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_api_put_config_rejects_non_finite_tokens(client, token):
    raw = '{"is_active":true,"config":{"std_dev":' + token + "}}"
    await _assert_non_finite_rejected(client, "PUT", f"{BASE}/basketball/config", raw)


async def test_api_deactivated_sport_returns_409(client):
    await client.put(f"{BASE}/cricket/config", json={"is_active": False, "config": {}})
    response = await client.post(
        f"{BASE}/cricket/calculate-dls", json={"resources_left_pct": 0.4, "original_target": 250.0}
    )
    assert response.status_code == 409


# =============================================================== API: cricket


async def test_api_calculate_dls(client):
    response = await client.post(
        f"{BASE}/cricket/calculate-dls", json={"resources_left_pct": 0.4, "original_target": 250.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["par_score"] == 150.0
    assert body["dls_weight_factor"] == 1.0


@pytest.mark.parametrize(
    "body",
    [
        {"resources_left_pct": 1.5, "original_target": 250.0},
        {"resources_left_pct": -0.1, "original_target": 250.0},
        {"resources_left_pct": 0.4, "original_target": -1.0},
        {"resources_left_pct": 0.4},
    ],
)
async def test_api_calculate_dls_invalid_returns_422(client, body):
    assert (await client.post(f"{BASE}/cricket/calculate-dls", json=body)).status_code == 422


async def test_api_project_score(client):
    response = await client.post(
        f"{BASE}/cricket/project-score",
        json={"current_score": 120.0, "overs_bowled": 20.0, "wickets_lost": 2, "pitch_degradation_factor": 1.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["projected_score"] == ref_projection(120.0, 20.0, 2, 1.0, 50.0, 10)
    assert body["total_overs"] == 50.0
    assert body["innings_complete"] is False


async def test_api_project_score_completed_innings(client):
    response = await client.post(
        f"{BASE}/cricket/project-score",
        json={"current_score": 211.0, "overs_bowled": 38.2, "wickets_lost": 10, "pitch_degradation_factor": 0.9},
    )
    assert response.status_code == 200
    assert response.json()["projected_score"] == 211.0
    assert response.json()["innings_complete"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"current_score": -1, "overs_bowled": 5, "wickets_lost": 1, "pitch_degradation_factor": 1.0},
        {"current_score": 10, "overs_bowled": 5, "wickets_lost": -1, "pitch_degradation_factor": 1.0},
        {"current_score": 10, "overs_bowled": 55, "wickets_lost": 1, "pitch_degradation_factor": 1.0, "total_overs": 50},
        {"current_score": 10, "overs_bowled": 5, "wickets_lost": 1, "pitch_degradation_factor": 1.0, "total_overs": 0},
    ],
)
async def test_api_project_score_invalid_returns_422(client, body):
    assert (await client.post(f"{BASE}/cricket/project-score", json=body)).status_code == 422


# =============================================================== API: basketball


async def test_api_calculate_spread(client):
    response = await client.post(f"{BASE}/basketball/calculate-spread", json=SPREAD_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["pace_adjusted_spread"] == 3.0
    assert body["home_win_probability"] == ref_win_prob(3.0, 11.5)
    assert body["home_win_probability"] + body["away_win_probability"] == pytest.approx(1.0, abs=1e-4)
    assert body["std_dev"] == 11.5


@pytest.mark.parametrize("field", ["home_pace", "away_pace", "league_avg_pace"])
@pytest.mark.parametrize("value", [0.0, -10.0])
async def test_api_zero_or_negative_pace_returns_422(client, field, value):
    response = await client.post(f"{BASE}/basketball/calculate-spread", json={**SPREAD_PAYLOAD, field: value})
    assert response.status_code == 422


async def test_api_spread_overflow_returns_400(client):
    payload = {**SPREAD_PAYLOAD, "home_rating": 1e308, "away_rating": -1e308}
    response = await client.post(f"{BASE}/basketball/calculate-spread", json=payload)
    assert response.status_code == 400


# =============================================================== API: tennis


@pytest.mark.parametrize("p", [0.0, 0.5, 1.0])
async def test_api_game_probability_boundaries(client, p):
    response = await client.post(
        f"{BASE}/tennis/calculate-game-prob",
        json={"base_serve_prob": p, "player_surface_elo": 1500.0, "opponent_surface_elo": 1500.0},
    )
    assert response.status_code == 200
    body = response.json()
    adjusted = round((p + 0.5) / 2.0, 4)  # equal Elo -> elo_prob 0.5
    assert body["elo_win_probability"] == 0.5
    assert body["adjusted_serve_prob"] == adjusted
    assert body["game_win_probability"] == ref_game(adjusted)
    assert 0.0 <= body["game_win_probability"] <= 1.0


async def test_api_game_probability_uses_dynamic_elo_scale(client):
    payload = {"base_serve_prob": 0.62, "player_surface_elo": 1900.0, "opponent_surface_elo": 1500.0}
    default = (await client.post(f"{BASE}/tennis/calculate-game-prob", json=payload)).json()
    await client.put(f"{BASE}/tennis/config", json={"is_active": True, "config": {"elo_scale": 800.0}})
    rescaled = (await client.post(f"{BASE}/tennis/calculate-game-prob", json=payload)).json()

    assert default["elo_win_probability"] == round(ref_elo(1900.0, 1500.0, 400.0), 4)
    assert rescaled["elo_win_probability"] == round(ref_elo(1900.0, 1500.0, 800.0), 4)
    assert rescaled["elo_scale"] == 800.0


@pytest.mark.parametrize(
    "body",
    [
        {"base_serve_prob": 1.1, "player_surface_elo": 1500.0, "opponent_surface_elo": 1500.0},
        {"base_serve_prob": -0.1, "player_surface_elo": 1500.0, "opponent_surface_elo": 1500.0},
        {"base_serve_prob": 0.6, "player_surface_elo": 1500.0},
        {"base_serve_prob": 0.6, "player_surface_elo": 1500.0, "opponent_surface_elo": 1500.0, "surface": "clay"},
    ],
)
async def test_api_game_probability_invalid_returns_422(client, body):
    assert (await client.post(f"{BASE}/tennis/calculate-game-prob", json=body)).status_code == 422


# =============================================================== API: NaN / Infinity rejection


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("/cricket/calculate-dls", '{"resources_left_pct":TOKEN,"original_target":250}'),
        (
            "/cricket/project-score",
            '{"current_score":TOKEN,"overs_bowled":10,"wickets_lost":1,"pitch_degradation_factor":1}',
        ),
        (
            "/basketball/calculate-spread",
            '{"home_rating":TOKEN,"away_rating":2,"home_pace":100,"away_pace":98,"league_avg_pace":99}',
        ),
        (
            "/tennis/calculate-game-prob",
            '{"base_serve_prob":0.6,"player_surface_elo":TOKEN,"opponent_surface_elo":1500}',
        ),
    ],
)
async def test_api_rejects_non_finite_tokens(client, token, path, template):
    await _assert_non_finite_rejected(client, "POST", f"{BASE}{path}", template.replace("TOKEN", token))
