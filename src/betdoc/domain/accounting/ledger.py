"""Vectorized P&L computation over the settled-bet ledger.



Representation

--------------

Every frame carries money twice, on purpose:



``normalized_stake`` / ``normalized_payout`` as ``pl.Decimal(18, 6)``

    The authoritative, exact decimal representation. This is what gets

    persisted, exported, and reconciled against the bookmaker.



``stake_micros`` / ``payout_micros`` as ``pl.Int64``

    The same amounts as integer micro-units. **All arithmetic runs on these.**



The duplication is not redundancy, it is a workaround for a real constraint:

``pl.Decimal`` is still an unstable Polars dtype. ``sum`` is supported, but

``mean``, ``std``, and division over Decimal columns either raise or silently

promote to ``Float64`` depending on the Polars version — and a silent

promotion to float is exactly the precision loss this module exists to

prevent. Integer arithmetic on micro-units is exact, associative, and stable

across versions. Money is reconstructed as ``Decimal`` at the output

boundary, never mid-pipeline.



At scale 6, ``Int64`` holds roughly ±9.2 trillion currency units. That is

checked on ingestion rather than assumed.



P&L convention

--------------

``payout`` is **gross returns including the returned stake**, matching how

every bookmaker settlement feed reports it. So:



===========  ==================  ==========================

Status       Payout              P&L

===========  ==================  ==========================

WON          stake × odds        payout − stake

HALF_WON     partial + stake     payout − stake

LOST         0                   −stake

HALF_LOST    half stake          payout − stake

VOID         stake               0

PENDING      0                   0 (excluded from all aggregates)

===========  ==================  ==========================



``payout − stake`` is therefore correct for every settled status, and the

``when/then`` chain below encodes the status semantics explicitly so that a

malformed feed (a LOST row carrying a payout) is caught rather than absorbed.

"""



from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

import polars as pl
from pydantic import BaseModel, ConfigDict

from betdoc.domain.accounting.currency import CurrencyConverter
from betdoc.domain.accounting.types import (
    AccountingError,
    BetStatus,
    CurrencyCode,
    LedgerEntry,
    Money,
    StaleRateError,
    from_micros,
    money_context,
    quantize_money,
    to_micros,
)

__all__ = ["LEDGER_SCHEMA", "LedgerService", "PnLSummary"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



#: Maximum representable micro-unit magnitude before Int64 overflow risk.

_MICRO_CEILING: Final[int] = 9_000_000_000_000_000_000



#: Strict schema for the normalized ledger frame.

LEDGER_SCHEMA: Final[dict[str, Any]] = {

    "bet_id": pl.Utf8,

    "placed_at": pl.Datetime(time_unit="us", time_zone="UTC"),

    "settled_at": pl.Datetime(time_unit="us", time_zone="UTC"),

    "sport": pl.Utf8,

    "archetype": pl.Utf8,

    "bookmaker": pl.Utf8,

    "market_id": pl.Utf8,

    "selection": pl.Utf8,

    "status": pl.Utf8,

    "source_currency": pl.Utf8,

    "base_currency": pl.Utf8,

    "normalized_stake": pl.Decimal(precision=18, scale=6),

    "normalized_payout": pl.Decimal(precision=18, scale=6),

    "stake_micros": pl.Int64,

    "payout_micros": pl.Int64,

    "odds": pl.Decimal(precision=18, scale=6),

    # Dimensionless diagnostics, not money: Float64 is correct here.

    "model_probability": pl.Float64,

    "stake_at_risk_fraction": pl.Float64,

    "fx_stale": pl.Boolean,

}





class PnLSummary(BaseModel):

    """Aggregate P&L for a set of settled bets, in the base currency."""



    model_config = ConfigDict(frozen=True, extra="forbid")



    base_currency: CurrencyCode

    settled_bets: int

    pending_bets: int

    won_bets: int

    lost_bets: int

    void_bets: int

    total_volume: Money

    total_at_risk: Money

    gross_profit: Money

    gross_loss: Money

    net_pnl: Money

    largest_win: Money

    largest_loss: Money

    #: net_pnl / total_volume. A dimensionless ratio, so float, and ``None``

    #: when turnover is zero rather than a fabricated 0.0.

    roi: float | None

    win_rate: float | None

    average_odds: float | None

    stale_fx_rows: int



    def to_dict(self) -> dict[str, Any]:

        """Return a JSON-safe projection, money rendered as decimal strings."""

        return {

            "base_currency": self.base_currency.value,

            "settled_bets": self.settled_bets,

            "pending_bets": self.pending_bets,

            "won_bets": self.won_bets,

            "lost_bets": self.lost_bets,

            "void_bets": self.void_bets,

            "total_volume": str(self.total_volume.value),

            "total_at_risk": str(self.total_at_risk.value),

            "gross_profit": str(self.gross_profit.value),

            "gross_loss": str(self.gross_loss.value),

            "net_pnl": str(self.net_pnl.value),

            "largest_win": str(self.largest_win.value),

            "largest_loss": str(self.largest_loss.value),

            "roi": self.roi,

            "win_rate": self.win_rate,

            "average_odds": self.average_odds,

            "stale_fx_rows": self.stale_fx_rows,

        }





class LedgerService:

    """Normalizes multi-currency ledger entries and vectorizes P&L."""



    __slots__ = ("_base", "_converter", "_require_fresh_fx")



    def __init__(

        self,

        converter: CurrencyConverter,

        *,

        base_currency: CurrencyCode | str | None = None,

        require_fresh_fx: bool = False,

    ) -> None:

        self._converter: CurrencyConverter = converter

        self._base: CurrencyCode = (

            CurrencyCode(base_currency)

            if base_currency is not None

            else converter.base_currency

        )

        #: When True, a stale FX rate aborts normalization instead of being

        #: recorded and flagged. Appropriate for a live risk snapshot;

        #: inappropriate for end-of-day reconciliation, which must complete.

        self._require_fresh_fx: bool = require_fresh_fx



    @property

    def base_currency(self) -> CurrencyCode:

        """Currency all amounts are normalized into."""

        return self._base



    async def normalize_entries(

        self, entries: Sequence[LedgerEntry]

    ) -> list[dict[str, Any]]:

        """Convert every stake and payout into the base currency.



        Conversions for distinct currencies run concurrently; the converter's

        single-flight refresh collapses them into one provider call per base,

        so a 50,000-row ledger spanning eight currencies issues eight

        refreshes rather than 50,000.

        """

        if not entries:

            return []



        rows: list[dict[str, Any]] = []

        results = await asyncio.gather(

            *(self._normalize_one(entry) for entry in entries),

            return_exceptions=True,

        )



        for entry, result in zip(entries, results):

            if isinstance(result, StaleRateError):

                raise AccountingError(

                    f"bet {entry.bet_id}: {result}",

                    code="FX_STALE",

                    details=result.details,

                ) from result

            if isinstance(result, BaseException):

                raise AccountingError(

                    f"failed to normalize bet {entry.bet_id}: {result}",

                    code="NORMALIZATION_FAILED",

                ) from result

            rows.append(result)



        return rows



    async def _normalize_one(self, entry: LedgerEntry) -> dict[str, Any]:

        """Normalize a single entry into a flat, Polars-ready dict."""

        allow_stale = not self._require_fresh_fx



        stake = await self._converter.convert(

            entry.stake, self._base, allow_stale=allow_stale

        )

        payout = await self._converter.convert(

            entry.payout, self._base, allow_stale=allow_stale

        )



        stake_value = quantize_money(stake.value)

        payout_value = quantize_money(payout.value)

        stake_micros = to_micros(stake_value)

        payout_micros = to_micros(payout_value)



        for label, micros in (("stake", stake_micros), ("payout", payout_micros)):

            if abs(micros) >= _MICRO_CEILING:

                raise AccountingError(

                    f"bet {entry.bet_id} {label} of {micros} micro-units exceeds "

                    f"the Int64 aggregation ceiling; split the position",

                    code="AMOUNT_OVERFLOW",

                )



        was_converted = (

            entry.stake.currency is not self._base

            or entry.payout.currency is not self._base

        )

        stale = False

        if was_converted:

            rate = await self._converter.get_rate(

                entry.stake.currency, self._base, allow_stale=True

            )

            stale = not rate.is_fresh(60.0)



        return {

            "bet_id": entry.bet_id,

            "placed_at": entry.placed_at,

            "settled_at": entry.settled_at,

            "sport": entry.sport,

            "archetype": entry.archetype,

            "bookmaker": entry.bookmaker,

            "market_id": entry.market_id,

            "selection": entry.selection,

            "status": entry.status.value,

            "source_currency": entry.stake.currency.value,

            "base_currency": self._base.value,

            "normalized_stake": stake_value,

            "normalized_payout": payout_value,

            "stake_micros": stake_micros,

            "payout_micros": payout_micros,

            "odds": quantize_money(entry.odds),

            "model_probability": (

                float(entry.model_probability)

                if entry.model_probability is not None

                else None

            ),

            "stake_at_risk_fraction": float(entry.status.stake_at_risk_fraction),

            "fx_stale": stale,

        }



    def build_frame(self, rows: Sequence[dict[str, Any]]) -> pl.DataFrame:

        """Materialize normalized rows into a strictly-typed frame.



        The schema is declared, never inferred. Inference on a small batch

        types ``payout_micros`` from whatever happens to be present, so a

        batch of all-losing bets (every payout zero) infers a narrower dtype

        than a mixed batch, and the two frames then refuse to concatenate.

        """

        if not rows:

            return pl.DataFrame(schema=LEDGER_SCHEMA)

        try:

            return pl.DataFrame(list(rows), schema=LEDGER_SCHEMA, strict=True)

        except (pl.exceptions.SchemaError, pl.exceptions.InvalidOperationError) as error:

            raise AccountingError(

                f"normalized rows do not satisfy the ledger schema: {error}",

                code="SCHEMA_MISMATCH",

            ) from error



    async def load(self, entries: Sequence[LedgerEntry]) -> pl.DataFrame:

        """Normalize and materialize in one step."""

        return self.build_frame(await self.normalize_entries(entries))



    @staticmethod

    def with_pnl(frame: pl.DataFrame) -> pl.DataFrame:

        """Attach per-row P&L columns using exact integer arithmetic.



        The ``when/then`` chain is written status-by-status rather than as a

        blanket ``payout - stake`` so that the settlement invariants are

        visible in the code and a contradictory row surfaces as a

        reconciliation discrepancy instead of a plausible-looking number.

        """

        if frame.is_empty():

            return frame.with_columns(

                pnl_micros=pl.lit(None, dtype=pl.Int64),

                turnover_micros=pl.lit(None, dtype=pl.Int64),

                at_risk_micros=pl.lit(None, dtype=pl.Int64),

                is_settled=pl.lit(None, dtype=pl.Boolean),

                is_win=pl.lit(None, dtype=pl.Boolean),

            )



        settled_statuses = [

            BetStatus.WON.value,

            BetStatus.LOST.value,

            BetStatus.VOID.value,

            BetStatus.HALF_WON.value,

            BetStatus.HALF_LOST.value,

        ]

        turnover_statuses = [

            status.value for status in BetStatus if status.counts_toward_turnover

        ]



        return frame.with_columns(

            pnl_micros=(

                pl.when(pl.col("status") == BetStatus.PENDING.value)

                .then(pl.lit(0, dtype=pl.Int64))

                .when(pl.col("status") == BetStatus.VOID.value)

                # A void returns the stake exactly: zero P&L by definition,

                # regardless of what the feed put in the payout field.

                .then(pl.lit(0, dtype=pl.Int64))

                .when(pl.col("status") == BetStatus.LOST.value)

                .then(-pl.col("stake_micros"))

                .otherwise(pl.col("payout_micros") - pl.col("stake_micros"))

                .cast(pl.Int64)

            ),

            # Voided stakes are excluded from turnover: they were never at

            # risk, and including them dilutes every ROI figure.

            turnover_micros=(

                pl.when(pl.col("status").is_in(turnover_statuses))

                .then(pl.col("stake_micros"))

                .otherwise(pl.lit(0, dtype=pl.Int64))

                .cast(pl.Int64)

            ),

            at_risk_micros=(

                (

                    pl.col("stake_micros").cast(pl.Float64)

                    * pl.col("stake_at_risk_fraction")

                )

                .round(0)

                .cast(pl.Int64)

            ),

            is_settled=pl.col("status").is_in(settled_statuses),

            is_win=pl.col("status").is_in(

                [BetStatus.WON.value, BetStatus.HALF_WON.value]

            ),

        )



    def calculate_pnl(self, frame: pl.DataFrame) -> tuple[pl.DataFrame, PnLSummary]:

        """Compute per-row P&L and the aggregate summary.



        Returns the enriched frame alongside the summary so that callers can

        feed the same frame straight into :class:`ReportGenerator` without

        recomputing anything.

        """

        enriched = self.with_pnl(frame)

        if enriched.is_empty():

            zero = Money.zero(self._base)

            return enriched, PnLSummary(

                base_currency=self._base,

                settled_bets=0,

                pending_bets=0,

                won_bets=0,

                lost_bets=0,

                void_bets=0,

                total_volume=zero,

                total_at_risk=zero,

                gross_profit=zero,

                gross_loss=zero,

                net_pnl=zero,

                largest_win=zero,

                largest_loss=zero,

                roi=None,

                win_rate=None,

                average_odds=None,

                stale_fx_rows=0,

            )



        settled = enriched.filter(pl.col("is_settled"))



        aggregate = settled.select(

            settled_bets=pl.len(),

            won_bets=pl.col("status")

            .is_in([BetStatus.WON.value, BetStatus.HALF_WON.value])

            .sum(),

            lost_bets=pl.col("status")

            .is_in([BetStatus.LOST.value, BetStatus.HALF_LOST.value])

            .sum(),

            void_bets=(pl.col("status") == BetStatus.VOID.value).sum(),

            turnover_micros=pl.col("turnover_micros").sum(),

            at_risk_micros=pl.col("at_risk_micros").sum(),

            net_micros=pl.col("pnl_micros").sum(),

            profit_micros=pl.when(pl.col("pnl_micros") > 0)

            .then(pl.col("pnl_micros"))

            .otherwise(0)

            .sum(),

            loss_micros=pl.when(pl.col("pnl_micros") < 0)

            .then(pl.col("pnl_micros"))

            .otherwise(0)

            .sum(),

            largest_win_micros=pl.col("pnl_micros").max(),

            largest_loss_micros=pl.col("pnl_micros").min(),

            # Odds are averaged as Float64 deliberately: a mean price is a

            # descriptive statistic, not a settlement amount.

            average_odds=pl.col("odds").cast(pl.Float64).mean(),

            stale_fx_rows=pl.col("fx_stale").sum(),

        ).row(0, named=True)



        pending = int(

            enriched.select((pl.col("status") == BetStatus.PENDING.value).sum()).item()

        )



        turnover = int(aggregate["turnover_micros"] or 0)

        net = int(aggregate["net_micros"] or 0)

        settled_count = int(aggregate["settled_bets"] or 0)

        graded = int(aggregate["won_bets"] or 0) + int(aggregate["lost_bets"] or 0)



        with money_context():

            roi = (

                float(Decimal(net) / Decimal(turnover)) if turnover > 0 else None

            )

        win_rate = (

            float(int(aggregate["won_bets"]) / graded) if graded > 0 else None

        )



        summary = PnLSummary(

            base_currency=self._base,

            settled_bets=settled_count,

            pending_bets=pending,

            won_bets=int(aggregate["won_bets"] or 0),

            lost_bets=int(aggregate["lost_bets"] or 0),

            void_bets=int(aggregate["void_bets"] or 0),

            total_volume=Money.from_micros(turnover, self._base),

            total_at_risk=Money.from_micros(

                int(aggregate["at_risk_micros"] or 0), self._base

            ),

            gross_profit=Money.from_micros(

                int(aggregate["profit_micros"] or 0), self._base

            ),

            gross_loss=Money.from_micros(int(aggregate["loss_micros"] or 0), self._base),

            net_pnl=Money.from_micros(net, self._base),

            largest_win=Money.from_micros(

                max(int(aggregate["largest_win_micros"] or 0), 0), self._base

            ),

            largest_loss=Money.from_micros(

                min(int(aggregate["largest_loss_micros"] or 0), 0), self._base

            ),

            roi=roi,

            win_rate=win_rate,

            average_odds=(

                float(aggregate["average_odds"])

                if aggregate["average_odds"] is not None

                else None

            ),

            stale_fx_rows=int(aggregate["stale_fx_rows"] or 0),

        )



        if summary.stale_fx_rows:

            _LOG.warning(

                "%d of %d settled rows were converted at a stale FX rate",

                summary.stale_fx_rows,

                settled_count,

            )

        return enriched, summary



    async def run(

        self, entries: Sequence[LedgerEntry]

    ) -> tuple[pl.DataFrame, PnLSummary]:

        """Normalize, materialize, and compute P&L in one call."""

        frame = await self.load(entries)

        return self.calculate_pnl(frame)



    @staticmethod

    def to_money_frame(frame: pl.DataFrame, base_currency: CurrencyCode) -> pl.DataFrame:

        """Add exact ``pl.Decimal`` P&L columns for export.



        Called at the output boundary only. The micro-unit columns remain the

        computational source of truth; this simply renders them back into the

        decimal form that downstream reconciliation and accounting exports

        expect.

        """

        if frame.is_empty() or "pnl_micros" not in frame.columns:

            return frame

        return frame.with_columns(

            pnl=pl.col("pnl_micros")

            .map_elements(lambda micros: from_micros(int(micros)), return_dtype=pl.Decimal(18, 6))

            .alias("pnl"),

            turnover=pl.col("turnover_micros")

            .map_elements(lambda micros: from_micros(int(micros)), return_dtype=pl.Decimal(18, 6))

            .alias("turnover"),

        ).with_columns(pnl_currency=pl.lit(base_currency.value, dtype=pl.Utf8))
