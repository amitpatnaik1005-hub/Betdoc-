
"""Bookmaker-agnostic order execution contract.



Every adapter normalises its wire protocol into :class:`ExecutionResult` so

that strategy and reconciliation code never branches on bookmaker specifics.

All monetary values crossing this boundary are :class:`~decimal.Decimal` INR.

"""



from __future__ import annotations



from abc import ABC, abstractmethod

from dataclasses import dataclass, field

from datetime import datetime, timezone

from decimal import Decimal

from enum import Enum

from types import TracebackType

from typing import Any, Final, Mapping



__all__ = ["ExecutionClient", "ExecutionError", "ExecutionResult", "ExecutionStatus"]



MONEY_EXPONENT: Final[Decimal] = Decimal("0.01")

ODDS_EXPONENT: Final[Decimal] = Decimal("0.01")





class ExecutionStatus(str, Enum):

    """Normalised lifecycle state of an execution attempt."""



    ACCEPTED = "ACCEPTED"

    PARTIALLY_MATCHED = "PARTIALLY_MATCHED"

    PENDING = "PENDING"

    CANCELLED = "CANCELLED"

    EXPIRED = "EXPIRED"

    REJECTED = "REJECTED"

    FAILED = "FAILED"



    @property

    def is_terminal(self) -> bool:

        """Whether no further state transition is expected."""

        return self in {

            ExecutionStatus.CANCELLED,

            ExecutionStatus.EXPIRED,

            ExecutionStatus.REJECTED,

            ExecutionStatus.FAILED,

        }



    @property

    def is_successful(self) -> bool:

        """Whether the bookmaker accepted the instruction."""

        return self in {

            ExecutionStatus.ACCEPTED,

            ExecutionStatus.PARTIALLY_MATCHED,

            ExecutionStatus.PENDING,

            ExecutionStatus.CANCELLED,

        }





class ExecutionError(RuntimeError):

    """Raised when an execution instruction cannot be completed.



    Parameters

    ----------

    message:

        Operator-facing description of the failure.

    code:

        Stable machine readable code, e.g. ``UNSUPPORTED``, ``TRANSPORT``,

        ``NOT_AUTHENTICATED``, ``REJECTED``, ``TIMEOUT``.

    bookmaker:

        Name of the adapter that raised the error, when known.

    details:

        Structured non-secret context, safe to log.

    """



    __slots__ = ("bookmaker", "code", "details")



    def __init__(

        self,

        message: str = "execution failed",

        *,

        code: str = "EXECUTION_ERROR",

        bookmaker: str | None = None,

        details: Mapping[str, Any] | None = None,

    ) -> None:

        super().__init__(message)

        self.code: str = code

        self.bookmaker: str | None = bookmaker

        self.details: dict[str, Any] = dict(details or {})



    def __str__(self) -> str:

        prefix = f"{self.bookmaker}/" if self.bookmaker else ""

        return f"[{prefix}{self.code}] {super().__str__()}"





@dataclass(frozen=True, slots=True)

class ExecutionResult:

    """Normalised outcome of a single execution instruction."""



    success: bool

    status: ExecutionStatus

    bookmaker: str

    order_id: str | None = None

    bet_id: str | None = None

    market_id: str | None = None

    selection_id: int | None = None

    requested_odds: Decimal = Decimal("0")

    matched_odds: Decimal = Decimal("0")

    requested_stake: Decimal = Decimal("0")

    matched_stake: Decimal = Decimal("0")

    remaining_stake: Decimal = Decimal("0")

    currency: str = "INR"

    code: str | None = None

    message: str | None = None

    placed_at: datetime = field(

        default_factory=lambda: datetime.now(tz=timezone.utc)

    )

    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)



    @property

    def is_fully_matched(self) -> bool:

        """Whether the entire requested stake was matched."""

        return (

            self.requested_stake > 0

            and self.matched_stake >= self.requested_stake

            and self.remaining_stake == 0

        )



    @property

    def matched_liability(self) -> Decimal:

        """Cash actually committed at the matched price."""

        return (self.matched_stake).quantize(MONEY_EXPONENT)



    @property

    def potential_return(self) -> Decimal:

        """Gross return if the matched portion wins."""

        if self.matched_stake <= 0 or self.matched_odds <= 0:

            return Decimal("0.00")

        return (self.matched_stake * self.matched_odds).quantize(MONEY_EXPONENT)



    @classmethod

    def failure(

        cls,

        bookmaker: str,

        *,

        code: str,

        message: str,

        status: ExecutionStatus = ExecutionStatus.FAILED,

        market_id: str | None = None,

        selection_id: int | None = None,

        requested_odds: Decimal = Decimal("0"),

        requested_stake: Decimal = Decimal("0"),

        raw: Mapping[str, Any] | None = None,

    ) -> "ExecutionResult":

        """Construct a failed result without raising."""

        return cls(

            success=False,

            status=status,

            bookmaker=bookmaker,

            market_id=market_id,

            selection_id=selection_id,

            requested_odds=requested_odds,

            requested_stake=requested_stake,

            code=code,

            message=message,

            raw=dict(raw or {}),

        )



    def to_dict(self) -> dict[str, Any]:

        """Return a JSON-safe projection for audit logging."""

        return {

            "success": self.success,

            "status": self.status.value,

            "bookmaker": self.bookmaker,

            "order_id": self.order_id,

            "bet_id": self.bet_id,

            "market_id": self.market_id,

            "selection_id": self.selection_id,

            "requested_odds": str(self.requested_odds),

            "matched_odds": str(self.matched_odds),

            "requested_stake": str(self.requested_stake),

            "matched_stake": str(self.matched_stake),

            "remaining_stake": str(self.remaining_stake),

            "currency": self.currency,

            "code": self.code,

            "message": self.message,

            "placed_at": self.placed_at.isoformat(),

        }





class ExecutionClient(ABC):

    """Abstract async execution client for a single bookmaker.



    Subclasses implement the wire protocol. Used as an async context manager,

    :meth:`authenticate` runs on entry and :meth:`close` always runs on exit,

    including on an exception path.

    """



    __slots__ = ("_authenticated", "_name")



    def __init__(self, name: str) -> None:

        if not name:

            raise ExecutionError("bookmaker name must not be empty", code="MALFORMED")

        self._name: str = name

        self._authenticated: bool = False



    @property

    def name(self) -> str:

        """Bookmaker identifier used in results and audit entries."""

        return self._name



    @property

    def is_authenticated(self) -> bool:

        """Whether a usable session is currently held."""

        return self._authenticated



    async def __aenter__(self) -> "ExecutionClient":

        try:

            await self.authenticate()

        except BaseException:

            await self.close()

            raise

        return self



    async def __aexit__(

        self,

        exc_type: type[BaseException] | None,

        exc: BaseException | None,

        tb: TracebackType | None,

    ) -> None:

        await self.close()



    @abstractmethod

    async def authenticate(self) -> None:

        """Establish an authenticated session with the bookmaker."""



    @abstractmethod

    async def get_balance(self) -> Decimal:

        """Return the available balance as a ``Decimal`` INR amount."""



    @abstractmethod

    async def place_order(

        self,

        market_id: str,

        selection_id: int,

        odds: Decimal,

        stake: Decimal,

    ) -> ExecutionResult:

        """Submit a back order and return the normalised outcome."""



    @abstractmethod

    async def cancel_order(self, order_id: str) -> ExecutionResult:

        """Cancel a resting order by bookmaker order identifier."""



    @abstractmethod

    async def cashout(self, bet_id: str) -> ExecutionResult:

        """Cash out an open bet, where the bookmaker supports it."""



    @abstractmethod

    async def close(self) -> None:

        """Release sockets and discard session state. Must be idempotent."""



    @staticmethod

    def _quantize_money(amount: Decimal) -> Decimal:

        """Round a monetary amount to two decimal places."""

        return Decimal(amount).quantize(MONEY_EXPONENT)



    @staticmethod

    def _quantize_odds(odds: Decimal) -> Decimal:

        """Round decimal odds to two decimal places."""

        return Decimal(odds).quantize(ODDS_EXPONENT)



    def __repr__(self) -> str:

        return (

            f"{type(self).__name__}(name={self._name!r}, "

            f"authenticated={self._authenticated})"

        )


