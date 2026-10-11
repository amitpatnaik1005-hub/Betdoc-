"""
Pydantic v2 Schemas for Paper Trading, Currency and Section 115BBJ Tax Ledger (Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Money crosses the wire as numbers rounded to the paisa (the database keeps exact decimals). A paper bet is either
one flat leg (``sport_key``, ``match_id``, ``market_type``, ``selection``, ``odds``) or ``legs`` (a parlay of up
to 12, one per match); an Ashoka slip arrives in the Oracle's market grammar through ``OracleSlipCreate``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
MAX_LEGS = 12
Currency = Literal["INR", "USD", "EUR", "GBP", "USDT", "BTC"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================================================================ placing
class PaperLegCreate(_In):
    sport_key: str = Field(min_length=1, max_length=64, description="Sport identifier e.g. cricket, soccer, basketball")
    match_id: str = Field(min_length=1, max_length=128, description="Unique fixture identifier")
    event_name: str = Field(default="", max_length=255, description="E.g. Arsenal vs Chelsea")
    market_type: str = Field(min_length=1, max_length=64, description="E.g. moneyline, 1x2_match_odds, total, asian_handicap")
    selection: str = Field(min_length=1, max_length=64, description="E.g. home, away, draw, over")
    odds: float = Field(gt=1.0, le=1000.0, description="Decimal odds requested at placement time")
    line: float | None = Field(default=None, ge=-500, le=5000)
    params: dict[str, Any] = Field(default_factory=dict)
    quote_market: str | None = Field(default=None, max_length=64, description="The book's market key to snap the live price with")
    quote_selection: str | None = Field(default=None, max_length=32)


class PaperBetCreate(_In):
    """A single (the flat leg fields) or a parlay (``legs``), staked in virtual rupees."""

    sport_key: str | None = Field(default=None, min_length=1, max_length=64)
    match_id: str | None = Field(default=None, min_length=1, max_length=128)
    event_name: str | None = Field(default=None, max_length=255)
    market_type: str | None = Field(default=None, min_length=1, max_length=64)
    selection: str | None = Field(default=None, min_length=1, max_length=64)
    odds: float | None = Field(default=None, gt=1.0, le=1000.0)
    line: float | None = Field(default=None, ge=-500, le=5000)
    params: dict[str, Any] = Field(default_factory=dict)
    quote_market: str | None = Field(default=None, max_length=64)
    quote_selection: str | None = Field(default=None, max_length=32)
    legs: list[PaperLegCreate] | None = Field(default=None, min_length=1, max_length=MAX_LEGS)
    stake_amount: float = Field(gt=0.0, le=1e9, description="Virtual stake in INR")
    target_bookmaker: str = Field(default="parimatch", max_length=32, description="Bookmaker line being forward-tested")
    source: Literal["MANUAL", "ASHOKA", "SPORTS_ENGINE", "BACKTEST"] = "MANUAL"
    strategy: str | None = Field(default=None, max_length=64)
    fair_probability: float | None = Field(default=None, gt=0, lt=1)
    confidence_score: float | None = Field(default=None, ge=0, le=100)
    client_ref: str | None = Field(default=None, min_length=1, max_length=64, description="Idempotency key")

    @model_validator(mode="after")
    def _shape(self) -> PaperBetCreate:
        flat = [self.sport_key, self.match_id, self.market_type, self.selection, self.odds]
        if self.legs is None and any(v is None for v in flat):
            raise ValueError("a single needs sport_key, match_id, market_type, selection and odds (or send legs)")
        if self.legs is not None and any(v is not None for v in flat):
            raise ValueError("send either the flat single fields or legs, not both")
        return self

    def leg_list(self) -> list[PaperLegCreate]:
        if self.legs is not None:
            return list(self.legs)
        return [PaperLegCreate(sport_key=self.sport_key or "", match_id=self.match_id or "", event_name=self.event_name or "",
                               market_type=self.market_type or "", selection=self.selection or "", odds=self.odds or 0.0, line=self.line,
                               params=self.params, quote_market=self.quote_market, quote_selection=self.quote_selection)]


class OracleLegCreate(_In):
    sport_key: str | None = Field(default=None, max_length=64, description="The Odds API sport key (soccer_epl); absent means soccer")
    match_id: str = Field(min_length=1, max_length=128)
    market: str = Field(min_length=1, max_length=64, description="Oracle grammar: Match Odds, Totals 2.5, Asian Handicap -0.75, BTTS...")
    selection: str = Field(min_length=1, max_length=16, description="HOME, DRAW, AWAY, OVER, UNDER, YES, NO, 1X, 12, X2")
    odds: float = Field(gt=1.0, le=1000.0)
    event_name: str = Field(default="", max_length=255)


class OracleSlipCreate(_In):
    """An Ashoka slip forward-tested on the sandbox at the target book's live price."""

    legs: list[OracleLegCreate] = Field(min_length=1, max_length=MAX_LEGS)
    stake_amount: float = Field(gt=0.0, le=1e9)
    target_bookmaker: str = Field(default="parimatch", max_length=32)
    fair_probability: float | None = Field(default=None, gt=0, lt=1)
    confidence_score: float | None = Field(default=None, ge=0, le=100)
    strategy: str | None = Field(default="ashoka", max_length=64)
    slip_id: str | None = Field(default=None, max_length=64, description="Ashoka's slip id: the idempotency key")
    event_name: str | None = Field(default=None, max_length=255)


class PaperSettleRequest(_In):
    """A hand settlement: the final result of the bet's match, or one per match (parlays)."""

    match_result: dict[str, Any] | None = None
    results_by_match: dict[str, dict[str, Any]] | None = None
    settlement_notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one(self) -> PaperSettleRequest:
        if (self.match_result is None) == (self.results_by_match is None):
            raise ValueError("send match_result (a single) or results_by_match (keyed by match id)")
        return self


class CashOutRequest(_In):
    amount: float = Field(ge=0, le=1e10, description="The virtual cash-out value in INR")


class LiveCheckRequest(_In):
    """FA-1 on a paper bet: the live state of its match, scored by the sport's adapter."""

    home_stats: dict[str, Any] = Field(default_factory=dict)
    away_stats: dict[str, Any] = Field(default_factory=dict)
    live_state: dict[str, Any]
    context: dict[str, Any] | None = None
    side: Literal["home", "away", "draw"] | None = Field(default=None, description="The backed side; read from the bet when absent")
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    cashout_offer_inr: float | None = Field(default=None, ge=0, le=1e10)


class ResetRequest(_In):
    initial_balance: float | None = Field(default=None, gt=0, le=1e9)


class LiveTicketRequest(_In):
    target_bookmaker: str = Field(default="parimatch", max_length=32)
    odds: float = Field(gt=1.0, le=1000.0)
    stake_amount: float = Field(gt=0, le=1e9)
    description: str = Field(min_length=1, max_length=255)


# ================================================================ reading
class PaperLegRead(_Out):
    sport_key: str
    match_id: str
    event_name: str = ""
    market_type: str
    selection: str
    line: float | None = None
    odds: float
    executed_odds: float
    status: str
    quote_market: str | None = None
    quote_selection: str | None = None


class PaperBetRead(_Out):
    id: uuid.UUID
    account_id: uuid.UUID
    client_ref: str
    structure: str
    sport_key: str
    match_id: str
    event_name: str
    market_type: str
    selection: str
    line: float | None = None
    legs: list[PaperLegRead]
    odds: float
    executed_odds: float
    stake_amount: float
    potential_payout: float
    status: str
    payout: float | None = None
    pnl: float
    tds_amount: float
    net_post_tax_payout: float | None = None
    target_bookmaker: str
    source: str
    strategy: str | None = None
    fair_probability: float | None = None
    confidence_score: float | None = None
    kelly_stake: float | None = None
    execution: dict[str, Any] | None = None
    discipline: dict[str, Any] | None = None
    placed_at: datetime
    settled_at: datetime | None = None
    settlement_notes: str | None = None
    created: bool = True
    developer_credit: str = DEVELOPER_CREDIT


class PaperAccountRead(_Out):
    id: uuid.UUID
    user_id: uuid.UUID
    currency: str
    virtual_balance: float
    initial_balance: float
    total_paper_bets: int
    won_bets: int
    lost_bets: int
    push_bets: int
    cashed_out_bets: int
    total_staked: float
    total_returned: float
    total_pnl: float
    roi_pct: float
    total_tds: float
    peak_equity: float
    max_drawdown_pct: float
    resets: int
    is_active: bool
    last_reset_at: datetime | None = None
    created_at: datetime
    developer_credit: str = DEVELOPER_CREDIT


class SignificanceRead(BaseModel):
    sample: int
    mean_return: float
    std_return: float
    t_stat: float | None = None
    p_value: float | None = None
    proven: bool
    verdict: str


class PaperPerformanceStats(BaseModel):
    virtual_balance: float
    initial_balance: float
    equity: float
    total_pnl: float
    net_pnl_after_tds: float
    total_tds: float
    roi_pct: float
    yield_pct: float
    win_rate_pct: float
    total_bets: int
    won_bets: int
    lost_bets: int
    push_bets: int
    cashed_out_bets: int
    pending_bets: int
    open_exposure: float
    max_drawdown_pct: float
    profit_factor: float | None = None
    sharpe_ratio: float | None = None
    brier_score: float | None = None
    significance: SignificanceRead
    status_summary: str
    live_ready: bool
    by_sport: dict[str, dict[str, Any]]
    by_strategy: dict[str, dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    epoch: int
    developer_credit: str = DEVELOPER_CREDIT


class LiveCheckResponse(BaseModel):
    bet_id: uuid.UUID
    sport_key: str
    side: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    live_probability: float
    alert: bool
    alert_threshold: float
    shield: dict[str, Any]
    message: str
    developer_credit: str = DEVELOPER_CREDIT


class CurrencyRateRead(BaseModel):
    base_currency: str
    quote_currency: str
    rate: float
    last_updated_at: datetime
    provider: str
    developer_credit: str = DEVELOPER_CREDIT


class ConversionRead(BaseModel):
    amount: float
    from_currency: str
    to_currency: str
    converted: float
    rate: float
    last_updated_at: datetime
    provider: str
    developer_credit: str = DEVELOPER_CREDIT


class CurrencyBoardRead(BaseModel):
    base: str
    rates: dict[str, dict[str, Any]]
    missing: dict[str, str]
    max_age_seconds: int
    developer_credit: str = DEVELOPER_CREDIT


class TaxEstimateResponse(BaseModel):
    gross_win: float
    stake: float
    gross_profit: float
    tds_rate_pct: float = 30.0
    tds_deducted: float
    net_take_home: float
    cess: float
    effective_rate_pct: float
    currency: str = "INR"
    jurisdiction: str = "IN (Section 115BBJ)"
    compliance_notes: str = ("Section 115BBJ: a flat 30% on the net winnings of each bet (plus 4% cess on the tax), no deduction "
                             "or set-off of losses; Section 194BA withholds it on withdrawal and at the financial year's end.")
    developer_credit: str = DEVELOPER_CREDIT


class TaxLedgerEntryRead(_Out):
    id: uuid.UUID
    paper_bet_id: uuid.UUID | None = None
    mode: str
    gross_payout: float
    stake: float
    net_winnings: float
    tds_rate: float
    tds_amount: float
    net_post_tax_payout: float
    currency: str
    jurisdiction: str
    section: str
    financial_year: str
    created_at: datetime


class TaxLedgerRead(BaseModel):
    entries: list[TaxLedgerEntryRead]
    by_financial_year: dict[str, dict[str, float]]
    developer_credit: str = DEVELOPER_CREDIT


class ExecutionReceiptRead(BaseModel):
    commander: str
    mode: str
    status: str
    venue: str
    venue_url: str | None = None
    requested_odds: str
    executed_odds: str | None = None
    live_odds: str | None = None
    slippage_pct: float | None = None
    price_source: str
    quote_age_seconds: float | None = None
    router_latency_ms: float
    venue_latency_ms: float
    total_latency_ms: float
    slip_ref: str
    reason: str | None = None
    legs: list[dict[str, Any]] = Field(default_factory=list)
    developer_credit: str = DEVELOPER_CREDIT
