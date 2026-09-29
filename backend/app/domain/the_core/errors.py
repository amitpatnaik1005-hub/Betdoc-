"""Domain errors raised by The Core."""

from typing import Any
from uuid import UUID


class CoreDomainError(Exception):
    """Base error for every failure raised inside The Core."""

    default_message = "The Core encountered a domain error."

    def __init__(self, message: str | None = None, *, context: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.context: dict[str, Any] = dict(context or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        safe_context = {
            key: value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
            for key, value in self.context.items()
        }
        return {"error": type(self).__name__, "message": self.message, "context": safe_context}


class SmallcaseNotFoundError(CoreDomainError):
    default_message = "Smallcase not found in the Engine Room registry."

    def __init__(self, smallcase_id: UUID | str | None = None, message: str | None = None) -> None:
        self.smallcase_id = smallcase_id
        resolved = message or (
            f"Smallcase {smallcase_id} not found in the Engine Room registry." if smallcase_id else None
        )
        super().__init__(resolved, context={"smallcase_id": smallcase_id} if smallcase_id else None)


class InvalidSmallcaseStateError(CoreDomainError):
    default_message = "The Smallcase is not in a valid state for this operation."


class EngineConcurrencyError(CoreDomainError):
    default_message = "A concurrent mutation won the compare-and-swap race."
