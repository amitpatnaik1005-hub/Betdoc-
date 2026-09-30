"""ControlPanelManager tests: singleton race, partial updates, redaction safety, emergency stop."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.control_panel.errors import ControlPanelDomainError
from app.domain.control_panel.manager import EDITABLE_FIELDS
from app.models.control_panel import SystemSettingsModel
from app.schemas.control_panel import REDACTED, SECRET_FIELDS, SettingsRead

pytestmark = pytest.mark.asyncio

READ_ONLY_FIELDS = {"id", "last_emergency_stop_at", "created_at", "updated_at"}


async def _row(session_factory):
    async with session_factory() as session:
        settings = (
            await session.execute(select(SystemSettingsModel).where(SystemSettingsModel.id == 1))
        ).scalar_one()
        return SettingsRead.model_validate(settings)


async def _count(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(SystemSettingsModel))).scalar_one()


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ---------------------------------------------------------------- schema coverage


async def test_schemas_cover_every_column_dynamically():
    columns = {c.name for c in SystemSettingsModel.__table__.columns}
    assert set(SettingsRead.model_fields) == columns
    assert EDITABLE_FIELDS == columns - READ_ONLY_FIELDS
    assert SECRET_FIELDS == {"odds_api_key", "news_api_key"}


# ---------------------------------------------------------------- singleton


async def test_get_settings_creates_singleton_with_defaults(manager, db_session):
    settings = await manager.get_settings(db_session)
    snapshot = SettingsRead.model_validate(settings)

    assert snapshot.id == 1
    assert snapshot.developer_name == "Amit Ashok Kumar Patnaik"
    assert snapshot.theme == "auto"
    assert snapshot.bots_enabled is True
    assert snapshot.default_kelly_fraction == 0.25
    assert snapshot.max_daily_exposure == 500.0
    assert snapshot.odds_api_key is None
    assert snapshot.created_at is not None  # refresh trap avoided
    assert snapshot.updated_at is not None


async def test_get_settings_is_idempotent(manager, session_factory):
    async with session_factory() as session:
        await manager.get_settings(session)
    async with session_factory() as session:
        await manager.get_settings(session)
    assert await _count(session_factory) == 1


async def test_get_or_create_recovers_from_integrity_error_race(manager, session_factory, monkeypatch):
    # Another request wins the race and inserts the singleton first.
    async with session_factory() as winner:
        winner.add(SystemSettingsModel(id=1, developer_name="Race Winner"))
        await winner.commit()

    real_find = manager._find_settings
    calls = {"count": 0}

    async def stale_read_then_real(db):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # this request read before the winner committed
        return await real_find(db)

    monkeypatch.setattr(manager, "_find_settings", stale_read_then_real)

    async with session_factory() as loser:
        settings = await manager._get_or_create_settings(loser)
        developer_name = settings.developer_name
        created_at = settings.created_at

    assert calls["count"] == 2  # stale read, IntegrityError, rollback, re-query
    assert developer_name == "Race Winner"
    assert created_at is not None
    assert await _count(session_factory) == 1


async def test_get_or_create_raises_when_row_cannot_be_loaded(manager, session_factory, monkeypatch):
    async with session_factory() as other:
        other.add(SystemSettingsModel(id=1))
        await other.commit()

    async def always_missing(db):
        return None

    monkeypatch.setattr(manager, "_find_settings", always_missing)
    async with session_factory() as session:
        with pytest.raises(ControlPanelDomainError):
            await manager._get_or_create_settings(session)


# ---------------------------------------------------------------- updates


async def test_update_settings_is_partial(manager, session_factory):
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": "odds-real", "news_api_key": "news-real"})
    async with session_factory() as session:
        settings = await manager.update_settings(session, {"theme": "dark", "max_bet_size": 25.0})
        theme, max_bet = settings.theme, settings.max_bet_size

    assert (theme, max_bet) == ("dark", 25.0)
    stored = await _row(session_factory)
    assert stored.odds_api_key == "odds-real"
    assert stored.news_api_key == "news-real"
    assert stored.accent_color == "#3b82f6"
    assert stored.default_kelly_fraction == 0.25


async def test_update_ignores_redacted_placeholder(manager, session_factory):
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": "odds-real", "news_api_key": "news-real"})
    async with session_factory() as session:
        await manager.update_settings(
            session, {"odds_api_key": REDACTED, "news_api_key": "news-rotated", "reduce_motion": True}
        )

    stored = await _row(session_factory)
    assert stored.odds_api_key == "odds-real"
    assert stored.news_api_key == "news-rotated"
    assert stored.reduce_motion is True


async def test_update_can_clear_api_keys(manager, session_factory):
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": "odds-real", "news_api_key": "news-real"})
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": None, "news_api_key": "   "})
    stored = await _row(session_factory)
    assert stored.odds_api_key is None
    assert stored.news_api_key is None


async def test_update_with_only_redacted_values_changes_nothing(manager, session_factory):
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": "odds-real"})
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": REDACTED})
    assert (await _row(session_factory)).odds_api_key == "odds-real"


@pytest.mark.parametrize("kelly", [1.5, -0.1, float("inf")])
async def test_update_rejects_invalid_kelly_fraction(manager, session_factory, kelly):
    async with session_factory() as session:
        await manager.get_settings(session)
    async with session_factory() as session:
        with pytest.raises(ControlPanelDomainError):
            await manager.update_settings(session, {"default_kelly_fraction": kelly})
    assert (await _row(session_factory)).default_kelly_fraction == 0.25


@pytest.mark.parametrize("field", sorted(READ_ONLY_FIELDS | {"nonexistent_setting"}))
async def test_update_rejects_unknown_and_read_only_fields(manager, db_session, field):
    with pytest.raises(ControlPanelDomainError):
        await manager.update_settings(db_session, {field: 1})


@pytest.mark.parametrize("field", ["theme", "bots_enabled", "max_bet_size", "developer_name"])
async def test_update_rejects_null_for_required_settings(manager, db_session, field):
    with pytest.raises(ControlPanelDomainError):
        await manager.update_settings(db_session, {field: None})


async def test_update_refreshes_updated_at(manager, db_session):
    settings = await manager.update_settings(db_session, {"theme": "light"})
    assert settings.updated_at is not None
    assert settings.theme == "light"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("default_kelly_fraction", 1.5),
        ("default_kelly_fraction", -0.5),
        ("global_stop_loss", -1.0),
        ("max_bet_size", -1.0),
        ("max_daily_exposure", -1.0),
    ],
)
async def test_storage_check_constraints_block_invalid_risk_values(manager, session_factory, column, value):
    async with session_factory() as session:
        await manager.get_settings(session)
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(SystemSettingsModel).where(SystemSettingsModel.id == 1).values({column: value})
            )
        await session.rollback()


async def test_storage_rejects_second_singleton_row(session_factory):
    async with session_factory() as session:
        session.add(SystemSettingsModel(id=2))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    assert await _count(session_factory) == 0


# ---------------------------------------------------------------- emergency stop


async def test_emergency_stop_zeroes_exposure_and_disables_bots(manager, session_factory):
    async with session_factory() as session:
        await manager.update_settings(session, {"odds_api_key": "odds-real"})
    before = datetime.now(UTC)

    async with session_factory() as session:
        settings = await manager.emergency_stop(session)
        snapshot = SettingsRead.model_validate(settings)

    assert snapshot.bots_enabled is False
    assert snapshot.max_daily_exposure == 0.0
    assert snapshot.last_emergency_stop_at is not None
    halted_at = _as_aware(snapshot.last_emergency_stop_at)
    assert (halted_at - before).total_seconds() >= -1
    assert (datetime.now(UTC) - halted_at).total_seconds() < 60

    stored = await _row(session_factory)
    assert stored.bots_enabled is False
    assert stored.max_daily_exposure == 0.0
    assert stored.last_emergency_stop_at is not None
    assert stored.odds_api_key == "odds-real"


async def test_emergency_stop_creates_singleton_if_missing(manager, db_session):
    settings = await manager.emergency_stop(db_session)
    assert settings.id == 1
    assert settings.bots_enabled is False
