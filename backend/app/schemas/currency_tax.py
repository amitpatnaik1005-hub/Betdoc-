"""Contracts for TODAR MAL's exchange rates and the Section 115BBJ estimator (``/currency``, ``/tax``).

Moved out of the retired Paper Trading Sandbox in Group 97; the shapes are unchanged. ``developer_credit`` is read
from ``system_settings`` by each endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CurrencyRateRead(BaseModel):
    base_currency: str
    quote_currency: str
    rate: float
    last_updated_at: datetime
    provider: str
    developer_credit: str


class ConversionRead(BaseModel):
    amount: float
    from_currency: str
    to_currency: str
    converted: float
    rate: float
    last_updated_at: datetime
    provider: str
    developer_credit: str


class CurrencyBoardRead(BaseModel):
    base: str
    rates: dict[str, dict[str, Any]]
    missing: dict[str, str]
    max_age_seconds: int
    developer_credit: str


class TaxEstimateResponse(BaseModel):
    gross_win: float
    stake: float
    gross_profit: float
    tds_rate_pct: float
    tds_deducted: float
    net_take_home: float
    cess: float
    effective_rate_pct: float
    currency: str = "INR"
    jurisdiction: str = "IN (Section 115BBJ)"
    compliance_notes: str = ("Section 115BBJ: a flat 30% on the net winnings of each bet (plus 4% cess on the tax), no deduction "
                             "or set-off of losses; Section 194BA withholds it on withdrawal and at the financial year's end.")
    developer_credit: str
