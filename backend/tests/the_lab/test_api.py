import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser, get_db
from app.api.v1 import the_lab as lab_api
from tests.the_lab.conftest import ScriptedAgent

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(lab_api, "research_agent", ScriptedAgent(result="# API report"))

    async def override_get_db():
        async with session_factory() as session:
            yield session

    auth_dependency = CurrentUser.__metadata__[0].dependency  # resolve real dep from the alias
    app = FastAPI()
    app.include_router(lab_api.router, prefix="/lab")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[auth_dependency] = lambda: SimpleNamespace(id=uuid.uuid4())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_submit_research_returns_202_and_background_completes(client) -> None:
    response = await client.post("/lab/research", json={"category": "PRE_MATCH", "topic": "Derby"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"

    # ASGITransport awaits background tasks before returning.
    fetched = await client.get(f"/lab/research/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "COMPLETED"
    assert fetched.json()["markdown_content"] == "# API report"


async def test_research_404_and_pagination_validation(client) -> None:
    assert (await client.get(f"/lab/research/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/lab/research", params={"limit": 0})).status_code == 422
    assert (await client.get("/lab/research", params={"offset": -1})).status_code == 422


async def test_experiment_lifecycle_status_codes(client) -> None:
    created = await client.post("/lab/experiments", json={
        "name": "Shootout", "hypothesis": "DC > Poisson",
        "model_a_name": "Poisson", "model_b_name": "Dixon-Coles",
    })
    assert created.status_code == 201
    experiment_id = created.json()["id"]

    bad_winner = await client.patch(f"/lab/experiments/{experiment_id}/conclude",
                                    json={"winner": "Elo", "metrics": {}})
    assert bad_winner.status_code == 422

    concluded = await client.patch(f"/lab/experiments/{experiment_id}/conclude",
                                   json={"winner": "Dixon-Coles", "metrics": {"brier": 0.19}})
    assert concluded.status_code == 200
    assert concluded.json()["status"] == "CONCLUDED"

    again = await client.patch(f"/lab/experiments/{experiment_id}/conclude",
                               json={"winner": "Dixon-Coles", "metrics": {}})
    assert again.status_code == 409

    missing = await client.patch(f"/lab/experiments/{uuid.uuid4()}/conclude",
                                 json={"winner": "Poisson", "metrics": {}})
    assert missing.status_code == 404

    listed = await client.get("/lab/experiments", params={"limit": 1})
    assert listed.status_code == 200 and len(listed.json()) == 1


async def test_health_endpoint(client) -> None:
    response = await client.get("/lab/health")
    assert response.status_code == 200
    assert {s["source_name"] for s in response.json()} == {"FBref", "Transfermarkt", "football-data.co.uk"}
