"""Domain errors for Oracle Popular Picks."""

from uuid import UUID


class PopularPicksDomainError(Exception):
    """Base error for the Popular Picks domain."""

    def __init__(self, message: str = "ASHOKA could not process the popular pick request.") -> None:
        self.message = message
        super().__init__(message)


class PopularPickNotFoundError(PopularPicksDomainError):
    """Raised when a popular parlay does not exist."""

    def __init__(self, parlay_id: UUID) -> None:
        self.parlay_id = parlay_id
        super().__init__(f"Popular parlay {parlay_id} not found.")
