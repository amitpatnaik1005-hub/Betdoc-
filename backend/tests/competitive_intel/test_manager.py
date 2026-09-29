"""CompetitiveIntelManager tests: scans, selectinload dashboard, cascades, reports."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import selectinload

from app.domain.competitive_intel.errors import CompetitiveIntelDomainError, SiteNotFoundError
from app.models.competitive_intel import CompetitorBotModel, DevSuggestionModel, FeatureGapAlertModel
from app.schemas.competitive_intel import DashboardResponse

pytestmark = pytest.mark.asyncio

FIXED_TIME = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


async def _count(session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return (await session.execute(statement)).scalar_one()


# ---------------------------------------------------------------- scans


async def test_trigger_scan_seeds_bots_gaps_and_suggestions(manager, db_session):
    summary = await manager.trigger_scan(db_session)

    assert summary == {
        "bots_created": 3,
        "bots_updated": 0,
        "gaps_created": 6,
        "gaps_refreshed": 0,
        "suggestions_created": 6,
    }
    bots = (await db_session.execute(select(CompetitorBotModel.target_site, CompetitorBotModel.status,
                                            CompetitorBotModel.last_scan_at))).all()
    assert sorted(b.target_site for b in bots) == ["actionnetwork.com", "oddsshark.com", "pinnacle.com"]
    assert all(b.status == "IDLE" for b in bots)
    assert all(b.last_scan_at is not None for b in bots)
    assert await _count(db_session, FeatureGapAlertModel) == 6
    assert await _count(db_session, DevSuggestionModel) == 6


async def test_trigger_scan_is_idempotent(manager, db_session):
    await manager.trigger_scan(db_session)
    summary = await manager.trigger_scan(db_session)

    assert summary["bots_created"] == 0
    assert summary["bots_updated"] == 3
    assert summary["gaps_created"] == 0
    assert summary["gaps_refreshed"] == 6
    assert summary["suggestions_created"] == 0
    assert await _count(db_session, CompetitorBotModel) == 3
    assert await _count(db_session, FeatureGapAlertModel) == 6
    assert await _count(db_session, DevSuggestionModel) == 6


async def test_trigger_scan_recovers_errored_bot(manager, session_factory):
    async with session_factory() as session:
        await session.execute(
            insert(CompetitorBotModel).values(
                id=uuid4(), name="Legacy Scout", target_site="oddsshark.com", status="ERROR"
            )
        )
        await session.commit()

    async with session_factory() as session:
        summary = await manager.trigger_scan(session)
    assert summary["bots_created"] == 2
    assert summary["bots_updated"] == 1

    async with session_factory() as session:
        row = (
            await session.execute(
                select(CompetitorBotModel.name, CompetitorBotModel.status, CompetitorBotModel.last_scan_at).where(
                    CompetitorBotModel.target_site == "oddsshark.com"
                )
            )
        ).one()
    assert row.name == "Legacy Scout"
    assert row.status == "IDLE"
    assert row.last_scan_at is not None


async def test_trigger_scan_restores_missing_suggestion(manager, session_factory):
    async with session_factory() as session:
        await manager.trigger_scan(session)
    async with session_factory() as session:
        gap_id = (
            await session.execute(
                select(FeatureGapAlertModel.id).where(FeatureGapAlertModel.missing_feature == "Sharp Money Indicators")
            )
        ).scalar_one()
        await session.execute(delete(DevSuggestionModel).where(DevSuggestionModel.gap_id == gap_id))
        await session.commit()

    async with session_factory() as session:
        summary = await manager.trigger_scan(session)
        assert summary["suggestions_created"] == 1
        assert await _count(session, DevSuggestionModel, DevSuggestionModel.gap_id == gap_id) == 1


# ---------------------------------------------------------------- dashboard


async def test_dashboard_eager_loads_suggestions_via_selectinload(manager, session_factory):
    async with session_factory() as session:
        await manager.trigger_scan(session)

    async with session_factory() as session:
        dashboard = await manager.get_dashboard(session)
    # Session is closed: any lazy load here would raise instead of silently doing IO.

    assert len(dashboard["bots"]) == 3
    assert len(dashboard["gaps"]) == 6
    assert all(len(gap.suggestions) == 1 for gap in dashboard["gaps"])
    assert all(1 <= gap.suggestions[0].priority <= 5 for gap in dashboard["gaps"])

    response = DashboardResponse.model_validate(dashboard)
    assert len(response.gaps) == 6
    assert response.gaps[0].suggestions[0].gap_id == response.gaps[0].id


async def test_dashboard_orders_gaps_by_severity(manager, db_session):
    await manager.trigger_scan(db_session)
    dashboard = await manager.get_dashboard(db_session)
    severities = [gap.severity for gap in dashboard["gaps"]]
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    assert severities == sorted(severities, key=rank.__getitem__)
    assert severities[0] == "HIGH"


async def test_dashboard_excludes_resolved_gaps(manager, session_factory):
    async with session_factory() as session:
        await manager.trigger_scan(session)
        await session.execute(
            update(FeatureGapAlertModel)
            .where(FeatureGapAlertModel.missing_feature == "Betting Limits Transparency")
            .values(is_resolved=True)
        )
        await session.commit()

    async with session_factory() as session:
        dashboard = await manager.get_dashboard(session)
    features = {gap.missing_feature for gap in dashboard["gaps"]}
    assert len(features) == 5
    assert "Betting Limits Transparency" not in features


async def test_unloaded_suggestions_raise_instead_of_lazy_loading(manager, db_session):
    await manager.trigger_scan(db_session)
    gap = (await db_session.execute(select(FeatureGapAlertModel).limit(1))).scalar_one()
    with pytest.raises(InvalidRequestError):
        _ = gap.suggestions


# ---------------------------------------------------------------- cascades


async def test_core_delete_cascades_suggestions_at_db_level(manager, session_factory):
    async with session_factory() as session:
        await manager.trigger_scan(session)
    async with session_factory() as session:
        gap_id = (await session.execute(select(FeatureGapAlertModel.id).limit(1))).scalar_one()
        assert await _count(session, DevSuggestionModel, DevSuggestionModel.gap_id == gap_id) == 1
        await session.execute(delete(FeatureGapAlertModel).where(FeatureGapAlertModel.id == gap_id))
        await session.commit()

    async with session_factory() as session:
        assert await _count(session, DevSuggestionModel, DevSuggestionModel.gap_id == gap_id) == 0
        assert await _count(session, DevSuggestionModel) == 5


async def test_orm_delete_cascades_suggestions(manager, session_factory):
    async with session_factory() as session:
        await manager.trigger_scan(session)
    async with session_factory() as session:
        gap = (
            await session.execute(
                select(FeatureGapAlertModel).options(selectinload(FeatureGapAlertModel.suggestions)).limit(1)
            )
        ).scalar_one()
        gap_id = gap.id
        await session.delete(gap)
        await session.commit()

    async with session_factory() as session:
        assert await _count(session, FeatureGapAlertModel, FeatureGapAlertModel.id == gap_id) == 0
        assert await _count(session, DevSuggestionModel, DevSuggestionModel.gap_id == gap_id) == 0


# ---------------------------------------------------------------- constraints


async def test_bot_status_check_constraint(session_factory):
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(CompetitorBotModel).values(id=uuid4(), name="Rogue", target_site="rogue.example", status="ASLEEP")
            )
        await session.rollback()
        assert await _count(session, CompetitorBotModel) == 0


async def test_bot_target_site_is_unique(session_factory):
    async with session_factory() as session:
        await session.execute(
            insert(CompetitorBotModel).values(id=uuid4(), name="A", target_site="dup.example", status="IDLE")
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(CompetitorBotModel).values(id=uuid4(), name="B", target_site="dup.example", status="IDLE")
            )
        await session.rollback()
        assert await _count(session, CompetitorBotModel) == 1


async def test_gap_severity_check_constraint(session_factory):
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(FeatureGapAlertModel).values(
                    id=uuid4(), site_name="OddsShark", missing_feature="X", severity="CRITICAL"
                )
            )
        await session.rollback()
        assert await _count(session, FeatureGapAlertModel) == 0


@pytest.mark.parametrize("priority", [0, 6])
async def test_suggestion_priority_check_constraint(session_factory, priority):
    gap_id = uuid4()  # UUID captured before any rollback
    async with session_factory() as session:
        await session.execute(
            insert(FeatureGapAlertModel).values(id=gap_id, site_name="Pinnacle", missing_feature="Y", severity="LOW")
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(DevSuggestionModel).values(
                    id=uuid4(), gap_id=gap_id, suggestion_text="Out of range", priority=priority
                )
            )
        await session.rollback()
        assert await _count(session, DevSuggestionModel, DevSuggestionModel.gap_id == gap_id) == 0


# ---------------------------------------------------------------- reports


@pytest.mark.parametrize("site_name", ["OddsShark", "oddsshark", "ODDSSHARK", "  OddsShark  "])
async def test_report_is_case_insensitive(manager, site_name):
    report = manager.generate_markdown_report(site_name, generated_at=FIXED_TIME)
    assert report == manager.generate_markdown_report("oddsshark", generated_at=FIXED_TIME)
    assert report.startswith("# Site Report: OddsShark\n")


@pytest.mark.parametrize("site_name", ["oddsshark", "actionnetwork", "pinnacle"])
async def test_report_structure(manager, site_name):
    report = manager.generate_markdown_report(site_name, generated_at=FIXED_TIME)
    lines = report.splitlines()

    assert lines[0].startswith("# Site Report")
    assert "- **Generated:** 2026-09-29T12:00:00+00:00" in lines
    header_index = next(i for i, line in enumerate(lines) if line.startswith("| Feature |"))
    assert lines[header_index + 1] == "| --- | --- | --- | --- |"
    rows = [line for line in lines[header_index + 2 :] if line.startswith("| ")]
    assert len(rows) == 3
    assert all(row.count("|") == 5 for row in rows)
    assert "### Recommendation" in lines
    assert lines.index("### Recommendation") > header_index
    assert report.endswith("\n")


async def test_report_unknown_site_raises_not_found(manager):
    with pytest.raises(SiteNotFoundError) as exc_info:
        manager.generate_markdown_report("Bet365")
    assert exc_info.value.site_name == "Bet365"
    assert "OddsShark" in exc_info.value.message


async def test_report_blank_site_raises_domain_error(manager):
    with pytest.raises(CompetitiveIntelDomainError) as exc_info:
        manager.generate_markdown_report("   ")
    assert not isinstance(exc_info.value, SiteNotFoundError)
