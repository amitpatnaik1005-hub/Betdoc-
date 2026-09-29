"""Domain errors for ASHOKA (The Scout Oracle)."""


class ScoutDomainError(Exception):
    """Raised when ASHOKA rejects a request."""

    def __init__(self, message: str = "ASHOKA could not process the request.") -> None:
        self.message = message
        super().__init__(message)
