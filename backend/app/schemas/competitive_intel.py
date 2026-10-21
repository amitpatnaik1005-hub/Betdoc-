"""Pydantic V2 contracts for Competitive Intelligence (FA-7) and the live market consensus (Group 85, KARNA & PANINI).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Analytics Fleet."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

BotStatusLiteral = Literal["IDLE", "SCANNING", "ERROR"]
GapSeverityLiteral = Literal["HIGH", "MEDIUM", "LOW"]


class CompetitiveIntelSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class BotRead(CompetitiveIntelSchema):
    id: UUID
    name: str
    target_site: str
    status: BotStatusLiteral
    last_scan_at: datetime | None


class DevSuggestionRead(CompetitiveIntelSchema):
    id: UUID
    gap_id: UUID
    suggestion_text: str
    priority: int = Field(ge=1, le=5)
    created_at: datetime


class FeatureGapRead(CompetitiveIntelSchema):
    id: UUID
    site_name: str
    missing_feature: str
    severity: GapSeverityLiteral
    is_resolved: bool
    created_at: datetime
    suggestions: list[DevSuggestionRead] = Field(default_factory=list)


class DashboardResponse(CompetitiveIntelSchema):
    bots: list[BotRead]
    gaps: list[FeatureGapRead]


class ScanResponse(CompetitiveIntelSchema):
    status: Literal["scan_complete"] = "scan_complete"


class MarkdownReportResponse(CompetitiveIntelSchema):
    markdown: str


# ---------------------------------------------------------------- Group 85: live market consensus
ConsensusVerdictLiteral = Literal["SHARP_CONTRARIAN", "PUBLIC_TRAP", "SHARP_CONSENSUS", "NEUTRAL"]
SignalVerdictLiteral = Literal["PUBLIC_TRAP", "SHARP_VERIFIED", "CAUTION_HIGH_VIG", "NEUTRAL"]
SelectionLiteral = Literal["HOME", "DRAW", "AWAY"]


class PublicConsensusSnapshotRead(CompetitiveIntelSchema):
    """One live selection: Parimatch and 1xBet against the sharp zero-vig price; shares only where measured."""

    id: UUID
    match_id: str
    sport: str
    sport_key: str | None
    league: str | None
    home_team: str
    away_team: str
    kickoff_at: datetime | None
    market_type: str
    selection: SelectionLiteral
    selection_label: str
    public_ticket_pct: float | None
    sharp_money_pct: float | None
    public_source: str | None
    divergence_pct: float | None
    rlm_detected: bool
    opening_odds: float | None
    parimatch_odds: float | None
    one_xbet_odds: float | None
    sharp_fair_odds: float | None
    parimatch_margin_pct: float | None
    one_xbet_margin_pct: float | None
    best_bookmaker: str
    best_odds: float
    betdoc_true_prob: float
    betdoc_ev_pct: float
    public_trap_flag: bool
    contrarian_edge_score: float
    verdict: ConsensusVerdictLiteral
    plain_english_summary: str
    recommended_stake_inr: float
    recommended_stake_usd: float | None
    source_provider: str
    captured_at: datetime


class BookmakerBenchmarkRead(CompetitiveIntelSchema):
    id: UUID
    bookmaker_key: str
    bookmaker_display: str
    sport: str
    market_type: str
    measured_overround_pct: float
    payout_efficiency_pct: float
    best_price_share_pct: float | None
    sample_size: int
    updated_at: datetime


class PublicTrapSignalRead(CompetitiveIntelSchema):
    id: UUID
    origin: Literal["DERIVED", "RECORDED"]
    source_site: str
    sport: str
    match_id: str
    match_title: str
    public_selection: SelectionLiteral
    public_pick: str
    public_backing_pct: float
    betdoc_verdict: SignalVerdictLiteral
    betdoc_recommended_pick: str
    best_bookmaker: str | None
    best_odds: float | None
    betdoc_ev_pct: float | None
    plain_english_rationale: str
    stake_inr: float
    stake_usd: float | None
    recorded_by: str | None
    created_at: datetime
    graded_at: datetime


class PublicPickIn(BaseModel):
    """A public pick read from a named outside source (an administrator types it; nothing is scraped)."""

    model_config = ConfigDict(extra="forbid")

    source_site: str = Field(min_length=2, max_length=96)
    match_id: str = Field(min_length=1, max_length=128)
    selection: SelectionLiteral
    public_backing_pct: float = Field(ge=0, le=100)


class LiveConsensusSyncResponse(CompetitiveIntelSchema):
    status: Literal["synced"] = "synced"
    snapshots_synced: int
    benchmarks_synced: int
    signals_synced: int
    fixtures_read: int
    usd_rate: float | None  # INR per USD used for the secondary figures; none when no fresh rate exists
    architect: str
