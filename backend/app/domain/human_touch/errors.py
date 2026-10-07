"""Domain errors for Human Touch Mode (FA-8)."""

from uuid import UUID


class HumanTouchDomainError(Exception):
    """Raised for math violations or invalid Human Touch operations. Messages are safe to show clients."""

    def __init__(self, message: str = "Human Touch could not process the request.") -> None:
        self.message = message
        super().__init__(message)


class OverrideLogNotFoundError(HumanTouchDomainError):
    def __init__(self, log_id: UUID) -> None:
        self.log_id = log_id
        super().__init__(f"Override log {log_id} not found.")


class OverrideLogAlreadyResolvedError(HumanTouchDomainError):
    def __init__(self, log_id: UUID) -> None:
        self.log_id = log_id
        super().__init__(f"Override log {log_id} has already been resolved.")
