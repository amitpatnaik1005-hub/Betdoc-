"""Group 86: the emergency fortress (KAUTILYA, CHANAKYA & SHIVAJI). Developer Credit: Amit Ashok Kumar Patnaik.

The breaker's mathematics (no USD without a measured rate, no drawdown without a measured bankroll), the SHA-256
chain, the kill and resume lifecycle through the API (the Control Panel halted and restored with it), a book lock,
an automatic trip, a streak trip measured from the CFO ledger alone, tamper detection, and the lock the real-money
gateway reads.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.domain.control_panel.emergency_circuit_math import GENESIS_AUDIT_HASH, compute_emergency_audit_hash, evaluate_auto_drawdown_breaker
from app.models.bookmakers import BookmakerConfigModel
from app.models.cfo_vault import AuditEvent, AuditLog
from app.models.emergency_kill_switch import EmergencyKillSwitchAuditModel
from app.schemas.control_panel import BookmakerLockRequest
from app.services.emergency_kill_switch_service import EmergencyKillSwitchService, book_key, bookmaker_locked

BASE = "/api/v1/control-panel"
DEVELOPER = "Amit Ashok Kumar Patnaik"
COOLDOWNS = {"drawdown_cooldown_minutes": 240, "streak_cooldown_minutes": 120}


class StubRedis:
    """Just what a book lock touches: the lock keys, and the FX hashes (empty: no USD rate)."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        self.keys[key] = value

    async def delete(self, key: str) -> None:
        self.keys.pop(key, None)

    async def exists(self, key: str) -> int:
        return int(key in self.keys)

    async def hgetall(self, key: str) -> dict[str, Any]:  # noqa: ARG002
        return {}

    async def hget(self, key: str, field: str) -> None:  # noqa: ARG002
        return None


def test_the_breaker_mathematics() -> None:
    healthy = evaluate_auto_drawdown_breaker(reference_bankroll_inr=Decimal("100000.00"), rolling_loss_inr=Decimal("8000.00"), threshold_drawdown_pct=15.0,
                                             window_hours=24, consecutive_losses=2, max_consecutive_losses=5, **COOLDOWNS)
    assert not healthy.should_trip_breaker and healthy.lockout_mode == "ARMED" and healthy.drawdown_pct == 8.0 and healthy.reference_bankroll_usd is None
    tripped = evaluate_auto_drawdown_breaker(reference_bankroll_inr=Decimal("100000.00"), rolling_loss_inr=Decimal("16500.00"), threshold_drawdown_pct=15.0,
                                             window_hours=24, consecutive_losses=3, max_consecutive_losses=5, usd_inr_rate=Decimal("84.00"), **COOLDOWNS)
    assert tripped.should_trip_breaker and tripped.lockout_mode == "AUTO_DRAWDOWN_TRIPPED" and tripped.drawdown_pct == 16.5
    assert tripped.reference_bankroll_usd == Decimal("1190.48") and tripped.rolling_loss_usd == Decimal("196.43") and tripped.recommended_cooldown_minutes == 240
    unmeasured = evaluate_auto_drawdown_breaker(reference_bankroll_inr=None, rolling_loss_inr=Decimal("0.00"), threshold_drawdown_pct=15.0, window_hours=24,
                                                consecutive_losses=0, max_consecutive_losses=5, **COOLDOWNS)
    assert unmeasured.drawdown_pct is None and not unmeasured.should_trip_breaker and "no funded bankroll" in unmeasured.verdict_summary
    streak = evaluate_auto_drawdown_breaker(reference_bankroll_inr=Decimal("100000.00"), rolling_loss_inr=Decimal("4000.00"), threshold_drawdown_pct=15.0,
                                            window_hours=24, consecutive_losses=5, max_consecutive_losses=5, **COOLDOWNS)
    assert streak.should_trip_breaker and streak.lockout_mode == "STREAK_COOLDOWN" and streak.recommended_cooldown_minutes == 120


def test_the_audit_hash_chains() -> None:
    fields = {"event_type": "GLOBAL_KILL_ENGAGED", "target_scope": "GLOBAL", "triggered_by": DEVELOPER, "reason": "Test kill switch", "drawdown_pct": 12.5,
              "protected_capital_inr": Decimal("87500.00"), "timestamp_iso": "2026-10-21T12:00:00.000000Z"}
    h1 = compute_emergency_audit_hash(prev_hash=GENESIS_AUDIT_HASH, **fields)
    assert len(h1) == 64 and h1 == compute_emergency_audit_hash(prev_hash=GENESIS_AUDIT_HASH, **fields)  # deterministic
    assert compute_emergency_audit_hash(prev_hash=h1, **fields) != h1  # the link changes the hash
    assert compute_emergency_audit_hash(prev_hash=GENESIS_AUDIT_HASH, **{**fields, "reason": "edited"}) != h1


@pytest.mark.asyncio
async def test_kill_and_resume_through_the_api_with_a_verified_chain(client) -> None:  # noqa: ANN001 - the conftest client
    dash = (await client.get(f"{BASE}/emergency-dashboard")).json()
    assert dash["state"]["global_kill_engaged"] is False and dash["state"]["lockout_mode"] == "ARMED" and dash["architect"] == DEVELOPER
    assert dash["state"]["reference_bankroll_inr"] is None and dash["state"]["current_drawdown_pct"] is None  # empty ledgers measure nothing
    assert dash["state"]["auto_kill_drawdown_pct"] == get_settings().EMERGENCY_AUTO_KILL_DRAWDOWN_PCT and dash["chain_intact"] is True

    killed = await client.post(f"{BASE}/emergency-kill", json={"triggered_by": DEVELOPER, "reason": "Manual emergency halt during volatility spike."})
    assert killed.status_code == 200
    body = killed.json()
    assert body["state"]["global_kill_engaged"] and body["state"]["lockout_mode"] == "MANUAL_KILL" and body["state"]["parimatch_locked"] and body["state"]["onexbet_locked"]
    assert [a["event_type"] for a in body["audits"]] == ["GLOBAL_KILL_ENGAGED"] and body["audits"][0]["prev_hash"] == GENESIS_AUDIT_HASH
    halted = (await client.get(BASE)).json()
    assert halted["bots_enabled"] is False and halted["max_daily_exposure"] == 0.0  # the Control Panel halted with it

    resumed = await client.post(f"{BASE}/emergency-resume", json={"triggered_by": DEVELOPER, "reason": "Volatility cleared; resuming operations.",
                                                                    "restore_daily_exposure_inr": 7500.0})
    assert resumed.status_code == 200
    body2 = resumed.json()
    assert not body2["state"]["global_kill_engaged"] and body2["state"]["lockout_mode"] == "ARMED" and not body2["state"]["parimatch_locked"]
    assert [a["event_type"] for a in body2["audits"]] == ["GLOBAL_KILL_RESUMED", "GLOBAL_KILL_ENGAGED"]
    assert body2["audits"][0]["prev_hash"] == body2["audits"][1]["entry_hash"] and body2["chain_intact"] is True
    restored = (await client.get(BASE)).json()
    assert restored["bots_enabled"] is True and restored["max_daily_exposure"] == 7500.0


@pytest.mark.asyncio
async def test_a_book_lock_and_an_automatic_drawdown_trip(client) -> None:  # noqa: ANN001
    locked = (await client.post(f"{BASE}/bookmaker-lock", json={"bookmaker": "PARIMATCH", "locked": True, "reason": "Parimatch live cricket odds drift."})).json()
    assert locked["state"]["parimatch_locked"] is True and locked["state"]["onexbet_locked"] is False
    assert locked["audits"][0]["event_type"] == "BOOKMAKER_LOCKED" and locked["audits"][0]["target_scope"] == "PARIMATCH"
    tripped = (await client.post(f"{BASE}/auto-drawdown-check", json={"reference_bankroll_inr": 100000.0, "rolling_loss_inr": 18000.0, "threshold_drawdown_pct": 15.0,
                                                                       "window_hours": 24, "consecutive_losses": 3, "max_consecutive_losses": 5})).json()
    assert tripped["tripped"] and tripped["lockout_mode"] == "AUTO_DRAWDOWN_TRIPPED" and tripped["drawdown_pct"] == 18.0 and tripped["measured"] is False
    assert tripped["state"]["global_kill_engaged"] and tripped["state"]["onexbet_locked"] and tripped["rolling_loss_usd"] is None  # no fresh USD rate
    assert (await client.get(BASE)).json()["max_daily_exposure"] == 0.0


@pytest.mark.asyncio
async def test_a_streak_measured_from_the_ledger_trips_and_tampering_breaks_the_chain(client, session_factory: async_sessionmaker[AsyncSession]) -> None:  # noqa: ANN001
    async with session_factory() as session:
        for _ in range(5):
            session.add(AuditLog(event=AuditEvent.SETTLED, reason="settled", pnl_inr=Decimal("-500.00")))
        await session.commit()
    checked = (await client.post(f"{BASE}/auto-drawdown-check", json={})).json()
    assert checked["measured"] is True and checked["consecutive_losses"] == 5 and checked["rolling_loss_inr"] == 2500.0
    assert checked["tripped"] and checked["lockout_mode"] == "STREAK_COOLDOWN" and checked["drawdown_pct"] is None  # no bankroll: no percentage
    async with session_factory() as session:
        await session.execute(update(EmergencyKillSwitchAuditModel).values(reason="nothing happened"))
        await session.commit()
    assert (await client.get(f"{BASE}/emergency-dashboard")).json()["chain_intact"] is False


@pytest.mark.asyncio
async def test_a_locked_book_is_refused_and_its_config_follows(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings, redis, service = get_settings(), StubRedis(), EmergencyKillSwitchService()
    async with session_factory() as session:
        session.add(BookmakerConfigModel(id=uuid.uuid4(), name="Parimatch"))
        await session.commit()
        await service.set_bookmaker_lock(session, redis, settings, BookmakerLockRequest(bookmaker="PARIMATCH", locked=True))
        assert await bookmaker_locked(redis, settings, "parimatch") and not await bookmaker_locked(redis, settings, "onexbet")
        assert book_key(settings, "parimatch") in redis.keys
        row = (await session.execute(select(BookmakerConfigModel))).scalar_one()
        assert row.is_active is False
        await service.set_bookmaker_lock(session, redis, settings, BookmakerLockRequest(bookmaker="PARIMATCH", locked=False))
        assert not await bookmaker_locked(redis, settings, "parimatch")
        await session.refresh(row)
        assert row.is_active is True
    assert not await bookmaker_locked(None, settings, "parimatch")  # no Redis: nothing to read
