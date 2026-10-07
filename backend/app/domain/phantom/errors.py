"""Domain errors for PHANTOM."""

from typing import Any


class PhantomDomainError(Exception):
    """Raised when GARUDA rejects an input or cannot complete a calculation."""

    def __init__(self, message: str = "[GARUDA]: Calculation rejected.", **context: Any) -> None:
        self.message = message
        self.context = context
        super().__init__(message)
