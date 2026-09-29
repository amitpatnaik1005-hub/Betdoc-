import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.the_lab import ExperimentStatus, ResearchStatus


class _ReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _WriteSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- research
class ResearchCreate(_WriteSchema):
    category: str = Field(min_length=1, max_length=50)
    topic: str = Field(min_length=1, max_length=255)


class ResearchRead(_ReadSchema):
    id: uuid.UUID
    category: str
    topic: str
    status: ResearchStatus
    markdown_content: str | None
    created_at: datetime
    completed_at: datetime | None


# ------------------------------------------------------------- experiments
class ExperimentCreate(_WriteSchema):
    name: str = Field(min_length=1, max_length=255)
    hypothesis: str = Field(min_length=1, max_length=10_000)
    model_a_name: str = Field(min_length=1, max_length=100)
    model_b_name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _models_must_differ(self) -> Self:
        if self.model_a_name.casefold() == self.model_b_name.casefold():
            raise ValueError("model_a_name and model_b_name must be different models")
        return self


class ExperimentConclude(_WriteSchema):
    winner: str = Field(min_length=1, max_length=100)
    metrics: dict[str, Any]


class ExperimentRead(_ReadSchema):
    id: uuid.UUID
    name: str
    hypothesis: str
    model_a_name: str
    model_b_name: str
    status: ExperimentStatus
    winner: str | None
    metrics: dict[str, Any] | None
    created_at: datetime
    concluded_at: datetime | None


# ------------------------------------------------------------------ health
class ApiHealthStatus(_ReadSchema):
    source_name: str
    status: Literal["ONLINE", "DEGRADED", "OFFLINE"]
    latency_ms: int = Field(ge=0)
    last_checked: datetime
