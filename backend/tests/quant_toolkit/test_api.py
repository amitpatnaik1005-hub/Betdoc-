"""HTTP tests for The Lab's quant toolkit (PANINI) using httpx.AsyncClient + ASGITransport."""

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/lab/quant-toolkit"  # Group 96: absorbed from /phantom into The Lab

# ---------------------------------------------------------------- dutching


async def test_dutching_endpoint(client):
    response = await client.post(f"{BASE}/dutching", json={"target_total_stake": 100.0, "odds": [2.0, 4.0, 4.0]})
    assert response.status_code == 200
    body = response.json()
    assert body["stakes"] == [50.0, 25.0, 25.0]
    assert body["guaranteed_return"] == 100.0
    assert body["log"]["calc_type"] == "DUTCHING"
    assert body["log"]["inputs"]["odds"] == [2.0, 4.0, 4.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"target_total_stake": 100.0, "odds": [0.0, 2.0]},
        {"target_total_stake": 100.0, "odds": [-1.5, 2.0]},
        {"target_total_stake": 100.0, "odds": [2.0]},
        {"target_total_stake": -5.0, "odds": [2.0, 3.0]},
    ],
)
async def test_dutching_invalid_payloads_return_422(client, payload):
    response = await client.post(f"{BASE}/dutching", json=payload)
    assert response.status_code == 422


# ---------------------------------------------------------------- matched betting


@pytest.mark.parametrize("mode", ["STANDARD", "UNDERLAY", "OVERLAY"])
async def test_matched_betting_modes_satisfy_their_constraints(client, mode):
    back_stake, back_odds, lay_odds, commission = 10.0, 3.0, 3.2, 2.0
    response = await client.post(
        f"{BASE}/matched-betting",
        json={
            "back_stake": back_stake,
            "back_odds": back_odds,
            "lay_odds": lay_odds,
            "lay_commission_pct": commission,
            "mode": mode,
        },
    )
    assert response.status_code == 200
    body = response.json()
    tolerance = 0.005 * lay_odds + 0.01
    assert body["mode"] == mode
    assert body["log"]["calc_type"] == "MATCHED_BETTING"
    if mode == "STANDARD":
        assert abs(body["back_win_profit"] - body["lay_win_profit"]) <= tolerance
    elif mode == "UNDERLAY":
        assert abs(body["lay_win_profit"]) <= tolerance
    else:
        assert abs(body["back_win_profit"]) <= tolerance


@pytest.mark.parametrize(
    "overrides",
    [{"lay_odds": 1.0}, {"lay_odds": 0.0}, {"lay_commission_pct": 100.0}, {"mode": "SIDEWAYS"}, {"back_stake": 0}],
)
async def test_matched_betting_invalid_payloads_return_422(client, overrides):
    payload = {"back_stake": 10.0, "back_odds": 3.0, "lay_odds": 3.2, "lay_commission_pct": 2.0, "mode": "STANDARD"}
    response = await client.post(f"{BASE}/matched-betting", json={**payload, **overrides})
    assert response.status_code == 422


# ---------------------------------------------------------------- market maker

MM_PAYLOAD = {
    "mid_price": 2.0,
    "inventory": 5.0,
    "gamma": 0.1,
    "volatility_sigma": 0.2,
    "time_horizon_t": 1.0,
    "current_time_t": 0.0,
    "liquidity_k": 1.5,
}


async def test_market_maker_endpoint(client):
    response = await client.post(f"{BASE}/market-maker", json=MM_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["reservation_price"] < MM_PAYLOAD["mid_price"]  # long inventory skews quotes down
    assert body["optimal_ask"] > body["optimal_bid"]
    assert body["log"]["calc_type"] == "AVELLANEDA"


@pytest.mark.parametrize("overrides", [{"gamma": 0}, {"liquidity_k": 0}, {"gamma": -1}, {"volatility_sigma": -0.1}])
async def test_market_maker_invalid_payloads_return_422(client, overrides):
    response = await client.post(f"{BASE}/market-maker", json={**MM_PAYLOAD, **overrides})
    assert response.status_code == 422


async def test_market_maker_overflow_returns_400(client):
    response = await client.post(f"{BASE}/market-maker", json={**MM_PAYLOAD, "volatility_sigma": 1e200})
    assert response.status_code == 400
    assert "overflow" in response.json()["detail"]


# ---------------------------------------------------------------- cointegration


@pytest.mark.parametrize(
    ("z_score", "signal"),
    [(3.5, "STOP_LOSS_LIQUIDATE"), (-4.0, "STOP_LOSS_LIQUIDATE"), (2.4, "SELL_SPREAD"), (-2.4, "BUY_SPREAD"),
     (0.2, "CLOSE_POSITION"), (1.2, "HOLD")],
)
async def test_cointegration_endpoint(client, z_score, signal):
    response = await client.post(
        f"{BASE}/cointegration",
        json={"current_z_score": z_score, "entry_threshold": 2.0, "exit_threshold": 0.5, "stop_loss_threshold": 3.0},
    )
    assert response.status_code == 200
    assert response.json()["signal"] == signal
    assert response.json()["log"]["calc_type"] == "COINTEGRATION"


@pytest.mark.parametrize(
    "thresholds",
    [
        {"entry_threshold": 2.0, "exit_threshold": 2.0, "stop_loss_threshold": 3.0},
        {"entry_threshold": 3.0, "exit_threshold": 0.5, "stop_loss_threshold": 2.0},
        {"entry_threshold": 2.0, "exit_threshold": -0.5, "stop_loss_threshold": 3.0},
    ],
)
async def test_cointegration_invalid_thresholds_return_422(client, thresholds):
    response = await client.post(f"{BASE}/cointegration", json={"current_z_score": 1.0, **thresholds})
    assert response.status_code == 422
