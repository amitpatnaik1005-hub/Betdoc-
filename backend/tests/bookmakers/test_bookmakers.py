from __future__ import annotations

import json
import math
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.api.v1.bookmakers import nan_safe_validation_exception_handler
from app.core.registry import PluginNotFoundError, PluginRegistry
from app.domain.bookmakers import manager
from app.domain.bookmakers.adapters import (
    BetfairAdapter,
    BookmakerAdapter,
    GenericAdapter,
    OneXBetAdapter,
    ParimatchAdapter,
    PinnacleAdapter,
    StakeAdapter,
    bookmaker_registry,
    create_adapter,
)
from app.domain.bookmakers.analytics import find_best_odds
from app.domain.bookmakers.omniroute import FAILURE_SENTINEL_ODDS, OmniRouteClient
from app.models.bookmakers import BookmakerConfigModel

API = "/api/v1/bookmakers"
DEFAULT_NAMES = ["Stake", "Parimatch", "1xBet", "Pinnacle", "Betfair"]
MATCH_CONTEXT: dict[str, str] = {
    "sport": "Football",
    "league": "Premier League",
    "match": "Arsenal vs Chelsea",
    "market": "Match Odds",
    "selection": "Arsenal",
}


def make_config(name: str, rank: int, active: bool = True) -> BookmakerConfigModel:
    return BookmakerConfigModel(name=name, priority_rank=rank, is_active=active)


def _reject_constant(token: str) -> Any:
    raise ValueError(f"Non-standard JSON constant in response: {token}")


def strict_json(raw: str | bytes) -> Any:
    return json.loads(raw, parse_constant=_reject_constant)


async def activate(client: AsyncClient, name: str, **extra: Any) -> dict[str, Any]:
    response = await client.put(f"{API}/{name}/config", json={"is_active": True, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def count_configs(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(BookmakerConfigModel)) or 0)


# --------------------------------------------------------------------------- #
# PluginRegistry[T]
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Stake", StakeAdapter),
        ("stake", StakeAdapter),
        ("PARIMATCH", ParimatchAdapter),
        ("1xBet", OneXBetAdapter),
        ("OneXBet", OneXBetAdapter),
        ("Pinnacle", PinnacleAdapter),
        ("  pinnacle  ", PinnacleAdapter),
        ("Betfair", BetfairAdapter),
    ],
)
def test_registry_resolves_specific_adapters(name: str, expected: type[BookmakerAdapter]) -> None:
    assert bookmaker_registry.get(name) is expected
    assert bookmaker_registry.resolve(name) is expected


def test_registry_falls_back_to_generic_adapter_for_unknown_names() -> None:
    assert bookmaker_registry.get("DraftKings") is None
    assert bookmaker_registry.resolve("DraftKings") is GenericAdapter
    assert bookmaker_registry.fallback is GenericAdapter


def test_registry_stores_classes_and_creates_fresh_instances() -> None:
    assert all(isinstance(cls, type) for cls in bookmaker_registry.plugins().values())
    first = create_adapter("Pinnacle")
    second = create_adapter("Pinnacle")
    assert isinstance(first, PinnacleAdapter)
    assert first is not second


def test_generic_registry_supports_any_plugin_family() -> None:
    class Notifier:
        pass

    registry = PluginRegistry[Notifier]("notifiers")

    @registry.register("Email")
    class EmailNotifier(Notifier):
        pass

    @registry.register("default", fallback=True)
    class NullNotifier(Notifier):
        pass

    assert registry.resolve("email") is EmailNotifier
    assert registry.resolve("Slack") is NullNotifier
    assert isinstance(registry.create("EMAIL"), EmailNotifier)
    assert "EMAIL" in registry
    assert "Slack" not in registry
    assert len(registry) == 2


def test_registry_rejects_duplicates_and_reports_missing_plugins() -> None:
    registry = PluginRegistry[object]("strict")

    @registry.register("One")
    class First:
        pass

    with pytest.raises(ValueError):

        @registry.register("one")
        class Second:
            pass

    @registry.register("fallback-a", fallback=True)
    class FallbackA:
        pass

    with pytest.raises(ValueError):

        @registry.register("fallback-b", fallback=True)
        class FallbackB:
            pass

    registry.set_fallback(None)
    with pytest.raises(PluginNotFoundError):
        registry.resolve("two")
    with pytest.raises(ValueError):
        registry.get("   ")


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


def test_pinnacle_instruction_matches_contract() -> None:
    adapter = create_adapter("Pinnacle")
    instruction = adapter.generate_placement_instruction(
        "Football", "Premier League", "Arsenal vs Chelsea", "Match Odds", "Arsenal", 2.1
    )
    assert instruction == (
        "On Pinnacle: Select Football → Premier League → Arsenal vs Chelsea → Match Odds "
        "→ Select Arsenal (2.10)"
    )


@pytest.mark.parametrize("name", DEFAULT_NAMES)
def test_every_default_adapter_produces_branded_instruction(name: str) -> None:
    adapter = create_adapter(name)
    assert not isinstance(adapter, GenericAdapter)
    instruction = adapter.generate_placement_instruction(
        "Tennis", "ATP Tour", "Alcaraz vs Sinner", "Match Winner", "Sinner", 1.952
    )
    assert instruction.startswith(f"On {name}:")
    assert "Sinner (1.952)" in instruction


def test_generic_adapter_formats_unknown_bookmaker() -> None:
    adapter = create_adapter("DraftKings")
    assert isinstance(adapter, GenericAdapter)
    assert adapter.generate_placement_instruction(
        "Football", "Premier League", "Arsenal vs Chelsea", "Match Odds", "Arsenal", 2.2
    ) == "On DraftKings: Navigate to Football > Arsenal vs Chelsea and select Arsenal at odds 2.20"


@pytest.mark.parametrize("odds", [math.nan, math.inf, -1.5, 0.0])
def test_adapter_rejects_invalid_odds(odds: float) -> None:
    with pytest.raises(ValueError):
        create_adapter("Stake").generate_placement_instruction(
            "Football", "EPL", "A vs B", "Match Odds", "A", odds
        )


# --------------------------------------------------------------------------- #
# Database seeding and constraints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_seeds_default_bookmakers_when_empty(db_session: AsyncSession) -> None:
    configs = await manager.get_all_configs(db_session)
    assert [config.name for config in configs] == DEFAULT_NAMES
    assert all(config.is_active is False for config in configs)
    assert [config.priority_rank for config in configs] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_seeding_is_idempotent(db_session: AsyncSession) -> None:
    assert await manager.ensure_seeded(db_session) is True
    assert await manager.ensure_seeded(db_session) is False
    assert await count_configs(db_session) == 5


@pytest.mark.asyncio
async def test_concurrent_seeding_race_is_handled_gracefully(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as loser:
        # The loser sees an empty table...
        assert await manager.has_any_config(loser) is False

        # ...a concurrent request seeds and commits first...
        async with session_factory() as winner:
            assert await manager.ensure_seeded(winner) is True

        # ...so the loser's insert hits IntegrityError, rolls back, and carries on.
        assert await manager.seed_default_configs(loser) is False
        configs = await manager.get_all_configs(loser)
        assert [config.name for config in configs] == DEFAULT_NAMES

    async with session_factory() as verifier:
        assert await count_configs(verifier) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "rank"), [("", 1), ("ValidName", 0)])
async def test_database_check_constraints(db_session: AsyncSession, name: str, rank: int) -> None:
    db_session.add(BookmakerConfigModel(name=name, priority_rank=rank, is_active=False))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# --------------------------------------------------------------------------- #
# Configuration API
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_config_seeds_and_never_exposes_api_key(client: AsyncClient) -> None:
    response = await client.get(f"{API}/config")
    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body] == DEFAULT_NAMES
    for item in body:
        assert "api_key_encrypted" not in item
        assert item["has_api_key"] is False


@pytest.mark.asyncio
async def test_create_config_hides_api_key(client: AsyncClient) -> None:
    secret = "gAAAA-secret-ciphertext"
    response = await client.post(
        f"{API}/config",
        json={
            "name": "DraftKings",
            "is_active": True,
            "api_key_encrypted": secret,
            "base_url": "https://sportsbook.draftkings.com",
            "priority_rank": 6,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "DraftKings"
    assert body["has_api_key"] is True
    assert "api_key_encrypted" not in body
    assert secret not in response.text


@pytest.mark.asyncio
async def test_create_rejects_case_insensitive_duplicate(client: AsyncClient) -> None:
    response = await client.post(f"{API}/config", json={"name": "pinnacle"})
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "NewBook", "unexpected": 1},
        {"name": ""},
        {"name": "bad/name"},
        {"name": "NewBook", "priority_rank": 0},
        {"name": "NewBook", "api_key_encrypted": "   "},
    ],
)
async def test_create_rejects_invalid_payloads(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post(f"{API}/config", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_config_persists_changes_case_insensitively(client: AsyncClient) -> None:
    body = await activate(client, "pinnacle", api_key_encrypted="cipher-text", priority_rank=2)
    assert body["name"] == "Pinnacle"
    assert body["is_active"] is True
    assert body["priority_rank"] == 2
    assert body["has_api_key"] is True
    assert "api_key_encrypted" not in body


@pytest.mark.asyncio
async def test_update_unknown_bookmaker_returns_404(client: AsyncClient) -> None:
    response = await client.put(f"{API}/Nope/config", json={"is_active": True})
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"is_active": None}, {"priority_rank": None}])
async def test_update_rejects_empty_or_null_patches(
    client: AsyncClient, payload: dict[str, Any]
) -> None:
    response = await client.put(f"{API}/Stake/config", json=payload)
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Universal extensibility
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sixth_custom_bookmaker_uses_generic_fallback(client: AsyncClient) -> None:
    created = await client.post(
        f"{API}/config", json={"name": "DraftKings", "is_active": True, "priority_rank": 1}
    )
    assert created.status_code == 201, created.text
    await activate(client, "Pinnacle")

    listing = await client.get(f"{API}/config")
    names = {item["name"] for item in listing.json()}
    assert names == {*DEFAULT_NAMES, "DraftKings"}
    assert bookmaker_registry.resolve("DraftKings") is GenericAdapter

    response = await client.post(
        f"{API}/compare-odds",
        json={**MATCH_CONTEXT, "odds": {"DraftKings": 2.2, "Pinnacle": 2.1, "Stake": 9.5}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["best_bookmaker"] == "DraftKings"
    assert body["best_odds"] == pytest.approx(2.2)
    assert body["mean_odds"] == pytest.approx(2.15)
    assert body["edge_percentage"] == pytest.approx((2.2 - 2.15) / 2.15 * 100, abs=1e-4)
    assert body["ignored_bookmakers"] == ["Stake"]
    assert body["considered_bookmakers"] == ["DraftKings", "Pinnacle"]
    assert body["placement_instruction"] == (
        "On DraftKings: Navigate to Football > Arsenal vs Chelsea and select Arsenal at odds 2.20"
    )
    assert body["route"] is None


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #


def test_find_best_odds_prefers_highest_odds_over_rank() -> None:
    configs = [make_config("Zeta", 1), make_config("alpha", 5)]
    result = find_best_odds({"Zeta": 2.5, "alpha": 2.6}, configs)
    assert result["best_bookmaker"] == "alpha"


def test_find_best_odds_breaks_ties_by_priority_rank() -> None:
    configs = [make_config("Zeta", 1), make_config("alpha", 2), make_config("Beta", 3)]
    result = find_best_odds({"Zeta": 2.5, "alpha": 2.5, "Beta": 2.5}, configs)
    assert result["best_bookmaker"] == "Zeta"
    assert result["edge_percentage"] == 0.0


def test_find_best_odds_breaks_rank_ties_case_insensitively() -> None:
    # A naive ordinal sort would pick "Beta" because "B" < "a".
    assert sorted(["alpha", "Beta"]) == ["Beta", "alpha"]
    configs = [make_config("Zeta", 1), make_config("Beta", 1), make_config("alpha", 1)]
    market = {"Zeta": 2.5, "Beta": 2.5, "alpha": 2.5}
    assert find_best_odds(market, configs)["best_bookmaker"] == "alpha"
    assert find_best_odds(market, list(reversed(configs)))["best_bookmaker"] == "alpha"
    assert find_best_odds(market, configs)["considered_bookmakers"] == ["alpha", "Beta", "Zeta"]


def test_find_best_odds_ignores_inactive_and_matches_case_insensitively() -> None:
    configs = [make_config("Pinnacle", 4), make_config("Stake", 1)]
    result = find_best_odds({"PINNACLE": 2.4, "stake": 2.0, "Ghost": 50.0}, configs)
    assert result["best_bookmaker"] == "Pinnacle"
    assert result["best_odds"] == pytest.approx(2.4)
    assert result["mean_market_odds"] == pytest.approx(2.2)
    assert result["edge_percentage"] == pytest.approx((2.4 - 2.2) / 2.2 * 100, abs=1e-4)
    assert result["ignored_bookmakers"] == ["Ghost"]


@pytest.mark.parametrize("bad_odds", [-1.5, math.nan, math.inf, -math.inf, "2.0", True])
def test_find_best_odds_rejects_invalid_odds(bad_odds: Any) -> None:
    configs = [make_config("Pinnacle", 1), make_config("Stake", 2)]
    with pytest.raises(ValueError):
        find_best_odds({"Pinnacle": 2.0, "Stake": bad_odds}, configs)


def test_find_best_odds_validates_even_ignored_odds() -> None:
    with pytest.raises(ValueError):
        find_best_odds({"Pinnacle": 2.0, "Ghost": math.nan}, [make_config("Pinnacle", 1)])


def test_find_best_odds_rejects_negative_priority_rank() -> None:
    with pytest.raises(ValueError):
        find_best_odds({"Pinnacle": 2.0}, [make_config("Pinnacle", -1)])


def test_find_best_odds_requires_an_active_match() -> None:
    with pytest.raises(ValueError):
        find_best_odds({"Ghost": 2.0, "Phantom": 3.0}, [make_config("Pinnacle", 1)])


def test_find_best_odds_rejects_case_insensitive_duplicates() -> None:
    with pytest.raises(ValueError):
        find_best_odds({"Stake": 2.0, "stake": 2.1}, [make_config("Stake", 1)])


# --------------------------------------------------------------------------- #
# NaN / Infinity safety
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_compare_odds_rejects_non_finite_tokens(client: AsyncClient, token: str) -> None:
    raw = (
        '{"sport": "Football", "league": "Premier League", "match": "Arsenal vs Chelsea", '
        '"market": "Match Odds", "selection": "Arsenal", '
        f'"odds": {{"Pinnacle": {token}, "Stake": 2.0}}}}'
    )
    response = await client.post(
        f"{API}/compare-odds", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "NaN" not in response.text
    assert "Infinity" not in response.text
    body = strict_json(response.text)
    assert any(error["loc"][-1] == "Pinnacle" for error in body["detail"])


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_omniroute_execute_rejects_non_finite_tokens(client: AsyncClient, token: str) -> None:
    raw = f'{{"bookmaker_name": "Pinnacle", "odds": {token}, "stake": 10}}'
    response = await client.post(
        f"{API}/omniroute/execute", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    strict_json(response.text)


@pytest.mark.asyncio
async def test_nan_safe_handler_sanitizes_nested_values() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "finite_number",
                "loc": ("body", "odds"),
                "msg": "Input should be a finite number",
                "input": {"a": [math.nan, {"b": math.inf}], "c": (1.5, -math.inf)},
                "ctx": {"limit": -math.inf},
            }
        ]
    )
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    response = await nan_safe_validation_exception_handler(request, exc)
    assert response.status_code == 422
    payload = strict_json(bytes(response.body))
    error = payload["detail"][0]
    assert error["loc"] == ["body", "odds"]
    assert error["input"] == {"a": ["nan", {"b": "inf"}], "c": [1.5, "-inf"]}
    assert error["ctx"]["limit"] == "-inf"


# --------------------------------------------------------------------------- #
# Comparison endpoint edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_compare_odds_requires_at_least_two_bookmakers(client: AsyncClient) -> None:
    response = await client.post(f"{API}/compare-odds", json={**MATCH_CONTEXT, "odds": {"Stake": 2.0}})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_compare_odds_with_no_active_bookmakers_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/compare-odds", json={**MATCH_CONTEXT, "odds": {"Stake": 2.0, "Pinnacle": 2.1}}
    )
    assert response.status_code == 422
    assert "active bookmaker" in response.json()["detail"]


@pytest.mark.asyncio
async def test_compare_odds_execute_requires_stake(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/compare-odds",
        json={**MATCH_CONTEXT, "odds": {"Stake": 2.0, "Pinnacle": 2.1}, "execute": True},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# OmniRoute
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_omniroute_sentinel_failure_is_deterministic(omniroute_client: OmniRouteClient) -> None:
    for _ in range(25):
        result = await omniroute_client.execute_bet(
            "Pinnacle", {"odds": FAILURE_SENTINEL_ODDS, "stake": 10.0}
        )
        assert result["status"] == "FAILED"
        assert result["executed_odds"] is None
        assert result["requested_odds"] == FAILURE_SENTINEL_ODDS
        assert result["transaction_id"].startswith("omr_")
        assert result["failure_reason"]
        assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_omniroute_success_path_applies_latency_jitter(
    omniroute_client: OmniRouteClient,
) -> None:
    result = await omniroute_client.execute_bet("Pinnacle", {"odds": 2.05, "stake": 10.0})
    assert result["status"] == "SUCCESS"
    assert result["executed_odds"] == pytest.approx(2.05)
    assert result["failure_reason"] is None
    lower = omniroute_client.base_latency_ms
    upper = omniroute_client.base_latency_ms + omniroute_client.jitter_ms
    assert lower <= result["latency_ms"] <= upper


@pytest.mark.asyncio
async def test_omniroute_transaction_ids_are_unique(omniroute_client: OmniRouteClient) -> None:
    ids = {
        (await omniroute_client.execute_bet("Stake", {"odds": 1.9}))["transaction_id"]
        for _ in range(20)
    }
    assert len(ids) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"odds": math.nan},
        {"odds": math.inf},
        {"odds": -2.0},
        {"odds": "2.0"},
        {"odds": True},
        {"odds": 2.0, "stake": -1.0},
    ],
)
async def test_omniroute_rejects_invalid_payloads(
    omniroute_client: OmniRouteClient, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await omniroute_client.execute_bet("Pinnacle", payload)


@pytest.mark.asyncio
async def test_omniroute_rejects_blank_bookmaker(omniroute_client: OmniRouteClient) -> None:
    with pytest.raises(ValueError):
        await omniroute_client.execute_bet("   ", {"odds": 2.0})


def test_omniroute_client_rejects_invalid_latency_settings() -> None:
    with pytest.raises(ValueError):
        OmniRouteClient(base_latency_ms=-1.0)
    with pytest.raises(ValueError):
        OmniRouteClient(jitter_ms=math.nan)


@pytest.mark.asyncio
async def test_api_omniroute_sentinel_returns_failed(client: AsyncClient) -> None:
    await activate(client, "Pinnacle")
    response = await client.post(
        f"{API}/omniroute/execute",
        json={"bookmaker_name": "Pinnacle", "odds": FAILURE_SENTINEL_ODDS, "stake": 25.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["executed_odds"] is None
    assert body["bookmaker_name"] == "Pinnacle"


@pytest.mark.asyncio
async def test_api_omniroute_success(client: AsyncClient) -> None:
    await activate(client, "Betfair")
    response = await client.post(
        f"{API}/omniroute/execute",
        json={"bookmaker_name": "betfair", "odds": 1.95, "stake": 25.0, "selection": "Arsenal"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["executed_odds"] == pytest.approx(1.95)
    assert body["bookmaker_name"] == "Betfair"


@pytest.mark.asyncio
async def test_api_omniroute_inactive_bookmaker_returns_409(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/omniroute/execute", json={"bookmaker_name": "Stake", "odds": 2.0, "stake": 10.0}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_api_omniroute_unknown_bookmaker_returns_404(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/omniroute/execute", json={"bookmaker_name": "Ghost", "odds": 2.0, "stake": 10.0}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_compare_and_route_executes_winner_and_surfaces_failure(client: AsyncClient) -> None:
    await activate(client, "Pinnacle")
    await activate(client, "Betfair")
    response = await client.post(
        f"{API}/compare-odds",
        json={
            **MATCH_CONTEXT,
            "odds": {"Pinnacle": FAILURE_SENTINEL_ODDS, "Betfair": FAILURE_SENTINEL_ODDS},
            "execute": True,
            "stake": 10.0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Identical odds: Pinnacle (rank 4) beats Betfair (rank 5).
    assert body["best_bookmaker"] == "Pinnacle"
    assert body["mean_odds"] == pytest.approx(FAILURE_SENTINEL_ODDS)
    assert body["edge_percentage"] == 0.0
    assert body["placement_instruction"] == (
        "On Pinnacle: Select Football → Premier League → Arsenal vs Chelsea → Match Odds "
        "→ Select Arsenal (1.01)"
    )
    assert body["route"]["status"] == "FAILED"
    assert body["route"]["bookmaker_name"] == "Pinnacle"
