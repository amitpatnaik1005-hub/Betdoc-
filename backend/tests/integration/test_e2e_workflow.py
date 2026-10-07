"""End-to-end lifecycle: vault -> prediction -> bet -> settlement -> telemetry."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import build_orchestrator
from app.core.config import Settings
from app.core.encryption import EncryptionService
from app.domain.integration.models import ApiCredential, Base, BetStatus, EventResearch
from app.domain.integration.orchestrator import BettingHaltedError, OrchestratorService
from app.domain.integration.repositories import LabRepository
from app.domain.integration.schemas import ArenaMetrics, LabMetrics, OracleMetrics, SubsystemStatus, TelemetryResponse

PLAIN_API_KEY = "sk-live-1234567890abcdef"
EXPECTED_MASK = "sk-live-************cdef"
EVENT_ID = "evt-e2e-001"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def settings() -> Settings:
    url = URL.create(drivername="sqlite+aiosqlite", database=":memory:").render_as_string(hide_password=False)
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        DATABASE_URL=SecretStr(url),
        ENCRYPTION_KEY=SecretStr(Fernet.generate_key().decode("ascii")),
        SECRET_KEY=SecretStr("x" * 32),
        INGESTION_API_KEY=SecretStr("x" * 32),
    )


@pytest.fixture
def encryption(settings: Settings) -> EncryptionService:
    return EncryptionService(settings.encryption_key.get_secret_value())


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    # StaticPool: every session shares the single in-memory connection (otherwise each sees an empty DB).
    eng = create_async_engine(settings.database_url, poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()  # prevents "Task attached to a different loop" across tests


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def orchestrator(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    encryption: EncryptionService,
    settings: Settings,
) -> OrchestratorService:
    return build_orchestrator(session=session, session_factory=session_factory, encryption=encryption, settings=settings)


@pytest_asyncio.fixture
async def seeded_research(session: AsyncSession) -> EventResearch:
    research = EventResearch(event_id=EVENT_ID, expected_margin=7.0, spread_line=3.0, decimal_odds=2.0)
    session.add(research)
    await session.commit()
    return research


def _subsystem(telemetry: TelemetryResponse, name: str) -> SubsystemStatus:
    return next(s for s in telemetry.subsystems if s.name == name)


# --------------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_full_lifecycle(
    orchestrator: OrchestratorService,
    session: AsyncSession,
    encryption: EncryptionService,
    settings: Settings,
    seeded_research: EventResearch,
) -> None:
    # 1. Vault: encrypt + mask + round-trip
    stored = await orchestrator.store_api_key("pinnacle", PLAIN_API_KEY)
    assert stored.masked_key == EXPECTED_MASK
    row = (await session.execute(select(ApiCredential).where(ApiCredential.id == stored.id))).scalar_one()
    assert row.encrypted_key != PLAIN_API_KEY
    assert PLAIN_API_KEY not in row.encrypted_key
    assert encryption.decrypt_data(row.encrypted_key) == PLAIN_API_KEY
    assert encryption.encrypt_data(PLAIN_API_KEY) != row.encrypted_key  # Fernet is non-deterministic

    # 2. Oracle -> math model -> risk engine
    prediction = await orchestrator.execute_oracle_prediction_cycle(EVENT_ID)
    max_stake = settings.max_stake_fraction * settings.starting_bankroll
    assert 0.0 < prediction.probability < 1.0
    assert prediction.approved is True
    assert prediction.edge >= settings.min_edge
    assert 0.0 < prediction.recommended_stake <= max_stake
    assert prediction.implied_probability == pytest.approx(1.0 / seeded_research.decimal_odds)

    # 3. Place bet
    bet = await orchestrator.place_bet(prediction)
    assert bet.status is BetStatus.OPEN
    assert bet.stake == pytest.approx(prediction.recommended_stake)
    assert bet.pnl is None

    # 4. Settle bet (win)
    settled = await orchestrator.settle_bet(bet.id, "won")
    expected_pnl = bet.stake * (bet.decimal_odds - 1.0)
    assert settled.status is BetStatus.WON
    assert settled.pnl == pytest.approx(expected_pnl)
    assert settled.settled_at is not None

    # 5. Telemetry reflects settlement
    telemetry = await orchestrator.gather_dashboard_telemetry()
    assert telemetry.overall_status == "ok"
    assert all(s.status == "ok" and s.last_error is None for s in telemetry.subsystems)
    assert telemetry.realized_pnl == pytest.approx(expected_pnl)

    arena = _subsystem(telemetry, "arena").metrics
    assert isinstance(arena, ArenaMetrics)
    assert (arena.open_bets, arena.settled_bets) == (0, 1)
    assert arena.open_exposure == pytest.approx(0.0)
    assert arena.realized_pnl == pytest.approx(expected_pnl)

    lab = _subsystem(telemetry, "lab").metrics
    assert isinstance(lab, LabMetrics)
    assert lab.settled_bets == 1 and lab.hit_rate == pytest.approx(1.0)

    oracle = _subsystem(telemetry, "oracle").metrics
    assert isinstance(oracle, OracleMetrics)
    assert oracle.predictions_total == 1 and oracle.automated_predictions_enabled is True


@pytest.mark.asyncio
async def test_stop_loss_cascade_suspends_oracle_and_arena(
    orchestrator: OrchestratorService, seeded_research: EventResearch
) -> None:
    approved = await orchestrator.execute_oracle_prediction_cycle(EVENT_ID)
    assert approved.approved is True

    cascade = await orchestrator.trigger_stop_loss_cascade(limit_breached=-500.0)
    assert cascade.triggered is True
    assert {(c.subsystem, c.flag, c.enabled) for c in cascade.changes} == {
        ("oracle", "automated_predictions_enabled", False),
        ("arena", "live_betting_enabled", False),
    }

    suspended = await orchestrator.execute_oracle_prediction_cycle(EVENT_ID)
    assert suspended.approved is False and suspended.recommended_stake == 0.0
    with pytest.raises(BettingHaltedError):
        await orchestrator.place_bet(approved)

    telemetry = await orchestrator.gather_dashboard_telemetry()
    oracle = _subsystem(telemetry, "oracle").metrics
    arena = _subsystem(telemetry, "arena").metrics
    assert isinstance(oracle, OracleMetrics) and oracle.automated_predictions_enabled is False
    assert isinstance(arena, ArenaMetrics) and arena.live_betting_enabled is False


class _FailingLabRepository(LabRepository):
    async def metrics(self, session: AsyncSession) -> LabMetrics:
        raise RuntimeError("lab warehouse unreachable")


@pytest.mark.asyncio
async def test_telemetry_degrades_without_cancelling_healthy_subsystems(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    encryption: EncryptionService,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    degraded = build_orchestrator(
        session=session,
        session_factory=session_factory,
        encryption=encryption,
        settings=settings,
        lab=_FailingLabRepository(),
    )
    with caplog.at_level("ERROR"):
        telemetry = await degraded.gather_dashboard_telemetry()

    assert telemetry.overall_status == "degraded"
    lab = _subsystem(telemetry, "lab")
    assert lab.status == "error" and lab.metrics is None
    assert lab.last_error == "RuntimeError: lab warehouse unreachable"
    assert all(s.status == "ok" for s in telemetry.subsystems if s.name != "lab")
    assert telemetry.realized_pnl == pytest.approx(0.0)
    assert "subsystem=lab" in caplog.text
