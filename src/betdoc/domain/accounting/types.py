"""Value objects, enums, and errors for the accounting domain.



Precision policy

----------------

All money is :class:`decimal.Decimal` quantized to six decimal places with

``ROUND_HALF_EVEN`` (banker's rounding). Half-even is used rather than

half-up because half-up is biased: it rounds away from zero on every tie, so

across millions of settlements the error accumulates in one direction instead

of cancelling. On a book doing high volume at thin margins that bias is

measurable.



Six decimal places, not two, because intermediate FX conversions and partial

settlements compound: rounding to minor units at every step loses more than

it saves. Money is quantized to its currency's actual minor units only at the

point of payment or display, via :meth:`Money.to_minor_units`.



The global decimal context is deliberately never mutated. Setting

``decimal.getcontext().prec`` at import time changes arithmetic for every

library in the process, including NumPy interop and anything a dependency

does with Decimal. All arithmetic here runs inside :func:`money_context`.

"""



from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [

    "DECIMAL_PRECISION",
    "MONEY_QUANTUM",
    "MONEY_SCALE",
    "AccountingError",
    "BetStatus",
    "CurrencyCode",
    "CurrencyMismatchError",
    "ExchangeRate",
    "FXRateUnavailableError",
    "LedgerEntry",
    "Money",
    "StaleRateError",
    "from_micros",
    "money_context",
    "quantize_money",
    "to_micros",

]



#: Working precision for all intermediate money arithmetic.

DECIMAL_PRECISION: Final[int] = 28



#: Storage scale for money: six decimal places.

MONEY_SCALE: Final[int] = 6



#: Quantum implied by :data:`MONEY_SCALE`.

MONEY_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-MONEY_SCALE)



#: Scaling factor between a Decimal amount and its integer micro-units.

MICRO_FACTOR: Final[Decimal] = Decimal(10) ** MONEY_SCALE



_MONEY_CONTEXT: Final[Context] = Context(

    prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN

)





@contextlib.contextmanager

def money_context() -> Iterator[Context]:

    """Run a block under the accounting decimal context.



    Scoped rather than global so that leaving the block restores whatever

    context the caller had, and concurrent code in the same thread is not

    affected by our rounding mode.

    """

    import decimal



    with decimal.localcontext(_MONEY_CONTEXT) as context:

        yield context





def quantize_money(amount: Decimal | int | str) -> Decimal:

    """Quantize a value to :data:`MONEY_SCALE` using banker's rounding.



    Raises

    ------

    AccountingError

        If the value is not a finite decimal. NaN and infinity are rejected

        outright: an infinite balance is always a bug upstream, and allowing

        it to propagate turns one bad record into a poisoned aggregate.

    """

    if isinstance(amount, float):

        raise AccountingError(

            "refusing to quantize a float: binary floats cannot represent "

            "decimal money exactly (0.1 + 0.2 != 0.3). Pass Decimal or str.",

            code="FLOAT_REJECTED",

        )

    with money_context():

        try:

            value = amount if isinstance(amount, Decimal) else Decimal(str(amount))

        except (InvalidOperation, ValueError) as error:

            raise AccountingError(

                f"cannot interpret {amount!r} as a decimal amount",

                code="INVALID_AMOUNT",

            ) from error

        if not value.is_finite():

            raise AccountingError(

                f"monetary amount must be finite, got {value}",

                code="INVALID_AMOUNT",

            )

        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)





def to_micros(amount: Decimal) -> int:

    """Convert a Decimal amount to exact integer micro-units.



    Integer micro-units are the representation every Polars aggregation uses,

    because integer addition is associative and exact while Decimal columns

    are an unstable dtype that can be coerced to Float64 mid-pipeline.

    """

    with money_context():

        return int(quantize_money(amount) * MICRO_FACTOR)





def from_micros(micros: int) -> Decimal:

    """Reconstruct a Decimal amount from integer micro-units."""

    with money_context():

        return quantize_money(Decimal(int(micros)) / MICRO_FACTOR)





# ------------------------------------------------------------------------------

# Errors

# ------------------------------------------------------------------------------





class AccountingError(RuntimeError):

    """Base class for every accounting domain failure."""



    __slots__ = ("code", "details")



    def __init__(

        self,

        message: str,

        *,

        code: str = "ACCOUNTING_ERROR",

        details: dict[str, Any] | None = None,

    ) -> None:

        super().__init__(message)

        self.code: str = code

        self.details: dict[str, Any] = dict(details or {})



    def __str__(self) -> str:

        return f"[{self.code}] {super().__str__()}"





class FXRateUnavailableError(AccountingError):

    """No rate could be obtained from any source, fresh or stale.



    This is a hard stop. The alternative, substituting a guessed or

    last-resort rate, converts an outage into silently mispriced settlements

    that are extremely expensive to unwind later.

    """



    def __init__(

        self,

        base: str,

        quote: str,

        *,

        message: str | None = None,

        details: dict[str, Any] | None = None,

    ) -> None:

        super().__init__(

            message

            or f"no exchange rate available for {base}/{quote}: "

            f"upstream provider failed and no cached rate exists",

            code="FX_UNAVAILABLE",

            details={"base": base, "quote": quote, **(details or {})},

        )

        self.base: str = base

        self.quote: str = quote





class StaleRateError(AccountingError):

    """A rate exists but is older than the caller's tolerance."""



    def __init__(

        self,

        base: str,

        quote: str,

        age_seconds: float,

        max_age_seconds: float,

        *,

        rate: Decimal | None = None,

    ) -> None:

        super().__init__(

            f"rate {base}/{quote} is {age_seconds:.1f}s old, exceeding the "

            f"{max_age_seconds:.1f}s tolerance",

            code="FX_STALE",

            details={

                "base": base,

                "quote": quote,

                "age_seconds": age_seconds,

                "max_age_seconds": max_age_seconds,

            },

        )

        self.base: str = base

        self.quote: str = quote

        self.age_seconds: float = age_seconds

        self.max_age_seconds: float = max_age_seconds

        #: The stale rate itself, so a caller that explicitly accepts staleness

        #: can proceed without a second lookup.

        self.rate: Decimal | None = rate





class CurrencyMismatchError(AccountingError):

    """Arithmetic was attempted between two different currencies."""



    def __init__(self, left: str, right: str, operation: str = "operation") -> None:

        super().__init__(

            f"cannot perform {operation} between {left} and {right}: "

            f"convert to a common currency first",

            code="CURRENCY_MISMATCH",

            details={"left": left, "right": right, "operation": operation},

        )

        self.left: str = left

        self.right: str = right





# ------------------------------------------------------------------------------

# Enums

# ------------------------------------------------------------------------------





class CurrencyCode(str, Enum):

    """ISO 4217 currencies supported by the platform."""



    USD = "USD"

    EUR = "EUR"

    GBP = "GBP"

    INR = "INR"

    AUD = "AUD"

    CAD = "CAD"

    CHF = "CHF"

    JPY = "JPY"

    SGD = "SGD"

    HKD = "HKD"

    NZD = "NZD"

    ZAR = "ZAR"

    AED = "AED"

    BRL = "BRL"



    @property

    def minor_units(self) -> int:

        """Number of decimal places the currency is actually settled in.



        JPY has none: ¥100.50 is not a representable payment amount. Assuming

        two decimals everywhere produces settlement instructions that the

        bookmaker's rails reject or silently truncate.

        """

        return _MINOR_UNITS.get(self, 2)



    @property

    def quantum(self) -> Decimal:

        """Smallest payable increment for this currency."""

        return Decimal(1).scaleb(-self.minor_units)





_MINOR_UNITS: Final[dict[CurrencyCode, int]] = {

    CurrencyCode.JPY: 0,

}





class BetStatus(str, Enum):

    """Settlement state of a single bet."""



    PENDING = "PENDING"

    WON = "WON"

    LOST = "LOST"

    VOID = "VOID"

    HALF_WON = "HALF_WON"

    HALF_LOST = "HALF_LOST"



    @property

    def is_settled(self) -> bool:

        """Whether the bet has reached a final financial state."""

        return self is not BetStatus.PENDING



    @property

    def counts_toward_turnover(self) -> bool:

        """Whether the stake counts as turnover for yield purposes.



        Voided bets are excluded. Including them inflates the denominator of

        every ROI figure with stakes that were never genuinely at risk, which

        systematically understates measured edge.

        """

        return self in {

            BetStatus.WON,

            BetStatus.LOST,

            BetStatus.HALF_WON,

            BetStatus.HALF_LOST,

        }



    @property

    def stake_at_risk_fraction(self) -> Decimal:

        """Fraction of the stake genuinely exposed to loss.



        Asian-handicap half-results split the stake: half is settled and half

        is returned. Treating HALF_LOST as a full loss overstates drawdown by

        exactly 2x on those markets.

        """

        if self in {BetStatus.HALF_WON, BetStatus.HALF_LOST}:

            return Decimal("0.5")

        if self is BetStatus.VOID or self is BetStatus.PENDING:

            return Decimal("0")

        return Decimal("1")





# ------------------------------------------------------------------------------

# Value objects

# ------------------------------------------------------------------------------



_STRICT_MODEL = ConfigDict(

    frozen=True,

    extra="forbid",

    arbitrary_types_allowed=False,

    validate_assignment=True,

)





class Money(BaseModel):

    """An amount inseparably bound to its currency."""



    model_config = _STRICT_MODEL



    value: Decimal

    currency: CurrencyCode



    @field_validator("value", mode="before")

    @classmethod

    def _reject_float_and_quantize(cls, raw: Any) -> Decimal:

        """Coerce to a quantized Decimal, rejecting binary floats.



        ``float`` is refused rather than converted because the conversion is

        already lossy by the time it reaches us: ``Decimal(0.07)`` is

        ``0.070000000000000006938893903907228377647697925567626953125``. The

        caller must supply ``Decimal`` or ``str`` so the intended value is

        unambiguous.

        """

        if isinstance(raw, float):

            raise ValueError(

                "Money.value must not be a float; pass Decimal('12.34') or '12.34'"

            )

        return quantize_money(raw)



    @classmethod

    def zero(cls, currency: CurrencyCode | str) -> Money:

        """Return a zero amount in ``currency``."""

        return cls(value=Decimal("0"), currency=CurrencyCode(currency))



    @classmethod

    def from_micros(cls, micros: int, currency: CurrencyCode | str) -> Money:

        """Rebuild a Money from integer micro-units."""

        return cls(value=from_micros(micros), currency=CurrencyCode(currency))



    @property

    def micros(self) -> int:

        """Exact integer micro-unit representation."""

        return to_micros(self.value)



    @property

    def is_zero(self) -> bool:

        """Whether the amount is exactly zero."""

        return self.value == 0



    def to_minor_units(self) -> int:

        """Quantize to the currency's payable increment and return an integer.



        Use at the payment boundary only. Quantizing earlier discards

        precision that intermediate FX steps legitimately need.

        """

        with money_context():

            quantized = self.value.quantize(

                self.currency.quantum, rounding=ROUND_HALF_EVEN

            )

            return int(quantized.scaleb(self.currency.minor_units))



    def _require_same_currency(self, other: Money, operation: str) -> None:

        if self.currency is not other.currency:

            raise CurrencyMismatchError(

                self.currency.value, other.currency.value, operation

            )



    def __add__(self, other: Money) -> Money:

        self._require_same_currency(other, "addition")

        with money_context():

            return Money(value=self.value + other.value, currency=self.currency)



    def __sub__(self, other: Money) -> Money:

        self._require_same_currency(other, "subtraction")

        with money_context():

            return Money(value=self.value - other.value, currency=self.currency)



    def __mul__(self, factor: Decimal | int) -> Money:

        """Scale by a dimensionless factor.



        Money times money is meaningless, so only scalars are accepted.

        """

        if isinstance(factor, float):

            raise AccountingError(

                "cannot scale Money by a float; use Decimal",

                code="FLOAT_REJECTED",

            )

        with money_context():

            return Money(value=self.value * Decimal(factor), currency=self.currency)



    def __neg__(self) -> Money:

        return Money(value=-self.value, currency=self.currency)



    def __lt__(self, other: Money) -> bool:

        self._require_same_currency(other, "comparison")

        return self.value < other.value



    def __le__(self, other: Money) -> bool:

        self._require_same_currency(other, "comparison")

        return self.value <= other.value



    def __str__(self) -> str:

        return f"{self.value} {self.currency.value}"





class ExchangeRate(BaseModel):

    """A single directional rate with provenance and an observation time."""



    model_config = _STRICT_MODEL



    base: CurrencyCode

    quote: CurrencyCode

    rate: Decimal = Field(gt=Decimal("0"))

    fetched_at: datetime

    source: str = "unknown"



    @field_validator("rate", mode="before")

    @classmethod

    def _coerce_rate(cls, raw: Any) -> Decimal:

        if isinstance(raw, float):

            raise ValueError("ExchangeRate.rate must not be a float; use Decimal or str")

        with money_context():

            value = raw if isinstance(raw, Decimal) else Decimal(str(raw))

        if not value.is_finite() or value <= 0:

            raise ValueError(f"exchange rate must be finite and positive, got {value}")

        return value



    @field_validator("fetched_at")

    @classmethod

    def _require_tz_aware(cls, raw: datetime) -> datetime:

        """Reject naive timestamps.



        A naive timestamp makes staleness arithmetic depend on the server's

        local zone, which is how a rate cache silently serves 6-hour-old

        rates as fresh after a DST change.

        """

        if raw.tzinfo is None:

            raise ValueError("fetched_at must be timezone-aware")

        return raw.astimezone(UTC)



    def age_seconds(self, *, now: datetime | None = None) -> float:

        """Seconds elapsed since this rate was observed."""

        reference = now or datetime.now(tz=UTC)

        return max(0.0, (reference - self.fetched_at).total_seconds())



    def is_fresh(self, ttl_seconds: float, *, now: datetime | None = None) -> bool:

        """Whether the rate is within ``ttl_seconds`` of its observation."""

        return self.age_seconds(now=now) < ttl_seconds



    def inverted(self) -> ExchangeRate:

        """Return the reciprocal rate.



        Inversion is computed at full working precision before quantization;

        inverting an already-rounded rate and rounding again compounds the

        error on every hop of a cross-rate chain.

        """

        with money_context():

            return ExchangeRate(

                base=self.quote,

                quote=self.base,

                rate=Decimal(1) / self.rate,

                fetched_at=self.fetched_at,

                source=f"{self.source}:inverted",

            )





class LedgerEntry(BaseModel):

    """One settled (or pending) bet as recorded in the ledger."""



    model_config = _STRICT_MODEL



    bet_id: str = Field(min_length=1)

    placed_at: datetime

    settled_at: datetime | None = None

    sport: str = Field(min_length=1)

    archetype: str = Field(min_length=1)

    bookmaker: str = Field(min_length=1)

    market_id: str = Field(min_length=1)

    selection: str = Field(min_length=1)

    status: BetStatus

    stake: Money

    payout: Money

    odds: Decimal = Field(gt=Decimal("1"))

    model_probability: Decimal | None = None



    @field_validator("placed_at", "settled_at")

    @classmethod

    def _require_tz_aware(cls, raw: datetime | None) -> datetime | None:

        if raw is None:

            return None

        if raw.tzinfo is None:

            raise ValueError("ledger timestamps must be timezone-aware")

        return raw.astimezone(UTC)



    @field_validator("odds", "model_probability", mode="before")

    @classmethod

    def _coerce_decimal(cls, raw: Any) -> Any:

        if raw is None:

            return None

        if isinstance(raw, float):

            raise ValueError("odds and probabilities must be Decimal or str, not float")

        return raw if isinstance(raw, Decimal) else Decimal(str(raw))



    @model_validator(mode="after")

    def _check_settlement_coherence(self) -> LedgerEntry:

        """Reject internally inconsistent settlements.



        These combinations indicate an upstream settlement bug. Admitting them

        produces a ledger that balances arithmetically while describing

        something that never happened.

        """

        if self.status is BetStatus.PENDING:

            if not self.payout.is_zero:

                raise ValueError("a PENDING bet cannot carry a non-zero payout")

        elif self.settled_at is None:

            raise ValueError(f"a {self.status.value} bet requires settled_at")



        if self.status is BetStatus.LOST and not self.payout.is_zero:

            raise ValueError("a LOST bet must have a zero payout")



        if self.settled_at is not None and self.settled_at < self.placed_at:

            raise ValueError("settled_at cannot precede placed_at")



        if self.model_probability is not None and not (

            Decimal("0") <= self.model_probability <= Decimal("1")

        ):

            raise ValueError(

                f"model_probability must lie in [0, 1], got {self.model_probability}"

            )

        return self



    @property

    def is_same_currency(self) -> bool:

        """Whether stake and payout share a currency."""

        return self.stake.currency is self.payout.currency
