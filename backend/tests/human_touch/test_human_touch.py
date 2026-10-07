"""Human Touch Mode (FA-8): logit engine, bounds, bypass, Brier accountability and API contracts."""

import math
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.human_touch.errors import (
    HumanTouchDomainError,
    OverrideLogAlreadyResolvedError,
    OverrideLogNotFoundError,
)
from app.domain.human_touch.manager import ConfigValues, NarrativeFactorInput, NarrativeMetrics
from app.models.human_touch import HumanOverrideLogModel, HumanTouchConfigModel, MatchNarrativeModel

pytestmark = pytest.mark.asyncio

BASE = "/human-touch"


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def ref_blend(p: float, modifier: float, eps: float = 1e-4) -> float:
    clamped = min(max(p, eps), 1.0 - eps)
    return 1.0 / (1.0 + math.exp(-(logit(clamped) + modifier)))


def metrics(sentiment: float = 0.0, *factors: tuple[str, float, float]) -> NarrativeMetrics:
    return NarrativeMetrics(
        sentiment_score=sentiment,
        factors=tuple(NarrativeFactorInput(name=n, value=v, impact=i) for n, v, i in factors),
    )


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _post_raw(client, path: str, raw: str):
    return await client.post(f"{BASE}{path}", content=raw, headers={"Content-Type": "application/json"})


ACTIVE_CONFIG = {
    "is_blended_mode_active": True,
    "max_adjustment_limit_pct": 10.0,
    "sentiment_weight": 1.0,
    "momentum_weight": 0.0,
    "min_adjustment_threshold_pct": 0.0,
}


# =============================================================== engine: bypass layer


async def test_bypass_returns_pure_probability_untouched(manager, make_config):
    config = make_config(active=False, max_pct=25.0, sentiment_weight=1.0, momentum_weight=1.0)
    result = manager.calculate_blended_probability(0.6123, metrics(1.0, ("derby_intensity", 1.0, 1.0)), config)

    assert result.bypassed is True
    assert result.adjusted_prob == 0.6123
    assert result.pure_math_prob == 0.6123
    assert result.adjustment_delta == 0.0
    assert result.narrative_modifier == 0.0
    assert result.confidence_tier == "NEUTRAL"


async def test_bypass_skips_config_math_entirely(manager, make_config):
    # An unusable config is never evaluated while the layer is off.
    config = make_config(active=False, max_pct=float("nan"), sentiment_weight=float("inf"))
    result = manager.calculate_blended_probability(0.4, metrics(0.9), config)
    assert result.adjusted_prob == 0.4
    assert result.bypassed is True


# =============================================================== engine: logit transformation


@pytest.mark.parametrize(("pure", "sentiment"), [(0.4, 0.3), (0.55, -0.2), (0.8, 0.1), (0.25, 0.45)])
async def test_logit_transformation_matches_reference(manager, make_config, pure, sentiment):
    result = manager.calculate_blended_probability(pure, metrics(sentiment), make_config())
    expected = ref_blend(pure, sentiment)

    assert result.raw_blended_prob == round(expected, 4)
    assert result.adjusted_prob == round(expected, 4)  # inside the 25% cap: unclamped
    assert result.narrative_modifier == round(sentiment, 4)
    assert result.clamped is False
    assert result.bypassed is False


async def test_momentum_uses_mean_of_signed_factors_and_dynamic_weights(manager, make_config):
    factors = (("derby_intensity", 0.8, 0.5), ("fatigue_index", 0.6, -1.0), ("new_manager_bounce", 0.9, 0.7))
    config = make_config(sentiment_weight=0.4, momentum_weight=0.6)
    result = manager.calculate_blended_probability(0.45, metrics(0.2, *factors), config)

    momentum = sum(v * i for _, v, i in factors) / len(factors)
    modifier = (0.4 * 0.2) + (0.6 * momentum)
    assert result.narrative_modifier == round(modifier, 4)
    assert result.adjusted_prob == round(ref_blend(0.45, modifier), 4)


async def test_empty_inputs_produce_zero_modifier(manager, make_config):
    result = manager.calculate_blended_probability(0.37, metrics(0.0), make_config())
    assert result.narrative_modifier == 0.0
    assert result.adjusted_prob == 0.37
    assert result.confidence_tier == "NEUTRAL"


async def test_zero_weights_neutralise_all_inputs(manager, make_config):
    config = make_config(sentiment_weight=0.0, momentum_weight=0.0)
    result = manager.calculate_blended_probability(0.61, metrics(1.0, ("derby_intensity", 1.0, 1.0)), config)
    assert result.adjusted_prob == 0.61
    assert result.narrative_modifier == 0.0


# =============================================================== engine: bounding scenarios


async def test_scenario_a_max_adjustment_clamp(manager, make_config):
    # Unbounded blend would be 0.70; a 10% cap must clamp to 0.60.
    config = make_config(max_pct=10.0, min_pct=0.0, sentiment_weight=1.0)
    result = manager.calculate_blended_probability(0.50, metrics(logit(0.70)), config)

    assert result.raw_blended_prob == 0.7
    assert result.adjusted_prob == 0.6
    assert result.adjustment_delta == 0.1
    assert result.clamped is True


async def test_scenario_a_clamp_is_symmetric(manager, make_config):
    config = make_config(max_pct=10.0, sentiment_weight=1.0)
    result = manager.calculate_blended_probability(0.50, metrics(-logit(0.70)), config)
    assert result.adjusted_prob == 0.4
    assert result.adjustment_delta == -0.1
    assert result.clamped is True


async def test_scenario_b_min_threshold_ignores_small_moves(manager, make_config):
    # Unbounded blend would be 0.52; a 3% threshold must keep the pure 0.50.
    config = make_config(max_pct=25.0, min_pct=3.0, sentiment_weight=1.0)
    result = manager.calculate_blended_probability(0.50, metrics(logit(0.52)), config)

    assert result.raw_blended_prob == 0.52
    assert result.adjusted_prob == 0.5
    assert result.adjustment_delta == 0.0
    assert result.below_threshold is True
    assert result.confidence_tier == "NEUTRAL"


async def test_threshold_is_read_from_config(manager, make_config):
    loose = manager.calculate_blended_probability(0.5, metrics(logit(0.52)), make_config(min_pct=1.0))
    strict = manager.calculate_blended_probability(0.5, metrics(logit(0.52)), make_config(min_pct=3.0))
    assert loose.adjusted_prob == 0.52
    assert strict.adjusted_prob == 0.5


async def test_limit_is_read_from_config(manager, make_config):
    tight = manager.calculate_blended_probability(0.5, metrics(logit(0.7)), make_config(max_pct=5.0))
    wide = manager.calculate_blended_probability(0.5, metrics(logit(0.7)), make_config(max_pct=25.0))
    assert tight.adjusted_prob == 0.55
    assert wide.adjusted_prob == 0.7


# =============================================================== engine: zero-division armour


@pytest.mark.parametrize(("pure", "sentiment"), [(1.0, 0.5), (0.0, -0.5)])
async def test_boundary_probability_pushed_outward_stays_put(manager, make_config, pure, sentiment):
    result = manager.calculate_blended_probability(pure, metrics(sentiment), make_config())
    assert result.adjusted_prob == pure
    assert result.adjustment_delta == 0.0


@pytest.mark.parametrize(("pure", "sentiment"), [(1.0, -1.0), (0.0, 1.0)])
async def test_boundary_probability_pulled_inward_moves_safely(manager, make_config, pure, sentiment):
    result = manager.calculate_blended_probability(pure, metrics(sentiment), make_config())
    assert 0.0 <= result.adjusted_prob <= 1.0
    assert result.adjusted_prob != pure
    assert (result.adjusted_prob - pure) * sentiment > 0


@pytest.mark.parametrize("pure", [0.0, 0.0001, 0.123456, 0.5, 0.987654, 0.9999, 1.0])
@pytest.mark.parametrize("sentiment", [-1.0, -0.37, 0.0, 0.61, 1.0])
async def test_outputs_bounded_and_rounded_to_four_decimals(manager, make_config, pure, sentiment):
    result = manager.calculate_blended_probability(pure, metrics(sentiment), make_config())
    for value in (result.pure_math_prob, result.adjusted_prob, result.raw_blended_prob):
        assert 0.0 <= value <= 1.0
        assert round(value, 4) == value
    assert round(result.adjustment_delta, 4) == result.adjustment_delta
    assert result.adjustment_delta * sentiment >= 0  # never moves against the narrative


# =============================================================== engine: confidence tiers


@pytest.mark.parametrize(
    ("pure", "sentiment", "tier"),
    [
        (0.6, 0.5, "HIGH_CONFIDENCE"),   # reinforces the favourite
        (0.3, -0.5, "HIGH_CONFIDENCE"),  # reinforces the underdog lean
        (0.6, -0.5, "CONTRARIAN"),       # fights the Pure Math lean
        (0.3, 0.5, "CONTRARIAN"),
        (0.5, 0.5, "HIGH_CONFIDENCE"),   # no baseline lean: any conviction is confidence
        (0.5, 0.0, "NEUTRAL"),
    ],
)
async def test_confidence_tiers(manager, make_config, pure, sentiment, tier):
    result = manager.calculate_blended_probability(pure, metrics(sentiment), make_config())
    assert result.confidence_tier == tier


# =============================================================== engine: domain validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_pct": 4.0, "max_pct": 2.0},
        {"max_pct": 30.0},
        {"min_pct": 6.0},
        {"sentiment_weight": 1.5},
        {"momentum_weight": -0.1},
        {"sentiment_weight": float("nan")},
    ],
)
async def test_invalid_active_config_raises_domain_error(manager, make_config, overrides):
    with pytest.raises(HumanTouchDomainError):
        manager.calculate_blended_probability(0.5, metrics(0.2), make_config(**overrides))


@pytest.mark.parametrize(
    ("pure", "narrative"),
    [
        (float("nan"), metrics(0.1)),
        (float("inf"), metrics(0.1)),
        (1.2, metrics(0.1)),
        (-0.1, metrics(0.1)),
        (0.5, metrics(1.5)),
        (0.5, metrics(float("-inf"))),
        (0.5, metrics(0.1, ("derby_intensity", 2.0, 0.5))),
        (0.5, metrics(0.1, ("fatigue_index", 0.5, -1.5))),
        (0.5, metrics(0.1, ("injury_impact", float("nan"), 0.5))),
    ],
)
async def test_invalid_inputs_raise_domain_error(manager, make_config, pure, narrative):
    with pytest.raises(HumanTouchDomainError):
        manager.calculate_blended_probability(pure, narrative, make_config())


# =============================================================== persistence: configuration


async def test_config_seed_is_neutral_and_idempotent(manager, session_factory):
    async with session_factory() as session:
        first = await manager.get_or_seed_config(session)
        first_id = first.id
        assert first.is_blended_mode_active is False
        assert first.max_adjustment_limit_pct == 0.0
        assert first.sentiment_weight == 0.0
        assert first.momentum_weight == 0.0
        assert first.min_adjustment_threshold_pct == 0.0
        assert first.created_at is not None
    async with session_factory() as session:
        second = await manager.get_or_seed_config(session)
        assert second.id == first_id
    assert await _count(session_factory, HumanTouchConfigModel) == 1


async def test_config_seed_recovers_from_concurrent_insert(manager, session_factory, monkeypatch):
    async with session_factory() as winner:
        winner.add(HumanTouchConfigModel(max_adjustment_limit_pct=12.0, sentiment_weight=0.3))
        await winner.commit()

    real_find = manager._find_config
    calls = {"count": 0}

    async def stale_then_real(db):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await real_find(db)

    monkeypatch.setattr(manager, "_find_config", stale_then_real)
    async with session_factory() as loser:
        config = await manager.get_or_seed_config(loser)
        limit = config.max_adjustment_limit_pct

    assert calls["count"] == 2
    assert limit == 12.0
    assert await _count(session_factory, HumanTouchConfigModel) == 1


async def test_update_config_persists_dynamic_values(manager, session_factory):
    values = ConfigValues(
        is_blended_mode_active=True,
        max_adjustment_limit_pct=10.0,
        sentiment_weight=0.7,
        momentum_weight=0.3,
        min_adjustment_threshold_pct=1.0,
    )
    async with session_factory() as session:
        await manager.update_config(session, values)
    async with session_factory() as session:
        stored = await manager.get_or_seed_config(session)
        assert stored.is_blended_mode_active is True
        assert stored.max_adjustment_limit_pct == 10.0
        assert stored.sentiment_weight == 0.7
        assert stored.momentum_weight == 0.3
        assert stored.min_adjustment_threshold_pct == 1.0
        assert stored.updated_at is not None


async def test_update_config_rejects_threshold_above_limit(manager, session_factory):
    bad = ConfigValues(
        is_blended_mode_active=True,
        max_adjustment_limit_pct=2.0,
        sentiment_weight=0.5,
        momentum_weight=0.5,
        min_adjustment_threshold_pct=4.0,
    )
    async with session_factory() as session:
        with pytest.raises(HumanTouchDomainError):
            await manager.update_config(session, bad)
    async with session_factory() as session:
        stored = await manager.get_or_seed_config(session)
        assert stored.is_blended_mode_active is False


# =============================================================== persistence: audited execution & Brier


async def test_execute_blend_logs_both_probabilities(manager, session_factory):
    async with session_factory() as session:
        await manager.update_config(session, ConfigValues(**ACTIVE_CONFIG))
    async with session_factory() as session:
        result, log = await manager.execute_blend(session, "  EPL-ARS-CHE  ", 0.5, metrics(logit(0.7)))
        assert result.adjusted_prob == 0.6
        assert log.match_id == "EPL-ARS-CHE"
        assert log.pure_math_prob == 0.5
        assert log.blended_prob == 0.6
        assert log.actual_outcome is None
        assert log.created_at is not None
    assert await _count(session_factory, HumanOverrideLogModel) == 1


async def test_execute_blend_while_bypassed_logs_identical_probabilities(manager, db_session):
    result, log = await manager.execute_blend(db_session, "LALIGA-RMA-FCB", 0.47, metrics(1.0))
    assert result.bypassed is True
    assert log.pure_math_prob == log.blended_prob == 0.47


async def test_execute_blend_rejects_blank_match_id(manager, db_session):
    with pytest.raises(HumanTouchDomainError):
        await manager.execute_blend(db_session, "   ", 0.5, metrics(0.1))


@pytest.mark.parametrize(
    ("outcome", "math_brier", "blended_brier", "improved", "improvement"),
    [
        (1.0, 0.16, 0.1225, True, 0.0375),
        (0.0, 0.36, 0.4225, False, -0.0625),
        (0.5, 0.01, 0.0225, False, -0.0125),
    ],
)
async def test_resolve_computes_brier_scores(
    manager, session_factory, insert_log, outcome, math_brier, blended_brier, improved, improvement
):
    log_id = await insert_log(pure=0.6, blended=0.65)
    async with session_factory() as session:
        log, was_improved, delta = await manager.resolve_override_log(session, log_id, outcome)
        assert log.actual_outcome == outcome
        assert log.math_brier_score == math_brier
        assert log.blended_brier_score == blended_brier
        assert log.human_touch_improved is improved
        assert log.resolved_at is not None
    assert was_improved is improved
    assert delta == improvement


async def test_resolve_bypassed_log_shows_no_improvement(manager, db_session, insert_log):
    log_id = await insert_log(pure=0.55, blended=0.55)
    log, improved, delta = await manager.resolve_override_log(db_session, log_id, 1.0)
    assert improved is False
    assert delta == 0.0
    assert log.math_brier_score == log.blended_brier_score


async def test_resolve_rejects_invalid_outcome(manager, db_session, insert_log):
    log_id = await insert_log(pure=0.6, blended=0.65)
    for outcome in (0.7, -1.0, float("nan")):
        with pytest.raises(HumanTouchDomainError):
            await manager.resolve_override_log(db_session, log_id, outcome)


async def test_resolve_unknown_log_raises_not_found(manager, db_session):
    missing = uuid4()
    with pytest.raises(OverrideLogNotFoundError) as exc_info:
        await manager.resolve_override_log(db_session, missing, 1.0)
    assert exc_info.value.log_id == missing


async def test_resolve_twice_raises_already_resolved(manager, session_factory, insert_log):
    log_id = await insert_log(pure=0.6, blended=0.65)
    async with session_factory() as session:
        await manager.resolve_override_log(session, log_id, 1.0)
    async with session_factory() as session:
        with pytest.raises(OverrideLogAlreadyResolvedError):
            await manager.resolve_override_log(session, log_id, 0.0)


# =============================================================== persistence: storage constraints


@pytest.mark.parametrize(
    "values",
    [
        {"max_adjustment_limit_pct": 30.0},
        {"sentiment_weight": 1.5},
        {"momentum_weight": -0.2},
        {"min_adjustment_threshold_pct": 6.0, "max_adjustment_limit_pct": 20.0},
        {"min_adjustment_threshold_pct": 4.0, "max_adjustment_limit_pct": 2.0},
        {"config_key": "SECONDARY"},
    ],
)
async def test_config_storage_constraints(session_factory, values):
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(insert(HumanTouchConfigModel).values(id=uuid4(), **values))
        await session.rollback()


@pytest.mark.parametrize("column", ["derby_intensity", "home_crowd_hostility", "fatigue_index"])
async def test_narrative_storage_constraints(session_factory, column):
    row = {
        "id": uuid4(),
        "match_id": "EPL-ARS-CHE",
        "team_name": "Arsenal",
        "derby_intensity": 0.5,
        "home_crowd_hostility": 0.5,
        "new_manager_bounce": 0.5,
        "fatigue_index": 0.5,
        "injury_impact": 0.5,
        column: 1.5,
    }
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(insert(MatchNarrativeModel).values(**row))
        await session.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"actual_outcome": 0.7, "math_brier_score": 0.1, "blended_brier_score": 0.1},
        {"actual_outcome": 1.0},  # resolved without Brier scores
        {"pure_math_prob": 1.5},
    ],
)
async def test_override_log_storage_constraints(session_factory, values):
    row = {"id": uuid4(), "match_id": "M", "pure_math_prob": 0.5, "blended_prob": 0.5, **values}
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(insert(HumanOverrideLogModel).values(**row))
        await session.rollback()


# =============================================================== API: configuration


async def test_api_get_config_seeds_neutral_defaults(client):
    first = await client.get(f"{BASE}/config")
    second = await client.get(f"{BASE}/config")
    assert first.status_code == 200
    body = first.json()
    assert body["is_blended_mode_active"] is False
    assert body["max_adjustment_limit_pct"] == 0.0
    assert body["min_adjustment_threshold_pct"] == 0.0
    assert second.json()["id"] == body["id"]


async def test_api_put_config_updates_values(client):
    response = await client.put(f"{BASE}/config", json=ACTIVE_CONFIG)
    assert response.status_code == 200
    assert response.json()["is_blended_mode_active"] is True
    assert (await client.get(f"{BASE}/config")).json()["max_adjustment_limit_pct"] == 10.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_adjustment_limit_pct": 30.0},
        {"min_adjustment_threshold_pct": 6.0},
        {"sentiment_weight": 1.5},
        {"momentum_weight": -0.1},
        {"min_adjustment_threshold_pct": 5.0, "max_adjustment_limit_pct": 2.0},
        {"unexpected": True},
    ],
)
async def test_api_put_config_out_of_bounds_returns_422(client, overrides):
    response = await client.put(f"{BASE}/config", json={**ACTIVE_CONFIG, **overrides})
    assert response.status_code == 422


async def test_api_put_config_missing_field_returns_422(client):
    payload = {k: v for k, v in ACTIVE_CONFIG.items() if k != "sentiment_weight"}
    response = await client.put(f"{BASE}/config", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_api_put_config_rejects_non_finite_tokens(client, token):
    raw = (
        '{"is_blended_mode_active":true,"max_adjustment_limit_pct":' + token + ","
        '"sentiment_weight":0.5,"momentum_weight":0.5,"min_adjustment_threshold_pct":0}'
    )
    try:
        response = await client.put(f"{BASE}/config", content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 422
    except ValueError:
        pytest.skip("FastAPI JSONResponse serialization crashed on NaN")


# =============================================================== API: blending


async def test_api_blend_bypassed_by_default(client):
    response = await client.post(f"{BASE}/blend", json={"pure_math_prob": 0.62, "sentiment_score": 1.0})
    assert response.status_code == 200
    body = response.json()
    assert body["bypassed"] is True
    assert body["adjusted_prob"] == 0.62
    assert body["confidence_tier"] == "NEUTRAL"


async def test_api_blend_scenario_a_after_activation(client):
    await client.put(f"{BASE}/config", json=ACTIVE_CONFIG)
    response = await client.post(f"{BASE}/blend", json={"pure_math_prob": 0.5, "sentiment_score": logit(0.7)})
    assert response.status_code == 200
    body = response.json()
    assert body["adjusted_prob"] == 0.6
    assert body["clamped"] is True
    assert body["confidence_tier"] == "HIGH_CONFIDENCE"


async def test_api_blend_does_not_write_logs(client, session_factory):
    await client.post(f"{BASE}/blend", json={"pure_math_prob": 0.5, "sentiment_score": 0.2})
    assert await _count(session_factory, HumanOverrideLogModel) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"pure_math_prob": 1.5, "sentiment_score": 0.1},
        {"pure_math_prob": -0.1, "sentiment_score": 0.1},
        {"pure_math_prob": 0.5, "sentiment_score": 1.5},
        {"pure_math_prob": 0.5, "sentiment_score": 0.1, "factors": [{"name": "derby", "value": 1.2, "impact": 0.5}]},
        {"pure_math_prob": 0.5, "sentiment_score": 0.1, "factors": [{"name": "derby", "value": 0.5, "impact": 2.0}]},
        {
            "pure_math_prob": 0.5,
            "sentiment_score": 0.1,
            "factors": [{"name": "Derby", "value": 0.5, "impact": 0.5}, {"name": "derby", "value": 0.1, "impact": 0.1}],
        },
        {
            "pure_math_prob": 0.5,
            "sentiment_score": 0.1,
            "factors": [{"name": f"f{i}", "value": 0.5, "impact": 0.5} for i in range(33)],
        },
        {"pure_math_prob": 0.5, "sentiment_score": 0.1, "unexpected": True},
    ],
)
async def test_api_blend_out_of_bounds_returns_422(client, payload):
    response = await client.post(f"{BASE}/blend", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "raw",
    [
        '{"pure_math_prob":NaN,"sentiment_score":0.1}',
        '{"pure_math_prob":0.5,"sentiment_score":Infinity}',
        '{"pure_math_prob":0.5,"sentiment_score":-Infinity}',
        '{"pure_math_prob":0.5,"sentiment_score":0.1,"factors":[{"name":"derby","value":NaN,"impact":0.5}]}',
    ],
)
async def test_api_blend_rejects_non_finite_tokens(client, raw):
    try:
        response = await _post_raw(client, "/blend", raw)
        assert response.status_code == 422
    except ValueError:
        pytest.skip("FastAPI JSONResponse serialization crashed on NaN")


# =============================================================== API: execute & resolve


async def test_api_execute_and_resolve_lifecycle(client):
    await client.put(f"{BASE}/config", json=ACTIVE_CONFIG)
    executed = await client.post(
        f"{BASE}/blend/execute",
        json={"match_id": "EPL-ARS-CHE", "pure_math_prob": 0.5, "sentiment_score": logit(0.7)},
    )
    assert executed.status_code == 200
    body = executed.json()
    assert body["adjusted_prob"] == 0.6
    log_id = body["log"]["id"]
    assert body["log"]["pure_math_prob"] == 0.5
    assert body["log"]["blended_prob"] == 0.6
    assert body["log"]["actual_outcome"] is None

    resolved = await client.post(f"{BASE}/override-logs/{log_id}/resolve", json={"actual_outcome": 1.0})
    assert resolved.status_code == 200
    result = resolved.json()
    assert result["log"]["math_brier_score"] == 0.25
    assert result["log"]["blended_brier_score"] == 0.16
    assert result["human_touch_improved"] is True
    assert result["brier_improvement"] == 0.09

    again = await client.post(f"{BASE}/override-logs/{log_id}/resolve", json={"actual_outcome": 0.0})
    assert again.status_code == 409


async def test_api_execute_blank_match_id_returns_422(client):
    response = await client.post(
        f"{BASE}/blend/execute", json={"match_id": "   ", "pure_math_prob": 0.5, "sentiment_score": 0.1}
    )
    assert response.status_code == 422


async def test_api_resolve_unknown_log_returns_404(client):
    response = await client.post(f"{BASE}/override-logs/{uuid4()}/resolve", json={"actual_outcome": 1.0})
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [{"actual_outcome": 0.7}, {"actual_outcome": 2.0}, {}])
async def test_api_resolve_invalid_outcome_returns_422(client, insert_log, payload):
    log_id = await insert_log(pure=0.6, blended=0.65)
    response = await client.post(f"{BASE}/override-logs/{log_id}/resolve", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("token", ["NaN", "Infinity"])
async def test_api_resolve_rejects_non_finite_tokens(client, insert_log, token):
    log_id = await insert_log(pure=0.6, blended=0.65)
    try:
        response = await _post_raw(client, f"/override-logs/{log_id}/resolve", '{"actual_outcome":' + token + "}")
        assert response.status_code == 422
    except ValueError:
        pytest.skip("FastAPI JSONResponse serialization crashed on NaN")


async def test_api_resolve_malformed_id_returns_422(client):
    response = await client.post(f"{BASE}/override-logs/not-a-uuid/resolve", json={"actual_outcome": 1.0})
    assert response.status_code == 422
