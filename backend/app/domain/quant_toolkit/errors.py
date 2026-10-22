"""Domain errors for the Lab's quant toolkit (Group 96; the retired Phantom section's engines)."""

from typing import Any


class QuantToolkitError(Exception):
    """Raised when the quant toolkit rejects an input or cannot complete a calculation."""

    def __init__(self, message: str = "[PANINI]: Calculation rejected.", **context: Any) -> None:
        self.message = message
        self.context = context
        super().__init__(message)
