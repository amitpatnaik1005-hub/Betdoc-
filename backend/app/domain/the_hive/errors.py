import uuid


class HiveDomainError(Exception):
    """Base class for every Hive domain failure."""

    def __init__(self, message: str, *, task_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.task_id = task_id


class TaskNotFoundError(HiveDomainError):
    """The referenced task does not exist."""


class TaskAlreadyClaimedError(HiveDomainError):
    """CAS conflict: the task was claimed, reserved or modified by another bot."""


class DependencyCycleDetectedError(HiveDomainError):
    """Adding the dependency would introduce a cycle into the task DAG."""


class TaskExpiredError(HiveDomainError):
    """The task passed its expires_at deadline."""


class InvalidTaskStateError(HiveDomainError):
    """The task's current state does not permit the requested transition."""
