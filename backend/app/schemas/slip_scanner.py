"""Bodies of the slip scanner (Group 126): a bookmaker's slip, as a screenshot, a photo or text, read, vetted and
re-calibrated. Developed for Amit Ashok Kumar Patnaik.

Money and odds cross the wire as decimal strings; probabilities as fractions. A figure BetDoc could not measure is null,
never a default.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
ScanBook = Literal["auto", "parimatch", "1xbet", "stake"]


class SlipVerdict(StrEnum):
    APPROVED = "APPROVED"  # every leg vetted, the Consultant's gold grade
    PARTIAL = "PARTIAL"  # what could be vetted reaches gold, but not the slip as printed (legs off the board, or vetted as a straight multiple)
    DECLINED = "DECLINED"  # the Consultant would not approve it as it is (a re-calibration follows when one scores higher)
    UNRATED = "UNRATED"  # legs were read, but none is on the live board to vet
    UNREADABLE = "UNREADABLE"  # no leg could be read


class LegStatus(StrEnum):
    MATCHED = "MATCHED"  # on the live board: priced, vetted
    NO_ODDS = "NO_ODDS"  # its price was not read
    FIXTURE_NOT_READ = "FIXTURE_NOT_READ"
    NOT_ON_BOARD = "NOT_ON_BOARD"  # no pre-match fixture on the board (started, beyond 72 hours, or not priced)
    MARKET_NOT_COVERED = "MARKET_NOT_COVERED"  # a market Ashoka does not price (props, corners, halves, team totals ...)
    MARKET_NOT_PRICED = "MARKET_NOT_PRICED"  # Ashoka's market, but not on the board for this fixture now


class ScanSlipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    image_base64: str | None = Field(default=None, description="A PNG/JPEG/WEBP screenshot or photo as base64 (a data: URI is accepted)")
    slip_text: str | None = Field(default=None, description="The slip's text, pasted or typed")
    bookmaker: ScanBook = Field(default="auto", description="auto: the book the slip names, else the best retail price")
    stake_inr: float | None = Field(default=None, gt=0, le=1e8, description="Omitted: the stake the slip shows, if any")

    @model_validator(mode="after")
    def _something(self) -> ScanSlipRequest:
        if bool(self.image_base64) == bool(self.slip_text):
            raise ValueError("send one of a screenshot (image_base64) or the slip's text (slip_text)")
        return self


class OcrLineOut(BaseModel):
    text: str
    confidence: float
    parsed: bool


class ScannedLeg(BaseModel):
    number: int
    status: LegStatus
    note: str
    fixture_read: str | None
    market_read: str
    selection_read: str
    odds_read: str | None
    confidence: float
    leg_id: str | None = None
    fixture: str | None = None
    sport_key: str | None = None
    kickoff: str | None = None
    market: str | None = None
    selection: str | None = None
    label: str | None = None
    fair_probability: float | None = None
    consensus_books: int | None = None
    ev_at_slip_odds: float | None = None
    book_price_now: str | None = None
    ev_at_book_price: float | None = None


class SlipFault(BaseModel):
    source: Literal["scanner", "doctor"]
    code: str
    severity: str
    legs: list[int]
    text: str


class ReCalibratedLeg(BaseModel):
    action: Literal["RETAINED", "SUBSTITUTED", "DROPPED"]
    leg_id: str | None
    fixture: str
    label: str
    price: str | None
    fair_probability: float | None
    ev: float | None
    replaces: str | None = None
    rationale: str


class ConsultantLeg(BaseModel):
    """A leg as the Bet-Slip store holds it, to open the slip in the Consultant."""

    leg_id: str
    fixture: str
    label: str
    market: str
    prices: dict[str, float]
    fair_probability: float


class ScanSlipResponse(BaseModel):
    developer_credit: str = DEVELOPER_CREDIT
    scan_id: str
    source: Literal["image", "text"]
    bookmaker: str | None
    skin: str
    ocr_lines: list[OcrLineOut]
    detected_booking_code: str | None
    detected_kind: str | None
    detected_stake_inr: str | None
    printed_total_odds: str | None
    printed_potential_return_inr: str | None
    odds_check: str | None
    legs_scanned: list[ScannedLeg]
    coverage: dict[str, int]
    total_scanned_odds: str | None
    stake_inr: str | None
    estimated_payout_inr: str | None
    joint_win_probability_pct: float | None
    overall_ev_pct: float | None
    vetting_score: float | None
    tier: str | None
    verdict: SlipVerdict
    verdict_status: str | None  # the Consultant's own: APPROVED_GOLD, APPROVED_WITH_CURE, REJECTED
    verdict_badge: str
    verdict_explanation: str
    bookmaker_margin_pct: float | None
    rating: dict[str, Any] | None
    faults_detected: list[SlipFault]
    is_recalibrated: bool
    recalibrated_legs: list[ReCalibratedLeg]
    recalibration_rounds: list[dict[str, Any]]
    recalibrated_score: float | None
    recalibrated_tier: str | None
    recalibrated_total_odds: str | None
    recalibrated_joint_win_prob_pct: float | None
    recalibrated_ev_pct: float | None
    recalibrated_projected_payout_inr: str | None
    recommended_kelly_stake_inr: str | None
    recalibration_summary: str | None
    consultant_legs: list[ConsultantLeg]
    recalibrated_consultant_legs: list[ConsultantLeg]
