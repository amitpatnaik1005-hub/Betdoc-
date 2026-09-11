"""Financial domain for BetDoc: ledger, FX, and performance reporting.



Every monetary quantity in this package is a :class:`Money` value object

carrying an explicit currency. Bare numerics are rejected at construction

time, which makes an unconverted-currency bug a validation error at the

boundary instead of a silently wrong P&L six months later.

"""



from __future__ import annotations

from betdoc.domain.accounting.currency import (
    CurrencyConverter,
    InMemoryRateCache,
    RateCache,
    RedisRateCache,
    StaticRateProvider,
)
from betdoc.domain.accounting.ledger import (
    LEDGER_SCHEMA,
    LedgerService,
    PnLSummary,
)
from betdoc.domain.accounting.reporting import (
    PerformanceRow,
    ReportGenerator,
)
from betdoc.domain.accounting.types import (
    MONEY_SCALE,
    AccountingError,
    BetStatus,
    CurrencyCode,
    CurrencyMismatchError,
    ExchangeRate,
    FXRateUnavailableError,
    LedgerEntry,
    Money,
    StaleRateError,
    money_context,
    quantize_money,
)

__all__ = [

    "LEDGER_SCHEMA",

    "MONEY_SCALE",

    "AccountingError",

    "BetStatus",

    "CurrencyCode",

    "CurrencyConverter",

    "CurrencyMismatchError",

    "ExchangeRate",

    "FXRateUnavailableError",

    "InMemoryRateCache",

    "LedgerEntry",

    "LedgerService",

    "Money",

    "PerformanceRow",

    "PnLSummary",

    "RateCache",

    "RedisRateCache",

    "ReportGenerator",

    "StaleRateError",

    "StaticRateProvider",

    "money_context",

    "quantize_money",

]
