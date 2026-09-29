"""HTTP contract tests. BackgroundTasks are not relied upon here: responses are

snapshotted as QUEUED before any worker runs; worker behaviour is covered in

test_orchestrator.py."""



from uuid import uuid4



import pytest

import pytest_asyncio

from fastapi import FastAPI

from httpx import ASGITransport, AsyncClient



from app.api.deps import get_db

from app.api.v1 import the_core as core_api



pytestmark = pytest.mark.asyncio



BASE = "/api/v1/engine"





@pytest_asyncio.fixture

async def client(session_factory, orch, broadcaster):

    app = FastAPI()

    app.include_router(core_api.router, prefix=BASE)



    async def override_get_db():

        async with session_factory() as session:

            yield session



    app.dependency_overrides[get_db] = override_get_db

    app.dependency_overrides[core_api.get_session_factory] = lambda: session_factory

    app.dependency_overrides[core_api.get_orchestrator] = lambda: orch

    app.dependency_overrides[core_api.get_ws_manager] = lambda: broadcaster



    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://engine-room.test") as http:

        yield http





async def _bootstrap(client):

    response = await client.post(f"{BASE}/bootstrap")

    assert response.status_code == 200

    return {item["name"]: item for item in response.json()}





def _poisson(smallcases):

    return smallcases["PRATAP Poisson-Kelly Classic"]





async def test_status_returns_telemetry(client):

    response = await client.get(f"{BASE}/status")

    assert response.status_code == 200

    body = response.json()

    assert body["master_bot"] == "PRATAP"

    assert body["math_engine"] == "PANINI"

    assert 0 <= body["cpu_usage_pct"] <= 100

    assert body["engine_state"] in {"ONLINE", "DEGRADED", "SATURATED"}





async def test_bootstrap_and_list(client):

    smallcases = await _bootstrap(client)

    assert len(smallcases) == 3



    listing = await client.get(f"{BASE}/smallcases")

    assert listing.status_code == 200

    assert len(listing.json()) == 3



    standby = await client.get(f"{BASE}/smallcases", params={"status": "STANDBY"})

    assert [s["name"] for s in standby.json()] == ["Elo Monte Carlo Fusion"]





async def test_smallcase_detail_and_errors(client):

    smallcases = await _bootstrap(client)

    detail = await client.get(f"{BASE}/smallcases/{_poisson(smallcases)['id']}")

    assert detail.status_code == 200

    body = detail.json()

    assert body["pipeline_config"] == ["PoissonModel", "KellyStake"]

    assert [s["execution_mode"] for s in body["pipeline_stages"]] == ["NATIVE", "SIMULATED"]



    assert (await client.get(f"{BASE}/smallcases/{uuid4()}")).status_code == 404

    assert (await client.get(f"{BASE}/smallcases/not-a-uuid")).status_code == 422





async def test_toggle_success_conflict_and_validation(client, broadcaster):

    smallcase_id = _poisson(await _bootstrap(client))["id"]

    body = {"expected_status": "ACTIVE", "target_status": "STANDBY"}



    ok = await client.post(f"{BASE}/smallcases/{smallcase_id}/toggle", json=body)

    assert ok.status_code == 200

    assert ok.json()["status"] == "STANDBY"

    assert broadcaster.events("smallcase.toggled")[-1]["payload"]["status"] == "STANDBY"



    conflict = await client.post(f"{BASE}/smallcases/{smallcase_id}/toggle", json=body)

    assert conflict.status_code == 409

    assert conflict.json()["detail"]["error"] == "EngineConcurrencyError"



    noop = await client.post(

        f"{BASE}/smallcases/{smallcase_id}/toggle", json={"expected_status": "STANDBY", "target_status": "STANDBY"}

    )

    assert noop.status_code == 422



    extra = await client.post(f"{BASE}/smallcases/{smallcase_id}/toggle", json={**body, "force": True})

    assert extra.status_code == 422



    missing = await client.post(f"{BASE}/smallcases/{uuid4()}/toggle", json=body)

    assert missing.status_code == 404





async def test_post_test_returns_queued(client, match_context):

    smallcase_id = _poisson(await _bootstrap(client))["id"]

    response = await client.post(f"{BASE}/test", json={"smallcase_id": smallcase_id, "match_context": match_context})

    assert response.status_code == 202

    body = response.json()

    assert body["status"] == "QUEUED"

    assert body["is_stress_test"] is False

    assert body["pipeline_execution_steps"] == []

    assert body["predicted_outcome"] is None

    assert body["match_context"]["home_team"] == match_context["home_team"]





async def test_post_test_rejections(client, match_context):

    smallcase_id = _poisson(await _bootstrap(client))["id"]



    unknown = await client.post(f"{BASE}/test", json={"smallcase_id": str(uuid4()), "match_context": match_context})

    assert unknown.status_code == 404



    empty = await client.post(f"{BASE}/test", json={"smallcase_id": smallcase_id, "match_context": {}})

    assert empty.status_code == 422



    extra = await client.post(

        f"{BASE}/test", json={"smallcase_id": smallcase_id, "match_context": match_context, "priority": 1}

    )

    assert extra.status_code == 422



    await client.post(

        f"{BASE}/smallcases/{smallcase_id}/toggle", json={"expected_status": "ACTIVE", "target_status": "DISABLED"}

    )

    disabled = await client.post(f"{BASE}/test", json={"smallcase_id": smallcase_id, "match_context": match_context})

    assert disabled.status_code == 409





async def test_stress_test_returns_queued_with_extreme_inputs(client):

    smallcase_id = _poisson(await _bootstrap(client))["id"]

    response = await client.post(f"{BASE}/smallcases/{smallcase_id}/stress-test")

    assert response.status_code == 202

    body = response.json()

    assert body["status"] == "QUEUED"

    assert body["is_stress_test"] is True

    assert body["match_context"]["odds"]["home"] >= 1_000_000

    assert body["match_context"]["odds"]["away"] == 0.0



    assert (await client.post(f"{BASE}/smallcases/{uuid4()}/stress-test")).status_code == 404





async def test_test_compare_returns_list(client, match_context):

    smallcases = await _bootstrap(client)

    ids = [s["id"] for s in smallcases.values()]

    response = await client.post(f"{BASE}/test-compare", json={"smallcase_ids": ids, "match_context": match_context})

    assert response.status_code == 202

    body = response.json()

    assert [r["smallcase_id"] for r in body] == ids

    assert all(r["status"] == "QUEUED" for r in body)



    duplicate = await client.post(

        f"{BASE}/test-compare", json={"smallcase_ids": [ids[0], ids[0]], "match_context": match_context}

    )

    assert duplicate.status_code == 422



    missing = await client.post(

        f"{BASE}/test-compare", json={"smallcase_ids": [ids[0], str(uuid4())], "match_context": match_context}

    )

    assert missing.status_code == 404





async def test_backtest_returns_queued(client):

    smallcase_id = _poisson(await _bootstrap(client))["id"]

    response = await client.post(

        f"{BASE}/backtest",

        json={"smallcase_id": smallcase_id, "start_date": "2024-01-01", "end_date": "2024-06-30"},

    )

    assert response.status_code == 202

    body = response.json()

    assert body["status"] == "QUEUED"

    assert body["start_date"] == "2024-01-01"

    assert body["end_date"] == "2024-06-30"

    assert body["total_matches_simulated"] == 0



    inverted = await client.post(

        f"{BASE}/backtest",

        json={"smallcase_id": smallcase_id, "start_date": "2024-06-30", "end_date": "2024-01-01"},

    )

    assert inverted.status_code == 422



    unknown = await client.post(

        f"{BASE}/backtest",

        json={"smallcase_id": str(uuid4()), "start_date": "2024-01-01", "end_date": "2024-01-31"},

    )

    assert unknown.status_code == 404