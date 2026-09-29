"""CompetitiveIntelManager: SPY-BOT's competitor scans, gap alerts and reports."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.competitive_intel.errors import CompetitiveIntelDomainError, SiteNotFoundError
from app.models.competitive_intel import (
    BotStatus,
    CompetitorBotModel,
    DevSuggestionModel,
    FeatureGapAlertModel,
)

logger = logging.getLogger("betdoc.spy")

MAX_SITE_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class FeatureBlueprint:
    name: str
    betdoc_has: bool
    severity: str | None = None
    suggestion: str | None = None
    priority: int | None = None


@dataclass(frozen=True, slots=True)
class SiteProfile:
    key: str
    display_name: str
    target_site: str
    bot_name: str
    features: tuple[FeatureBlueprint, ...]

    @property
    def gaps(self) -> tuple[FeatureBlueprint, ...]:
        return tuple(feature for feature in self.features if not feature.betdoc_has)


SITE_CATALOG: dict[str, SiteProfile] = {
    "oddsshark": SiteProfile(
        key="oddsshark",
        display_name="OddsShark",
        target_site="oddsshark.com",
        bot_name="SPY-BOT OddsShark Scout",
        features=(
            FeatureBlueprint(
                name="Public Betting Percentages",
                betdoc_has=False,
                severity="HIGH",
                suggestion="Aggregate anonymised BetDoc ticket counts into a live public-money split per market.",
                priority=1,
            ),
            FeatureBlueprint(
                name="Historical ATS Trends Database",
                betdoc_has=False,
                severity="MEDIUM",
                suggestion="Backfill against-the-spread results and expose filterable trend queries.",
                priority=3,
            ),
            FeatureBlueprint(name="Odds Comparison Grid", betdoc_has=True),
        ),
    ),
    "actionnetwork": SiteProfile(
        key="actionnetwork",
        display_name="ActionNetwork",
        target_site="actionnetwork.com",
        bot_name="SPY-BOT ActionNetwork Scout",
        features=(
            FeatureBlueprint(
                name="Sharp Money Indicators",
                betdoc_has=False,
                severity="HIGH",
                suggestion="Flag reverse line movement and steam moves from the odds feed in real time.",
                priority=1,
            ),
            FeatureBlueprint(
                name="Sportsbook Bet Tracking Sync",
                betdoc_has=False,
                severity="MEDIUM",
                suggestion="Offer an import pipeline so users can sync settled tickets into their BetDoc ledger.",
                priority=2,
            ),
            FeatureBlueprint(name="Expert Picks Feed", betdoc_has=True),
        ),
    ),
    "pinnacle": SiteProfile(
        key="pinnacle",
        display_name="Pinnacle",
        target_site="pinnacle.com",
        bot_name="SPY-BOT Pinnacle Scout",
        features=(
            FeatureBlueprint(
                name="Closing Line Value Tracker",
                betdoc_has=False,
                severity="HIGH",
                suggestion="Store closing prices per market and report each user's CLV per settled bet.",
                priority=2,
            ),
            FeatureBlueprint(
                name="Betting Limits Transparency",
                betdoc_has=False,
                severity="LOW",
                suggestion="Publish max-stake guidance per market alongside each suggested parlay.",
                priority=5,
            ),
            FeatureBlueprint(name="Live In-Play Markets", betdoc_has=True),
        ),
    ),
}

SCAN_ORDER: tuple[str, ...] = ("oddsshark", "actionnetwork", "pinnacle")

_SEVERITY_RANK = case(
    {"HIGH": 0, "MEDIUM": 1, "LOW": 2},
    value=FeatureGapAlertModel.severity,
    else_=3,
)


class CompetitiveIntelManager:
    @property
    def supported_sites(self) -> tuple[str, ...]:
        return tuple(SITE_CATALOG[key].display_name for key in SCAN_ORDER)

    # ------------------------------------------------------------------ dashboard

    async def get_dashboard(self, db: AsyncSession) -> dict[str, list[Any]]:
        bots_result = await db.execute(
            select(CompetitorBotModel)
            .order_by(CompetitorBotModel.target_site)
            .execution_options(populate_existing=True)
        )
        bots = list(bots_result.scalars().all())

        gaps_result = await db.execute(
            select(FeatureGapAlertModel)
            .options(selectinload(FeatureGapAlertModel.suggestions))
            .where(FeatureGapAlertModel.is_resolved.is_(False))
            .order_by(_SEVERITY_RANK, FeatureGapAlertModel.site_name, FeatureGapAlertModel.missing_feature)
            .execution_options(populate_existing=True)
        )
        gaps = list(gaps_result.scalars().unique().all())

        logger.info("SPY-BOT: dashboard assembled with %d bots and %d open gaps.", len(bots), len(gaps))
        return {"bots": bots, "gaps": gaps}

    # ------------------------------------------------------------------ scan

    async def trigger_scan(self, db: AsyncSession) -> dict[str, int]:
        """Mock-scan every tracked competitor. Idempotent; retries once on a concurrent-insert collision."""
        for attempt in (1, 2):
            try:
                summary = await self._run_scan(db)
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                if attempt == 2:
                    logger.error("SPY-BOT: scan collided with a concurrent writer twice; aborting.")
                    raise CompetitiveIntelDomainError(
                        "SPY-BOT scan collided with a concurrent writer; retry later."
                    ) from exc
                logger.warning("SPY-BOT: scan collided with a concurrent writer; retrying once.")
                continue
            logger.info(
                "SPY-BOT: scan complete (bots +%d/~%d, gaps +%d/~%d, suggestions +%d).",
                summary["bots_created"],
                summary["bots_updated"],
                summary["gaps_created"],
                summary["gaps_refreshed"],
                summary["suggestions_created"],
            )
            return summary
        raise CompetitiveIntelDomainError("SPY-BOT scan did not complete.")

    async def _run_scan(self, db: AsyncSession) -> dict[str, int]:
        summary = {
            "bots_created": 0,
            "bots_updated": 0,
            "gaps_created": 0,
            "gaps_refreshed": 0,
            "suggestions_created": 0,
        }
        for site_key in SCAN_ORDER:
            profile = SITE_CATALOG[site_key]
            logger.info("SPY-BOT: infiltrating %s (%s).", profile.display_name, profile.target_site)

            bot_id = (
                await db.execute(
                    select(CompetitorBotModel.id).where(CompetitorBotModel.target_site == profile.target_site)
                )
            ).scalar_one_or_none()
            if bot_id is None:
                await db.execute(
                    insert(CompetitorBotModel).values(
                        id=uuid4(),
                        name=profile.bot_name,
                        target_site=profile.target_site,
                        status=BotStatus.IDLE.value,
                        last_scan_at=func.now(),
                    )
                )
                summary["bots_created"] += 1
            else:
                await db.execute(
                    update(CompetitorBotModel)
                    .where(CompetitorBotModel.id == bot_id)
                    .values(status=BotStatus.IDLE.value, last_scan_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                summary["bots_updated"] += 1

            for gap in profile.gaps:
                await self._upsert_gap(db, profile, gap, summary)
        return summary

    async def _upsert_gap(
        self,
        db: AsyncSession,
        profile: SiteProfile,
        gap: FeatureBlueprint,
        summary: dict[str, int],
    ) -> None:
        if gap.severity is None or gap.suggestion is None or gap.priority is None:
            raise CompetitiveIntelDomainError(f"Gap blueprint {gap.name!r} is incomplete.")

        gap_id: UUID | None = (
            await db.execute(
                select(FeatureGapAlertModel.id).where(
                    FeatureGapAlertModel.site_name == profile.display_name,
                    FeatureGapAlertModel.missing_feature == gap.name,
                )
            )
        ).scalar_one_or_none()

        if gap_id is None:
            gap_id = uuid4()
            await db.execute(
                insert(FeatureGapAlertModel).values(
                    id=gap_id,
                    site_name=profile.display_name,
                    missing_feature=gap.name,
                    severity=gap.severity,
                    is_resolved=False,
                )
            )
            summary["gaps_created"] += 1
            logger.info("SPY-BOT: BetDoc is missing %r from %s.", gap.name, profile.display_name)
        else:
            await db.execute(
                update(FeatureGapAlertModel)
                .where(FeatureGapAlertModel.id == gap_id)
                .values(severity=gap.severity)
                .execution_options(synchronize_session=False)
            )
            summary["gaps_refreshed"] += 1

        suggestion_count = (
            await db.execute(
                select(func.count()).select_from(DevSuggestionModel).where(DevSuggestionModel.gap_id == gap_id)
            )
        ).scalar_one()
        if suggestion_count == 0:
            await db.execute(
                insert(DevSuggestionModel).values(
                    id=uuid4(),
                    gap_id=gap_id,
                    suggestion_text=gap.suggestion,
                    priority=gap.priority,
                )
            )
            summary["suggestions_created"] += 1

    # ------------------------------------------------------------------ reports

    def resolve_site(self, site_name: str) -> SiteProfile:
        if not isinstance(site_name, str):
            raise CompetitiveIntelDomainError("site_name must be a string.")
        normalized = site_name.strip().lower()
        if not normalized:
            raise CompetitiveIntelDomainError("site_name must not be blank.")
        if len(normalized) > MAX_SITE_NAME_LENGTH:
            raise CompetitiveIntelDomainError(f"site_name must be at most {MAX_SITE_NAME_LENGTH} characters.")
        profile = SITE_CATALOG.get(normalized)
        if profile is None:
            raise SiteNotFoundError(site_name.strip(), self.supported_sites)
        return profile

    def generate_markdown_report(self, site_name: str, *, generated_at: datetime | None = None) -> str:
        profile = self.resolve_site(site_name)
        timestamp = (generated_at or datetime.now(UTC)).isoformat(timespec="seconds")

        lines: list[str] = [
            f"# Site Report: {profile.display_name}",
            "",
            f"- **Target:** {profile.target_site}",
            f"- **Scout:** {profile.bot_name}",
            f"- **Generated:** {timestamp}",
            "",
            "## Feature Comparison",
            "",
            f"| Feature | {profile.display_name} | BetDoc | Gap Severity |",
            "| --- | --- | --- | --- |",
        ]
        for feature in profile.features:
            betdoc = "Yes" if feature.betdoc_has else "No"
            lines.append(f"| {feature.name} | Yes | {betdoc} | {feature.severity or 'None'} |")

        lines.extend(["", "### Recommendation", ""])
        gaps = sorted(profile.gaps, key=lambda g: g.priority or 5)
        if gaps:
            lines.append(
                f"BetDoc trails {profile.display_name} on {len(gaps)} of {len(profile.features)} "
                "tracked features. Build in this order:"
            )
            lines.append("")
            for index, gap in enumerate(gaps, start=1):
                lines.append(f"{index}. **{gap.name}** ({gap.severity}, P{gap.priority}): {gap.suggestion}")
        else:
            lines.append(f"BetDoc is at feature parity with {profile.display_name}. No action required.")

        logger.info("SPY-BOT: generated comparison report for %s.", profile.display_name)
        return "\n".join(lines) + "\n"
