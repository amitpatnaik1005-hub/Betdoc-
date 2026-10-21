"""Group 100: Scout Oracle's guardrail, local model (Ollama, mocked) and rule-engine fallback, and its additive contract.

No real model or network is used: Ollama is an ``httpx.MockTransport``. Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.domain.oracle_scout.errors import ScoutDomainError
from app.domain.oracle_scout.manager import OracleScoutManager, ScoutFacts
from app.schemas.oracle_scout import ScoutChatRequest, ScoutChatResponse
from app.services.llm.ollama_client import REFUSAL, OllamaScoutClient, actions_for, is_off_topic

FACTS = ScoutFacts(bankroll=50_000.0, exposure=2_500.0, daily_pnl=1_200.0, open_positions=2, win_rate_pct=68.5, stop_loss_status="ARMED")
ROUTES = {"/oracle", "/lab", "/vault", "/archive"}


def _settings(**overrides: Any):  # noqa: ANN202
    return get_settings().model_copy(update={"SCOUT_LLM_RETRY_AFTER_SECONDS": 30.0, **overrides})


def _ollama(calls: list[httpx.Request], *, up: bool = True, reply: str = "Arsenal 2-1 Chelsea is the model's likeliest score.") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not up:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}]})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": reply}})
    return httpx.MockTransport(handler)


def _db() -> MagicMock:
    db = MagicMock()
    db.add, db.commit, db.refresh, db.rollback = MagicMock(), AsyncMock(), AsyncMock(), AsyncMock()
    return db


def _manager(transport: httpx.MockTransport, **settings: Any) -> OracleScoutManager:
    return OracleScoutManager(llm=OllamaScoutClient(_settings(**settings), transport=transport))


# --------------------------------------------------------------------------- the guardrail
@pytest.mark.parametrize("question", [
    "Write a poem about the french revolution and Napoleon",  # "revolution" must not read as "ev"
    "What is the capital of France?",
    "Write python code for a flask app",
    "Should I buy bitcoin or stocks this week?",
])
def test_off_topic_questions_are_refused(question: str) -> None:
    assert is_off_topic(question)


@pytest.mark.parametrize("question", [
    "Write a poem about Arsenal's title win",  # off-topic shape, sporting subject: allowed
    "What are fair odds for Arsenal vs Chelsea over 2.5 goals?",
    "How much should I stake from my bankroll?",
    "Explain CLV",
    "hello",  # neither: the model's own system prompt holds the line
])
def test_sport_and_betdoc_questions_pass(question: str) -> None:
    assert not is_off_topic(question)


@pytest.mark.asyncio
async def test_refusal_never_reaches_the_model() -> None:
    calls: list[httpx.Request] = []
    entry, actions, source = await _manager(_ollama(calls)).chat(_db(), "Write a poem about the french revolution", "vault", None, FACTS)
    assert (entry.oracle_response, actions, source) == (REFUSAL, [], "guardrail") and calls == []


# --------------------------------------------------------------------------- the local model
@pytest.mark.asyncio
async def test_the_local_model_answers_with_the_live_book_as_facts() -> None:
    calls: list[httpx.Request] = []
    entry, actions, source = await _manager(_ollama(calls)).chat(_db(), "Predict the score of Arsenal vs Chelsea", "oracle", uuid.uuid4(), FACTS)
    assert source == "ollama" and entry.oracle_response.startswith("Arsenal 2-1 Chelsea")
    chat = json.loads(next(c for c in calls if c.url.path == "/api/chat").content)
    assert chat["model"] == get_settings().SCOUT_LLM_MODEL and chat["stream"] is False
    system = chat["messages"][0]["content"]
    assert "Amit Ashok Kumar Patnaik" in system and '"bankroll": 50000.0' in system and "Never invent" in system
    assert chat["messages"][1] == {"role": "user", "content": "Predict the score of Arsenal vs Chelsea"}
    assert {a["route"] for a in actions} <= ROUTES and any(a["type"] == "VIEW_PREDICTION" for a in actions)


@pytest.mark.asyncio
async def test_an_anonymous_asker_gives_the_model_no_figures() -> None:
    calls: list[httpx.Request] = []
    await _manager(_ollama(calls)).chat(_db(), "Is 2.10 value on the draw?", None, None, None)
    system = json.loads(next(c for c in calls if c.url.path == "/api/chat").content)["messages"][0]["content"]
    assert "Live facts (INR): none" in system


@pytest.mark.asyncio
async def test_an_unreachable_model_falls_back_and_is_not_retried_at_once() -> None:
    calls: list[httpx.Request] = []
    manager = _manager(_ollama(calls, up=False))
    entry, actions, source = await manager.chat(_db(), "Audit my current bankroll risk", "vault", uuid.uuid4(), FACTS)
    assert source == "quant_nlg" and "₹50,000.00" in entry.oracle_response and "Stop-loss: ARMED" in entry.oracle_response
    assert any(a["route"] == "/vault" for a in actions)
    probes = len(calls)
    await manager.chat(_db(), "And my exposure?", "vault", uuid.uuid4(), FACTS)
    assert len(calls) == probes  # remembered as down for SCOUT_LLM_RETRY_AFTER_SECONDS: no second probe


@pytest.mark.asyncio
async def test_a_disabled_model_is_never_called() -> None:
    calls: list[httpx.Request] = []
    _, _, source = await _manager(_ollama(calls), SCOUT_LLM_ENABLED=False).chat(_db(), "Build a 3-leg football parlay", None, None, None)
    assert source == "quant_nlg" and calls == []


# --------------------------------------------------------------------------- the rule engine
@pytest.mark.asyncio
async def test_rule_engine_prices_quoted_odds_but_not_market_lines() -> None:
    manager = _manager(_ollama([], up=False))
    entry, _, _ = await manager.chat(_db(), "Is 2.10 value on the draw?", None, None, None)
    assert "2.10 implies 47.6%" in entry.oracle_response
    entry, _, _ = await manager.chat(_db(), "Fair odds for over 2.5 goals at 1.95?", None, None, None)
    assert "1.95 implies 51.3%" in entry.oracle_response and "2.50 implies" not in entry.oracle_response
    entry, _, _ = await manager.chat(_db(), "How much should I stake?", "vault", None, None)
    assert "₹" not in entry.oracle_response  # anonymous: no figures, never an invented bankroll


@pytest.mark.asyncio
async def test_blank_message_is_rejected() -> None:
    with pytest.raises(ScoutDomainError):
        await _manager(_ollama([])).chat(_db(), "   ", None, None)


# --------------------------------------------------------------------------- action cards and the contract
def test_action_cards_point_only_at_real_pages() -> None:
    cards = actions_for("Audit my 3-leg parlay slip, predict the score and check my Kelly stake and CLV history")
    assert len(cards) == 3 and {c["route"] for c in cards} <= ROUTES
    assert [c["type"] for c in cards] == ["LOAD_PARLAY", "OPEN_CONSULTANT", "VIEW_PREDICTION"]
    assert actions_for("hello") == []


def test_contract_is_additive_and_routes_stay_in_app() -> None:
    req = ScoutChatRequest(user_message="Predict Arsenal score", page_context="arena")
    assert (req.user_message, req.page_context) == ("Predict Arsenal score", "arena")
    history_id = uuid.uuid4()
    legacy = ScoutChatResponse(response_text="ok", history_id=history_id)
    assert legacy.actions == [] and legacy.source == "quant_nlg"  # old callers keep working
    res = ScoutChatResponse(response_text="ok", history_id=history_id, source="ollama",
                            actions=[{"type": "NAVIGATE", "label": "Open The Vault", "route": "/vault"}])
    assert res.actions[0].route == "/vault"
    for bad in ("https://evil.example", "//evil.example", "//evil", "javascript:alert(1)"):
        with pytest.raises(ValidationError):
            ScoutChatResponse(response_text="x", history_id=history_id, actions=[{"type": "NAVIGATE", "label": "x", "route": bad}])
    with pytest.raises(ValidationError):
        ScoutChatResponse(response_text="x", history_id=history_id, source="gpt")
