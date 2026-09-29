import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.models.the_hive import BotStatus, HiveTaskModel, LegendaryBot, TaskStatus

JsonObject = dict[str, Any]


class _ReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _WriteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HeartbeatRequest(_WriteSchema):
    status: BotStatus = BotStatus.ONLINE
    uptime_seconds: int = Field(ge=0)
    resource_metrics: JsonObject = Field(default_factory=dict)


class BotProfileRead(_ReadSchema):
    id: uuid.UUID
    bot_name: LegendaryBot
    status: BotStatus
    uptime_seconds: int
    last_ping_at: datetime
    tasks_completed: int
    error_count: int
    resource_metrics: JsonObject


class TaskCreate(_WriteSchema):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=20_000)
    priority: int = Field(default=50, ge=0, le=100)
    max_retries: int = Field(default=0, ge=0, le=25)
    payload: JsonObject = Field(default_factory=dict)
    expires_at: AwareDatetime | None = None
    assignee_name: LegendaryBot | None = Field(default=None, description="Optional reservation for one bot")

    @field_validator("expires_at")
    @classmethod
    def _expiry_in_future(cls, value: datetime | None) -> datetime | None:
        if value is not None and value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


class DependencyCreate(_WriteSchema):
    parent_task_id: uuid.UUID


class ClaimRequest(_WriteSchema):
    bot_name: LegendaryBot


class CompleteRequest(_WriteSchema):
    bot_name: LegendaryBot
    result_payload: JsonObject = Field(default_factory=dict)


class FailRequest(_WriteSchema):
    bot_name: LegendaryBot
    error: JsonObject = Field(default_factory=dict)


class TaskRead(_ReadSchema):
    id: uuid.UUID
    title: str
    description: str
    assignee_name: LegendaryBot | None
    status: TaskStatus
    priority: int
    max_retries: int
    retry_count: int
    payload: JsonObject
    result_payload: JsonObject | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    parent_task_ids: list[uuid.UUID]
    child_task_ids: list[uuid.UUID]

    @classmethod
    def from_model(cls, task: HiveTaskModel) -> Self:
        """Requires parents/children eager-loaded (lazy='raise' otherwise)."""
        return cls(
            id=task.id,
            title=task.title,
            description=task.description,
            assignee_name=task.assignee_name,
            status=task.status,
            priority=task.priority,
            max_retries=task.max_retries,
            retry_count=task.retry_count,
            payload=task.payload,
            result_payload=task.result_payload,
            created_at=task.created_at,
            updated_at=task.updated_at,
            expires_at=task.expires_at,
            parent_task_ids=[parent.id for parent in task.parents],
            child_task_ids=[child.id for child in task.children],
        )


class FailureRead(BaseModel):
    task: TaskRead
    retried: bool
    cascaded_task_ids: list[uuid.UUID]


class LearningLogCreate(_WriteSchema):
    bot_name: LegendaryBot
    parameter_name: str = Field(min_length=1, max_length=255)
    old_value: Any = None
    new_value: Any
    reasoning: str = Field(min_length=1, max_length=20_000)
    confidence_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class LearningLogRead(_ReadSchema):
    id: uuid.UUID
    bot_name: LegendaryBot
    parameter_name: str
    old_value: Any
    new_value: Any
    reasoning: str
    confidence_score: float
    created_at: datetime


class TaskEventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    TASK_CLAIMED = "TASK_CLAIMED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_RETRY_SCHEDULED = "TASK_RETRY_SCHEDULED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CASCADE_FAILED = "TASK_CASCADE_FAILED"
    TASK_REVERTED = "TASK_REVERTED"
    TASK_EXPIRED = "TASK_EXPIRED"
    TASK_BLOCKED = "TASK_BLOCKED"
    DEPENDENCY_ADDED = "DEPENDENCY_ADDED"


class TaskEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: TaskEventType
    task_id: uuid.UUID
    status: TaskStatus
    assignee_name: LegendaryBot | None = None
    occurred_at: datetime
