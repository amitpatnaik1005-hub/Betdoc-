"""Pydantic V2 contracts for the Scout Oracle (ASHOKA)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.oracle_scout.manager import MAX_MESSAGE_LENGTH, MAX_PAGE_CONTEXT_LENGTH


class ScoutSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ScoutChatRequest(ScoutSchema):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    page_context: str | None = Field(default=None, max_length=MAX_PAGE_CONTEXT_LENGTH)
    user_message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    user_id: UUID | None = None


class ScoutChatResponse(ScoutSchema):
    response_text: str
    history_id: UUID


class ScoutHistoryRead(ScoutSchema):
    id: UUID
    user_id: UUID | None
    page_context: str | None
    user_message: str
    oracle_response: str
    created_at: datetime
