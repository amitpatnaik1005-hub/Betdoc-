"""CfoManager: TODAR MAL's parameter-driven advisory, stress testing, tax and alerts."""

import json
import logging
import math
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.domain.cfo.errors import AlertNotFoundError, CfoDomainError
from app.models.cfo import CfoAdvisoryModel, CfoAlertModel, StressTestResultModel, TaxRecordModel

logger = logging.getLogger("betdoc.the_vault.todarmal")

# Structural constants from the specified formula / column sizes, not business thresholds.
MAX_HEALTH_SCORE = 100.0
HEALTH_PENALTY_PER_EXPOSURE_POINT = 1.5
SUGGESTIONS_JSON_MAX_LENGTH = 1024
ALERT_MESSAGE_MAX_LENGTH = 255
MAX_ALERT_LIMIT = 200


def _require_finite(**values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CfoDomainError(f"{name} must be a finite number.")


def _require_pct(name: str, value: float) -> None:
    if not 0.0 <= value <= 100.0:
        raise CfoDomainError(f"{name} must be between 0 and 100.")


def _owner_filter(column: Any, user_id: UUID | None) -> Any:
    return column.is_(None) if user_id is None else column == user_id


class CfoManager:
    # ------------------------------------------------------------------ helpers

    async def _commit(self, db: AsyncSession, operation: str) -> None:
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            logger.warning("[TODAR MAL]: %s rejected by storage constraints.", operation)
            raise CfoDomainError(f"{operation} rejected by storage constraints.") from exc

    @staticmethod
    def _build_advisory_suggestions(
        *,
        current_bankroll: float,
        active_exposure: float,
        exposure_pct: float,
        health_score: float,
        variance_threshold_pct: float,
        variance_status: str,
    ) -> list[str]:
        threshold_exposure = current_bankroll * (variance_threshold_pct / 100.0)
        suggestions = [f"Capital health score is {health_score:.2f}/{MAX_HEALTH_SCORE:.0f}."]
        if variance_status == "HIGH":
            suggestions.append(
                f"Exposure is {exposure_pct:.2f}% of bankroll, {exposure_pct - variance_threshold_pct:.2f} points "
                f"above your {variance_threshold_pct:.2f}% variance threshold."
            )
            suggestions.append(
                f"Reduce open exposure by {active_exposure - threshold_exposure:.2f} to return to the threshold."
            )
        else:
            suggestions.append(
                f"Exposure is {exposure_pct:.2f}% of bankroll, within your {variance_threshold_pct:.2f}% "
                "variance threshold."
            )
            suggestions.append(
                f"Up to {threshold_exposure - active_exposure:.2f} of additional exposure remains "
                "before variance turns HIGH."
            )
        if active_exposure == 0:
            suggestions.append("No capital is currently deployed; the full bankroll is idle.")
        if health_score == 0:
            suggestions.append("Capital health is fully depleted at this exposure level.")
        return suggestions

    # ------------------------------------------------------------------ advisory

    async def generate_advisory_report(
        self,
        db: AsyncSession,
        user_id: UUID | None,
        current_bankroll: float,
        active_exposure: float,
        variance_threshold_pct: float,
    ) -> CfoAdvisoryModel:
        _require_finite(
            current_bankroll=current_bankroll,
            active_exposure=active_exposure,
            variance_threshold_pct=variance_threshold_pct,
        )
        if current_bankroll <= 0:
            raise CfoDomainError("current_bankroll must be greater than zero.")
        if active_exposure < 0:
            raise CfoDomainError("active_exposure must not be negative.")
        _require_pct("variance_threshold_pct", variance_threshold_pct)

        exposure_pct = (active_exposure / current_bankroll) * 100.0
        health_score = max(0.0, MAX_HEALTH_SCORE - (exposure_pct * HEALTH_PENALTY_PER_EXPOSURE_POINT))
        variance_status = "HIGH" if exposure_pct > variance_threshold_pct else "STABLE"
        suggestions = self._build_advisory_suggestions(
            current_bankroll=current_bankroll,
            active_exposure=active_exposure,
            exposure_pct=exposure_pct,
            health_score=health_score,
            variance_threshold_pct=variance_threshold_pct,
            variance_status=variance_status,
        )
        suggestions_json = json.dumps(suggestions)
        if len(suggestions_json) > SUGGESTIONS_JSON_MAX_LENGTH:
            raise CfoDomainError("Generated suggestions exceed the storage limit.")

        advisory = CfoAdvisoryModel(
            user_id=user_id,
            capital_health_score=health_score,
            variance_status=variance_status,
            suggestions_json=suggestions_json,
        )
        db.add(advisory)
        if variance_status == "HIGH":
            db.add(
                CfoAlertModel(
                    user_id=user_id,
                    level="WARNING",
                    message=(
                        f"Exposure at {exposure_pct:.2f}% exceeds your {variance_threshold_pct:.2f}% variance "
                        f"threshold (health score {health_score:.2f}/{MAX_HEALTH_SCORE:.0f})."
                    )[:ALERT_MESSAGE_MAX_LENGTH],
                )
            )
        await self._commit(db, "Advisory report")
        await db.refresh(advisory)

        logger.info(
            "[TODAR MAL]: advisory %s issued (health %.2f, exposure %.2f%%, threshold %.2f%%, status %s).",
            advisory.id,
            health_score,
            exposure_pct,
            variance_threshold_pct,
            variance_status,
        )
        return advisory

    # ------------------------------------------------------------------ stress test

    async def run_stress_test(
        self,
        db: AsyncSession,
        user_id: UUID | None,
        scenario: str,
        portfolio_value: float,
        shock_pct: float,
        survival_threshold_pct: float,
    ) -> StressTestResultModel:
        scenario_name = (scenario or "").strip()
        if not scenario_name:
            raise CfoDomainError("scenario must not be blank.")
        _require_finite(
            portfolio_value=portfolio_value, shock_pct=shock_pct, survival_threshold_pct=survival_threshold_pct
        )
        if portfolio_value <= 0:
            raise CfoDomainError("portfolio_value must be greater than zero.")
        _require_pct("shock_pct", shock_pct)
        _require_pct("survival_threshold_pct", survival_threshold_pct)

        simulated_pnl = -(portfolio_value * (shock_pct / 100.0)) or 0.0  # normalise -0.0
        survived = shock_pct < survival_threshold_pct
        recommendation = (
            "Buffer sufficient."
            if survived
            else f"Catastrophic risk detected. Shock exceeds threshold of {survival_threshold_pct}%."
        )

        result = StressTestResultModel(
            user_id=user_id,
            scenario_name=scenario_name,
            portfolio_value_before=portfolio_value,
            simulated_pnl=simulated_pnl,
            simulated_drawdown_pct=shock_pct,
            survived=survived,
            recommendation=recommendation[:ALERT_MESSAGE_MAX_LENGTH],
        )
        db.add(result)
        if not survived:
            db.add(
                CfoAlertModel(
                    user_id=user_id,
                    level="CRITICAL",
                    message=(
                        f"Stress test '{scenario_name}' failed: a {shock_pct}% shock breaches the "
                        f"{survival_threshold_pct}% survival threshold (simulated P&L {simulated_pnl:.2f})."
                    )[:ALERT_MESSAGE_MAX_LENGTH],
                )
            )
        await self._commit(db, "Stress test")
        await db.refresh(result)

        logger.info(
            "[TODAR MAL]: stress test '%s' on %.2f with %.2f%% shock -> P&L %.2f, survived=%s.",
            scenario_name,
            portfolio_value,
            shock_pct,
            simulated_pnl,
            survived,
        )
        return result

    # ------------------------------------------------------------------ taxes

    async def _find_tax_record(self, db: AsyncSession, user_id: UUID | None, year: int) -> TaxRecordModel | None:
        result = await db.execute(
            select(TaxRecordModel)
            .where(_owner_filter(TaxRecordModel.user_id, user_id), TaxRecordModel.year == year)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _apply_tax(record: TaxRecordModel, total_profit: float, taxable_amount: float, estimated_tax: float) -> None:
        record.total_profit = total_profit
        record.taxable_amount = taxable_amount
        record.estimated_tax = estimated_tax
        record.last_calculated_at = func.now()  # force a fresh timestamp even if the figures are unchanged

    async def calculate_tax(
        self,
        db: AsyncSession,
        user_id: UUID | None,
        year: int,
        total_profit: float,
        tax_allowance: float,
        tax_rate_pct: float,
    ) -> TaxRecordModel:
        if isinstance(year, bool) or not isinstance(year, int):
            raise CfoDomainError("year must be an integer.")
        _require_finite(total_profit=total_profit, tax_allowance=tax_allowance, tax_rate_pct=tax_rate_pct)
        if tax_allowance < 0:
            raise CfoDomainError("tax_allowance must not be negative.")
        _require_pct("tax_rate_pct", tax_rate_pct)

        taxable_amount = max(0.0, total_profit - tax_allowance)
        estimated_tax = taxable_amount * (tax_rate_pct / 100.0)

        record = await self._find_tax_record(db, user_id, year)
        if record is not None:
            self._apply_tax(record, total_profit, taxable_amount, estimated_tax)
            await self._commit(db, "Tax record update")
        else:
            record = TaxRecordModel(
                user_id=user_id,
                year=year,
                total_profit=total_profit,
                taxable_amount=taxable_amount,
                estimated_tax=estimated_tax,
            )
            db.add(record)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.warning(
                    "[TODAR MAL]: concurrent tax record creation for %s/%d; updating the existing row.",
                    user_id or "anonymous",
                    year,
                )
                record = await self._find_tax_record(db, user_id, year)
                if record is None:
                    raise CfoDomainError("Tax record could not be created or loaded.") from None
                self._apply_tax(record, total_profit, taxable_amount, estimated_tax)
                await self._commit(db, "Tax record update")
        await db.refresh(record)

        logger.info(
            "[TODAR MAL]: tax for %s/%d -> taxable %.2f at %.2f%% = %.2f.",
            user_id or "anonymous",
            year,
            taxable_amount,
            tax_rate_pct,
            estimated_tax,
        )
        return record

    # ------------------------------------------------------------------ alerts

    async def get_unread_alerts(
        self, db: AsyncSession, user_id: UUID | None, limit: int = 50
    ) -> list[CfoAlertModel]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ALERT_LIMIT:
            raise CfoDomainError(f"limit must be an integer between 1 and {MAX_ALERT_LIMIT}.")
        result = await db.execute(
            select(CfoAlertModel)
            .where(_owner_filter(CfoAlertModel.user_id, user_id), CfoAlertModel.is_read.is_(False))
            .order_by(CfoAlertModel.created_at.desc(), CfoAlertModel.id.desc())
            .limit(limit)
        )
        alerts = list(result.scalars().all())
        logger.info("[TODAR MAL]: %d unread alerts for %s.", len(alerts), user_id or "anonymous")
        return alerts

    async def mark_alert_read(self, db: AsyncSession, alert_id: UUID, user_id: UUID | None) -> CfoAlertModel:
        alert = (
            await db.execute(
                select(CfoAlertModel).where(
                    CfoAlertModel.id == alert_id, _owner_filter(CfoAlertModel.user_id, user_id)
                )
            )
        ).scalar_one_or_none()
        if alert is None:
            raise AlertNotFoundError(alert_id)
        alert.is_read = True
        await self._commit(db, "Alert acknowledgement")
        await db.refresh(alert)
        logger.info("[TODAR MAL]: alert %s acknowledged.", alert_id)
        return alert
