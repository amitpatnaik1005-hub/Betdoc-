"""Request and response bodies of the street-smart synthesizer, the system splitter and the daily market health (Group 115).
Money crosses the wire as decimal strings (the vault keeps exact decimals). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
DeskBook = Literal["parimatch", "1xbet"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class StreetSmartSynthesizeRequest(_In):
    target_stake_inr: float = Field(default=250.0, gt=0, le=1e6)
    target_return_inr: float = Field(default=5000.0, gt=0, le=1e8)
    max_legs: int | None = Field(default=None, ge=2, le=20, description="Omitted: SYNTH_MAX_LEGS (the Oracle settles at most 20 legs)")
    preferred_sports: list[str] | None = Field(default=None, max_length=10, description="Sport labels: Football, Cricket, Tennis, Basketball ...")
    bookmaker: DeskBook = "parimatch"

    @model_validator(mode="after")
    def _a_return(self) -> StreetSmartSynthesizeRequest:
        if self.target_return_inr <= self.target_stake_inr:
            raise ValueError("the target return must exceed the stake")
        return self


class SystemSplitRequest(_In):
    leg_ids: list[str] = Field(min_length=5, max_length=20, description="The legs to split, by id (a synthesized slip's), priced again now")
    total_session_stake_inr: float = Field(default=250.0, gt=0, le=1e6)
    bookmaker: DeskBook = "parimatch"


class SynthesizedLegOut(BaseModel):
    leg_id: str
    fixture_id: str
    fixture: str
    home: str
    away: str
    sport_key: str | None = None
    sport: str
    league: str | None = None
    kickoff: str | None = None
    market: str
    selection: str
    label: str
    book: str
    code: str
    odds: str
    fair_probability: float
    ev: float
    consensus_books: int
    prices: dict[str, float]
    recipe_rationale: str
    tier: Literal["ANCHOR", "RELAXED"] = "ANCHOR"  # Group 127: RELAXED legs filled a board short of anchors
    below_fair: bool = False  # the book pays less than the consensus says the leg is worth (EV < 0)


class StreetSmartSynthesizeResponse(BaseModel):
    book: str
    book_label: str
    legs: list[SynthesizedLegOut]
    leg_count: int
    total_odds: str
    target_stake_inr: str
    target_return_inr: str
    target_odds: str
    projected_return_inr: str
    joint_probability: float
    joint_ev: float
    expected_return_inr: str
    reached_target: bool
    shortfall: str | None = None
    sports_represented: list[str]
    is_multi_sport: bool
    diversification_relaxed: bool
    anchors_available: int
    anchor_tier: Literal["STRICT", "RELAXED"] = "STRICT"
    relaxed_leg_ids: list[str] = []
    relaxation_note: str | None = None
    rules: dict[str, Any]
    independence_note: str
    developer_name: str = DEVELOPER_CREDIT


class SplitSlipCard(BaseModel):
    tier: Literal["A", "B", "C"]
    tier_name: str
    tag: str
    leg_count: int
    stake_inr: str
    odds: str
    projected_payout_inr: str
    win_probability: float
    ev: float
    covers_session_stake: bool
    legs: list[dict[str, Any]]
    ticket_reference: str | None = None


class SystemSplitResponse(BaseModel):
    book: str
    book_label: str
    total_session_stake_inr: str
    slip_a_fortress: SplitSlipCard
    slip_b_growth: SplitSlipCard
    slip_c_moonshot: SplitSlipCard
    capital_shield_active: bool
    needed_slip_a_odds: str
    scenarios: list[dict[str, Any]]
    expected_session_return_inr: str
    recover_stake_probability: float
    single_ticket: dict[str, Any]
    formula_explanation: str
    developer_name: str = DEVELOPER_CREDIT


class RecordedSplitResponse(SystemSplitResponse):
    status: Literal["RECORDED"] = "RECORDED"
    ticket_references: list[str]
    message: str


class MarketHealthResponse(BaseModel):
    date: str
    health_score: int
    advisory_tier: Literal["REST_DAY", "SELECTIVE", "PRIME_HARVEST"]
    advisory_message: str
    should_bet: bool
    deductions: list[dict[str, Any]]
    unmeasured: list[str]
    active_fixtures_count: int
    top_tier_fixtures_count: int | None = None
    average_bookmaker_margin_pct: float | None = None
    rotation_risks_count: int | None = None
    anchors_count: int
    top_anchors: list[dict[str, Any]]
    dispatched_at: str | None = None
    dispatched_telegram: bool
    dispatched_sms: bool
    updated_at: str
    developer_name: str = DEVELOPER_CREDIT
