"""
Pydantic Schemas for the Real-Money Betting Gateway (Group 95).
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Money crosses the wire as rupee numbers rounded to the paisa (the vault keeps exact decimals). Every response carries
``developer_name``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
Outcome = Literal["WON", "HALF_WON", "LOST", "HALF_LOST", "PUSH", "VOID", "CANCELLED"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================================================================ requests
class DepositRequest(_In):
    amount: float = Field(gt=0, le=1e9, description="Amount in INR to deposit")
    currency: Literal["INR"] = "INR"
    reference: str | None = Field(default="UPI/IMPS Deposit", max_length=64)
    notes: str | None = Field(default=None, max_length=500)


class WithdrawRequest(_In):
    amount: float = Field(gt=0, le=1e9, description="Amount in INR to withdraw")
    notes: str | None = Field(default=None, max_length=500)


class CreateRealTicketRequest(_In):
    sport: str = Field(min_length=1, max_length=64, description="soccer, cricket, basketball, tennis, baseball, nfl, nhl, mma, esports")
    league: str = Field(min_length=1, max_length=128)
    match_id: str = Field(min_length=1, max_length=128)
    event_name: str = Field(min_length=1, max_length=255)
    market_type: str = Field(min_length=1, max_length=100, description="Match Odds, Over/Under 2.5, Asian Handicap -0.5, Both Teams To Score...")
    selection: str = Field(min_length=1, max_length=128, description="Home, Away, Draw, Over 2.5, Yes...")
    target_odds: float = Field(gt=1.0, le=1000.0)
    true_probability: float = Field(gt=0, lt=1, description="Aryabhata's consensus probability of the selection")
    model_edge_pct: float | None = Field(default=None, ge=-100, le=10000, description="Informational: the desk recomputes it from p and the odds")
    vetting_pillar_score: float = Field(default=90.0, ge=0, le=100)
    bookmaker: str = Field(default="parimatch", max_length=16)
    line: float | None = Field(default=None, ge=-500, le=5000)
    quote_market: str | None = Field(default=None, max_length=64, description="The books' market key (Match Odds, Totals 2.5) for the line-drift check")
    quote_selection: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=500)


class ConfirmPlacementRequest(_In):
    ticket_reference: str = Field(min_length=1, max_length=32)
    placed_odds: float = Field(gt=1.0, le=1000.0)
    placed_stake: float = Field(gt=0, le=1e9)
    bookmaker_slip_id: str | None = Field(default=None, max_length=64)
    override_odds_decay: bool = False


class SettleRealTicketRequest(_In):
    ticket_reference: str = Field(min_length=1, max_length=32)
    outcome: Outcome | None = None
    scorecard: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _something(self) -> SettleRealTicketRequest:
        if self.outcome is None and not self.scorecard:
            raise ValueError("send the outcome or the match's scorecard")
        if self.scorecard is not None and len(self.scorecard) > 64:
            raise ValueError("the scorecard holds at most 64 fields")
        return self


# ================================================================ responses
class RealBankrollResponse(BaseModel):
    user_id: str
    currency: str = "INR"
    cash_balance: float
    locked_exposure: float
    total_bankroll: float  # cash_balance + locked_exposure
    total_deposited: float
    total_withdrawn: float
    realized_pnl: float
    gross_winnings: float
    total_tds_deducted: float
    high_water_mark: float
    daily_starting_balance: float
    daily_realized_loss: float
    daily_drawdown_pct: float
    daily_drawdown_limit_pct: float
    consecutive_losses: int
    loss_streak_limit: int
    stop_loss_triggered: bool
    stop_loss_until: str | None = None
    is_in_cooldown: bool
    cooldown_seconds_remaining: int
    max_single_stake: float
    max_stake_pct: float
    trading_day: str | None = None
    can_bet: bool
    developer_name: str = DEVELOPER_CREDIT


class RealBetTicketResponse(BaseModel):
    id: str
    ticket_reference: str
    bookmaker: str
    sport: str
    league: str
    match_id: str
    event_name: str
    market_type: str
    selection: str
    recommended_stake: float
    placed_stake: float
    currency: str = "INR"
    target_odds: float
    placed_odds: float | None = None
    minimum_profitable_odds: float
    true_probability: float
    model_edge_pct: float
    kelly_fraction: float
    deep_link_url: str
    clipboard_payload: str
    bookmaker_slip_id: str | None = None
    quote_market: str | None = None
    quote_selection: str | None = None
    settleable_from_score: bool
    verdict_id: str | None = None  # Group 114: the signed verdict a Bet-Slip ticket was placed from
    user_placed_bet_id: str | None = None  # and the Oracle bet that settles it
    status: str
    gross_return: float
    settlement_payout: float
    net_profit: float
    tds_deducted: float
    vetting_pillar_score: float
    odds_decay_flag: bool
    notes: str | None = None
    placed_at: str | None = None
    settled_at: str | None = None
    created_at: str
    developer_name: str = DEVELOPER_CREDIT


class RealCashTransactionResponse(BaseModel):
    id: str
    ticket_id: str | None = None
    transaction_type: str
    amount: float
    currency: str = "INR"
    balance_before: float
    balance_after: float
    reference_id: str | None = None
    description: str
    created_at: str
    developer_name: str = DEVELOPER_CREDIT


class OddsCheckResponse(BaseModel):
    ticket_reference: str
    bookmaker: str
    current_odds: float | None = None
    target_odds: float
    minimum_profitable_odds: float
    odds_decay_detected: bool
    is_suspended: bool
    as_of: str | None = None
    developer_name: str = DEVELOPER_CREDIT


# ================================================================ Group 114: the handcrafted desk
class ConfirmHandcraftedPlacementRequest(_In):
    report_id: uuid.UUID = Field(description="The Consultant verdict (report) id the Bet-Slip was issued for")
    booking_code: str = Field(min_length=2, max_length=32, description="The booking code the bookmaker issued for the placed slip")
    placed_odds: float | None = Field(default=None, gt=1.0, le=1000.0, description="What the book gave; omitted: the ticket's odds")
    placed_stake_inr: float | None = Field(default=None, gt=0, le=1e9, description="What was staked; omitted: the verdict's stake")
    override_odds_decay: bool = False


class ConfirmTicketPlacementRequest(_In):
    """Group 115: a ticket the desk recorded (a system split), placed by hand at the book."""

    booking_code: str = Field(min_length=2, max_length=32, description="The booking code the bookmaker issued for the placed ticket")
    placed_odds: float | None = Field(default=None, gt=1.0, le=1e6, description="What the book gave; omitted: the ticket's odds")
    placed_stake_inr: float | None = Field(default=None, gt=0, le=1e9, description="What was staked; omitted: the ticket's stake")
    override_odds_decay: bool = False


class HandcraftedPlacementResponse(BaseModel):
    ticket: RealBetTicketResponse
    booking_code: str | None = None
    user_placed_bet_id: str
    stop_loss_pct: float
    stop_loss_floor_inr: float
    monitor: dict[str, Any] | None = None
    monitor_note: str | None = None
    replayed: bool = False
    developer_name: str = DEVELOPER_CREDIT


class CashoutEvaluationResponse(BaseModel):
    ticket_reference: str
    advice: Literal["HOLD", "CASHOUT_ADVISED", "STOP_LOSS_BREACH"]
    message: str
    current_offer: float
    fair_value: float
    stop_loss_floor: float
    stop_loss_pct: float
    hold_ratio: float | None = None
    target_profit_pct: float
    win_probability: float
    probability_source: Literal["LIVE", "LAST_TICK", "ENTRY"]
    placed_stake: float
    placed_odds: float | None = None
    twin_call: dict[str, str] | None = None
    developer_name: str = DEVELOPER_CREDIT


class ExecuteCashoutRequest(_In):
    ticket_reference: str = Field(min_length=1, max_length=32)
    cashout_amount_inr: float = Field(gt=0, le=1e9, description="What the book paid for the cashout (gross)")
    reason: str = Field(default="CASHOUT_ADVISED", max_length=32, description="STOP_LOSS_BREACH, CASHOUT_ADVISED, HOLD or a twin pullout reason")


class ExecuteCashoutResponse(BaseModel):
    ticket_reference: str
    status: str
    gross_cashout: float
    placed_stake: float
    net_winnings: float
    tds_deducted: float
    net_payout: float
    net_profit: float
    liquid_cash_balance: float
    locked_exposure: float
    monitor_closed: bool
    developer_name: str = DEVELOPER_CREDIT


class VaultReconciliationResponse(BaseModel):
    reconciled: bool  # the cash ledger agrees with liquid cash (exposure is always brought to the open tickets)
    open_tickets_count: int
    previous_exposure: float
    locked_exposure: float
    exposure_drift: float
    adjusted: bool
    liquid_cash: float
    ledger_cash: float
    cash_drift: float
    total_bankroll: float
    checked_at: str
    developer_name: str = DEVELOPER_CREDIT


class TaxCertificateResponse(BaseModel):
    user_id: str
    financial_year: str
    gross_winnings: float
    total_tds_withheld: float
    cess_amount: float
    effective_rate_pct: float
    total_winning_tickets: int
    jurisdiction: str = "IN"
    section: str = "115BBJ"
    issued_at: str
    developer_name: str = DEVELOPER_CREDIT
