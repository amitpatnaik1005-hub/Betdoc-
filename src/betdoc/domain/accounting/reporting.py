"""Analytical reporting over the enriched ledger frame.



The central question this answers: *which Bayesian archetype is actually

producing edge?* Aggregate ROI hides that. A book can be flat overall while

the Poisson football model prints and the Plackett-Luce racing model bleeds

an equal amount, and the only way to see it is to slice by

``(sport, archetype)``.



On the Sharpe proxy

-------------------

It is named a proxy because that is what it is: mean daily return divided by

the standard deviation of daily returns, where daily return is that day's

P&L over that day's turnover. Specifically it is **not** annualized and

assumes **no risk-free rate**, because neither was specified and inventing a

scaling factor would make the number look comparable to published Sharpe

ratios when it is not. With fewer than two distinct settlement days, or with

zero variance, it is ``None`` rather than zero: an undefined ratio reported

as 0.0 reads as "no risk-adjusted edge" when the truth is "not enough data".

"""



from __future__ import annotations

import logging
from typing import Any, Final

import polars as pl
from pydantic import BaseModel, ConfigDict

from betdoc.domain.accounting.types import (
    AccountingError,
    CurrencyCode,
    Money,
    from_micros,
)

__all__ = ["PerformanceRow", "ReportGenerator"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



#: Minimum distinct settlement days required for a variance estimate.

MIN_DAYS_FOR_VARIANCE: Final[int] = 2





class PerformanceRow(BaseModel):

    """One row of the performance matrix, typed and currency-aware."""



    model_config = ConfigDict(frozen=True, extra="forbid")



    sport: str

    archetype: str

    total_bets: int

    won_bets: int

    win_rate: float | None

    total_volume: Money

    net_profit: Money

    roi: float | None

    sharpe_ratio_proxy: float | None

    trading_days: int

    average_odds: float | None



    def to_dict(self) -> dict[str, Any]:

        """Return a JSON-safe projection with money as decimal strings."""

        return {

            "sport": self.sport,

            "archetype": self.archetype,

            "total_bets": self.total_bets,

            "won_bets": self.won_bets,

            "win_rate": self.win_rate,

            "total_volume": str(self.total_volume.value),

            "net_profit": str(self.net_profit.value),

            "currency": self.total_volume.currency.value,

            "roi": self.roi,

            "sharpe_ratio_proxy": self.sharpe_ratio_proxy,

            "trading_days": self.trading_days,

            "average_odds": self.average_odds,

        }





class ReportGenerator:

    """Builds performance matrices from an enriched ledger frame."""



    __slots__ = ("_base",)



    #: Columns that :meth:`LedgerService.with_pnl` must have added.

    REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(

        {

            "sport",

            "archetype",

            "settled_at",

            "status",

            "stake_micros",

            "pnl_micros",

            "turnover_micros",

            "is_settled",

            "is_win",

            "odds",

        }

    )



    def __init__(self, base_currency: CurrencyCode | str) -> None:

        self._base: CurrencyCode = CurrencyCode(base_currency)



    @property

    def base_currency(self) -> CurrencyCode:

        """Currency every monetary figure in the report is denominated in."""

        return self._base



    def _validate(self, frame: pl.DataFrame) -> None:

        missing = sorted(self.REQUIRED_COLUMNS - set(frame.columns))

        if missing:

            raise AccountingError(

                f"frame is missing column(s) {', '.join(missing)}; "

                f"pass the output of LedgerService.calculate_pnl, not the raw frame",

                code="SCHEMA_MISMATCH",

            )



    def generate_performance_matrix(self, frame: pl.DataFrame) -> pl.DataFrame:

        """Aggregate performance by ``(sport, archetype)``.



        Returns one row per model/sport pair with bet counts, strike rate,

        turnover, net profit, ROI, the Sharpe proxy, and the number of

        distinct settlement days behind the variance estimate. That day count

        is included because a Sharpe computed over three days is noise, and a

        reader needs to see the sample size next to the number.

        """

        self._validate(frame)



        settled = frame.filter(pl.col("is_settled"))

        if settled.is_empty():

            return pl.DataFrame(

                schema={

                    "sport": pl.Utf8,

                    "archetype": pl.Utf8,

                    "total_bets": pl.UInt32,

                    "won_bets": pl.UInt32,

                    "win_rate": pl.Float64,

                    "total_volume_micros": pl.Int64,

                    "net_profit_micros": pl.Int64,

                    "roi": pl.Float64,

                    "sharpe_ratio_proxy": pl.Float64,

                    "trading_days": pl.UInt32,

                    "average_odds": pl.Float64,

                }

            )



        base = (

            settled.group_by(["sport", "archetype"])

            .agg(

                total_bets=pl.len(),

                won_bets=pl.col("is_win").sum(),

                graded_bets=(pl.col("turnover_micros") > 0).sum(),
                total_volume_micros=pl.col("turnover_micros").sum(),
                net_profit_micros=pl.col("pnl_micros").sum(),
                average_odds=pl.col("odds").cast(pl.Float64).mean(),
            )
            .with_columns(
                # Integer-exact sums, then a single float division for the
                # dimensionless ratios. Division is the only place floats
                # enter, and never on a persisted monetary value.
                win_rate=pl.when(pl.col("graded_bets") > 0)
                .then(
                    pl.col("won_bets").cast(pl.Float64)
                    / pl.col("graded_bets").cast(pl.Float64)
                )

                .otherwise(None),

                roi=pl.when(pl.col("total_volume_micros") > 0)

                .then(

                    pl.col("net_profit_micros").cast(pl.Float64)

                    / pl.col("total_volume_micros").cast(pl.Float64)

                )

                .otherwise(None),

            )

            .drop("graded_bets")

        )



        variance = self._daily_variance(settled)

        return (

            base.join(variance, on=["sport", "archetype"], how="left")

            .with_columns(

                trading_days=pl.col("trading_days").fill_null(0).cast(pl.UInt32)

            )

            .sort("net_profit_micros", descending=True)

        )



    @staticmethod

    def _daily_variance(settled: pl.DataFrame) -> pl.DataFrame:

        """Compute the Sharpe proxy per ``(sport, archetype)``.



        Daily return is that day's P&L divided by that day's turnover, which

        normalizes for stake size: a day staking 10x as much should not

        dominate the mean simply for being larger. Days with zero turnover

        are dropped rather than treated as a 0% return day.

        """

        daily = (

            settled.with_columns(day=pl.col("settled_at").dt.date())

            .group_by(["sport", "archetype", "day"])

            .agg(

                day_pnl=pl.col("pnl_micros").sum(),

                day_turnover=pl.col("turnover_micros").sum(),

            )

            .filter(pl.col("day_turnover") > 0)

            .with_columns(

                daily_return=pl.col("day_pnl").cast(pl.Float64)

                / pl.col("day_turnover").cast(pl.Float64)

            )

        )



        if daily.is_empty():

            return pl.DataFrame(

                schema={

                    "sport": pl.Utf8,

                    "archetype": pl.Utf8,

                    "sharpe_ratio_proxy": pl.Float64,

                    "trading_days": pl.UInt32,

                }

            )



        return (

            daily.group_by(["sport", "archetype"])

            .agg(

                mean_daily_return=pl.col("daily_return").mean(),

                # ddof=1: this is a sample of trading days, not the

                # population. ddof=0 understates dispersion, which inflates

                # every Sharpe figure.

                std_daily_return=pl.col("daily_return").std(ddof=1),

                trading_days=pl.len(),

            )

            .with_columns(

                sharpe_ratio_proxy=pl.when(

                    (pl.col("trading_days") >= MIN_DAYS_FOR_VARIANCE)

                    & pl.col("std_daily_return").is_not_null()

                    & (pl.col("std_daily_return") > 0.0)

                )

                .then(pl.col("mean_daily_return") / pl.col("std_daily_return"))

                .otherwise(None)

            )

            .select(["sport", "archetype", "sharpe_ratio_proxy", "trading_days"])

        )



    def to_rows(self, matrix: pl.DataFrame) -> list[PerformanceRow]:

        """Convert the matrix into typed, currency-aware rows."""

        rows: list[PerformanceRow] = []

        for record in matrix.iter_rows(named=True):

            rows.append(

                PerformanceRow(

                    sport=record["sport"],

                    archetype=record["archetype"],

                    total_bets=int(record["total_bets"]),

                    won_bets=int(record["won_bets"] or 0),

                    win_rate=record.get("win_rate"),

                    total_volume=Money.from_micros(

                        int(record["total_volume_micros"] or 0), self._base

                    ),

                    net_profit=Money.from_micros(

                        int(record["net_profit_micros"] or 0), self._base

                    ),

                    roi=record.get("roi"),

                    sharpe_ratio_proxy=record.get("sharpe_ratio_proxy"),

                    trading_days=int(record.get("trading_days") or 0),

                    average_odds=record.get("average_odds"),

                )

            )

        return rows



    def generate_bookmaker_matrix(self, frame: pl.DataFrame) -> pl.DataFrame:

        """Aggregate performance by bookmaker.



        Separates model edge from execution quality: a strong archetype

        executed against a bookmaker with poor fills or aggressive limiting

        will show materially worse realized ROI than the same model

        elsewhere.

        """

        self._validate(frame)

        if "bookmaker" not in frame.columns:

            raise AccountingError(

                "frame has no 'bookmaker' column", code="SCHEMA_MISMATCH"

            )



        settled = frame.filter(pl.col("is_settled"))

        if settled.is_empty():

            return pl.DataFrame(

                schema={

                    "bookmaker": pl.Utf8,

                    "total_bets": pl.UInt32,

                    "total_volume_micros": pl.Int64,

                    "net_profit_micros": pl.Int64,

                    "roi": pl.Float64,

                }

            )



        return (

            settled.group_by("bookmaker")

            .agg(

                total_bets=pl.len(),

                total_volume_micros=pl.col("turnover_micros").sum(),

                net_profit_micros=pl.col("pnl_micros").sum(),

            )

            .with_columns(

                roi=pl.when(pl.col("total_volume_micros") > 0)

                .then(

                    pl.col("net_profit_micros").cast(pl.Float64)

                    / pl.col("total_volume_micros").cast(pl.Float64)

                )

                .otherwise(None)

            )

            .sort("net_profit_micros", descending=True)

        )



    def generate_equity_curve(self, frame: pl.DataFrame) -> pl.DataFrame:

        """Return the cumulative P&L curve with running peak and drawdown.



        Drawdown is computed on the cumulative series in micro-units so the

        peak-to-trough figure is exact, then surfaced as a Decimal.

        """

        self._validate(frame)

        settled = frame.filter(pl.col("is_settled"))

        if settled.is_empty():

            return pl.DataFrame(

                schema={

                    "day": pl.Date,

                    "day_pnl_micros": pl.Int64,

                    "cumulative_micros": pl.Int64,

                    "peak_micros": pl.Int64,

                    "drawdown_micros": pl.Int64,

                }

            )



        return (

            settled.with_columns(day=pl.col("settled_at").dt.date())

            .group_by("day")

            .agg(day_pnl_micros=pl.col("pnl_micros").sum())

            .sort("day")

            .with_columns(cumulative_micros=pl.col("day_pnl_micros").cum_sum())

            .with_columns(peak_micros=pl.col("cumulative_micros").cum_max())

            .with_columns(

                drawdown_micros=pl.col("cumulative_micros") - pl.col("peak_micros")

            )

        )



    def max_drawdown(self, frame: pl.DataFrame) -> Money:

        """Return the worst peak-to-trough decline as a negative Money."""

        curve = self.generate_equity_curve(frame)

        if curve.is_empty():

            return Money.zero(self._base)

        worst = curve.select(pl.col("drawdown_micros").min()).item()

        return Money.from_micros(min(int(worst or 0), 0), self._base)



    def summarize(self, frame: pl.DataFrame) -> dict[str, Any]:

        """Return a compact dashboard payload for the reporting API."""

        matrix = self.generate_performance_matrix(frame)

        rows = self.to_rows(matrix)

        drawdown = self.max_drawdown(frame)



        best = rows[0] if rows else None

        worst = rows[-1] if rows else None

        return {

            "base_currency": self._base.value,

            "segments": [row.to_dict() for row in rows],

            "best_segment": best.to_dict() if best else None,

            "worst_segment": worst.to_dict() if worst else None,

            "max_drawdown": str(drawdown.value),

            "total_net_profit": str(

                from_micros(

                    int(matrix.select(pl.col("net_profit_micros").sum()).item() or 0)

                )

            ),

        }
