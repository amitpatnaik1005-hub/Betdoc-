"""OracleScoutManager tests: context branching, persistence, chronological history."""

import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.domain.oracle_scout.errors import ScoutDomainError
from app.domain.oracle_scout.manager import (
    MAX_HISTORY_LIMIT,
    MAX_MESSAGE_LENGTH,
    OracleScoutManager,
    normalize_page_context,
    resolve_zone,
)
from app.models.oracle_scout import OracleScoutHistoryModel

pytestmark = pytest.mark.asyncio


async def _count(session) -> int:
    return (await session.execute(select(func.count()).select_from(OracleScoutHistoryModel))).scalar_one()


# ---------------------------------------------------------------- context branching


@pytest.mark.parametrize("page_context", ["/vault", "/VAULT/deposits", "  The-Vault  "])
async def test_vault_context_mentions_bankroll(manager, db_session, page_context):
    entry = await manager.chat(db_session, "How much should I stake?", page_context, None)
    assert "bankroll management" in entry.oracle_response
    assert "risk" in entry.oracle_response


@pytest.mark.parametrize("page_context", ["/arena", "/Arena/live/match-42"])
async def test_arena_context_mentions_live_odds(manager, db_session, page_context):
    entry = await manager.chat(db_session, "Is the line moving?", page_context, None)
    assert "live odds" in entry.oracle_response
    assert "in-play tracking" in entry.oracle_response


@pytest.mark.parametrize("page_context", ["/dashboard", "/lab", None, "", "   "])
async def test_default_context_mentions_sharp_betting(manager, db_session, page_context):
    entry = await manager.chat(db_session, "Any tips?", page_context, None)
    assert "sharp betting" in entry.oracle_response
    assert "bankroll management" not in entry.oracle_response
    assert "live odds" not in entry.oracle_response


async def test_vault_takes_precedence_over_arena():
    assert resolve_zone("/vault/arena-bets") == "vault"


async def test_normalize_page_context_handles_none_and_blank():
    assert normalize_page_context(None) is None
    assert normalize_page_context("   ") is None
    assert normalize_page_context("  /Vault ") == "/vault"


# ---------------------------------------------------------------- persistence


async def test_chat_persists_and_refreshes_server_defaults(manager, db_session):
    user_id = uuid4()
    entry = await manager.chat(db_session, "  What is value betting?  ", "/Arena", user_id)
    entry_id, stored_user, stored_context, stored_message, created_at = (
        entry.id,
        entry.user_id,
        entry.page_context,
        entry.user_message,
        entry.created_at,
    )

    assert created_at is not None  # refresh loaded the server_default
    assert stored_user == user_id
    assert stored_context == "/arena"
    assert stored_message == "What is value betting?"

    persisted = (
        await db_session.execute(
            select(OracleScoutHistoryModel.id).where(OracleScoutHistoryModel.id == entry_id)
        )
    ).scalar_one()
    assert persisted == entry_id


async def test_chat_with_null_context_is_saved_as_null(manager, db_session):
    entry = await manager.chat(db_session, "Hello Scout", None, None)
    assert entry.page_context is None
    assert entry.user_id is None
    assert await _count(db_session) == 1


async def test_chat_rejects_blank_message(manager, db_session):
    with pytest.raises(ScoutDomainError):
        await manager.chat(db_session, "   ", "/vault", None)
    assert await _count(db_session) == 0


async def test_chat_rejects_oversized_message(manager, db_session):
    with pytest.raises(ScoutDomainError):
        await manager.chat(db_session, "x" * (MAX_MESSAGE_LENGTH + 1), None, None)
    assert await _count(db_session) == 0


async def test_chat_awaits_mock_latency(db_session):
    slow_manager = OracleScoutManager(latency_s=0.05)
    started = time.perf_counter()
    await slow_manager.chat(db_session, "Latency check", None, None)
    assert time.perf_counter() - started >= 0.04


async def test_negative_latency_is_rejected():
    with pytest.raises(ValueError):
        OracleScoutManager(latency_s=-1)


# ---------------------------------------------------------------- history


async def test_history_returns_most_recent_in_chronological_order(manager, db_session, insert_history):
    user_id = uuid4()
    ids = await insert_history(5, user_id=user_id)

    history = await manager.get_history(db_session, user_id=user_id, limit=3)

    # DESC + LIMIT picks the newest three; reversing returns them oldest-to-newest.
    assert [entry.id for entry in history] == ids[2:]
    assert [entry.user_message for entry in history] == ["message 2", "message 3", "message 4"]
    timestamps = [entry.created_at for entry in history]
    assert timestamps == sorted(timestamps)


async def test_history_without_limit_pressure_returns_everything_in_order(manager, db_session, insert_history):
    user_id = uuid4()
    ids = await insert_history(4, user_id=user_id)
    history = await manager.get_history(db_session, user_id=user_id)
    assert [entry.id for entry in history] == ids


async def test_history_is_scoped_per_user(manager, db_session, insert_history):
    user_a, user_b = uuid4(), uuid4()
    ids_a = await insert_history(2, user_id=user_a)
    ids_b = await insert_history(3, user_id=user_b)
    anonymous_ids = await insert_history(1, user_id=None)

    assert [e.id for e in await manager.get_history(db_session, user_id=user_a)] == ids_a
    assert [e.id for e in await manager.get_history(db_session, user_id=user_b)] == ids_b
    assert [e.id for e in await manager.get_history(db_session, user_id=None)] == anonymous_ids


async def test_history_empty_for_unknown_user(manager, db_session, insert_history):
    await insert_history(2, user_id=uuid4())
    assert await manager.get_history(db_session, user_id=uuid4()) == []


@pytest.mark.parametrize("limit", [0, -1, MAX_HISTORY_LIMIT + 1])
async def test_history_rejects_invalid_limit(manager, db_session, limit):
    with pytest.raises(ScoutDomainError):
        await manager.get_history(db_session, user_id=None, limit=limit)


def test_briefings_quote_the_live_book_and_odds_maths():
    from app.domain.oracle_scout.manager import ScoutFacts, compose_response

    facts = ScoutFacts(bankroll=10_000.0, exposure=500.0, daily_pnl=-120.0, open_positions=2, win_rate_pct=55.0, stop_loss_status="ARMED")
    vault = compose_response("vault", "how much should I stake?", facts)
    assert "bankroll management" in vault  # the guidance stays...
    assert "INR 100.00 to INR 200.00" in vault and "(5.0%)" in vault  # ...and quotes the user's own numbers
    arena = compose_response("arena", "is 2.50 value here?", facts)
    assert "2.50 implies 40.0%" in arena and "2 open positions" in arena
    assert "Your book" not in compose_response("default", "hello")  # anonymous callers get no figures
