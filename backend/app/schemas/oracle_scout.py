"""Pydantic V2 contracts for the Scout Oracle (ASHOKA)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.oracle_scout.manager import MAX_MESSAGE_LENGTH, MAX_PAGE_CONTEXT_LENGTH


class ScoutSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ScoutChatRequest(ScoutSchema):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    page_context: str | None = Field(default=None, max_length=MAX_PAGE_CONTEXT_LENGTH)
    user_message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    user_id: UUID | None = None  # Accepted for compatibility only; the router uses the bearer token's user


class ScoutAction(ScoutSchema):
    """A clickable card under an answer (Group 100): it opens a BetDoc page."""

    type: Literal["LOAD_PARLAY", "NAVIGATE", "OPEN_CONSULTANT", "VIEW_PREDICTION"]
    label: str
    route: str = Field(pattern=r"^/([a-z0-9-][a-z0-9/-]*)?$")  # an in-app path, never a URL or a //host


class ScoutChatResponse(ScoutSchema):
    response_text: str
    history_id: UUID
    # Group 100, additive: the cards, and which engine answered (the guardrail's refusal, the local model, or the
    # rule engine on the live book)
    actions: list[ScoutAction] = Field(default_factory=list)
    source: Literal["guardrail", "ollama", "quant_nlg"] = "quant_nlg"


class ScoutHistoryRead(ScoutSchema):
    id: UUID
    user_id: UUID | None
    page_context: str | None
    user_message: str
    oracle_response: str
    created_at: datetime
