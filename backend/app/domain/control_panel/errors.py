"""Domain errors for the Control Panel."""


class ControlPanelDomainError(Exception):
    """Raised when the Control Panel rejects or cannot complete an operation."""

    def __init__(self, message: str = "CONTROL could not process the request.") -> None:
        self.message = message
        super().__init__(message)
