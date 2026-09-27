from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class ResolveBetRequest(BaseModel):
    status: Literal["ACCEPTED", "REJECTED"]
    exchange_bet_id: str | None = Field(default=None, max_length=128)

    @field_validator("exchange_bet_id")
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        # Whitespace-only IDs must not satisfy the ACCEPTED requirement.
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def require_exchange_bet_id_when_accepted(self) -> "ResolveBetRequest":
        if self.status == "ACCEPTED" and not self.exchange_bet_id:
            raise ValueError("exchange_bet_id is required when status is ACCEPTED")
        return self


class ResolveBetResponse(BaseModel):
    bet_id: UUID
    status: str


class SweepResponse(BaseModel):
    swept_count: int
