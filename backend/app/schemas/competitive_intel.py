"""Pydantic V2 contracts for Competitive Intelligence (FA-7)."""

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
