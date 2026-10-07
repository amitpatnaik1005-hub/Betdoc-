"""Domain errors for THE VAULT - CFO Advisory."""

from uuid import UUID


class CfoDomainError(Exception):
    """Raised when TODAR MAL rejects or cannot complete a financial operation."""

    def __init__(self, message: str = "[TODAR MAL]: The request could not be processed.") -> None:
        self.message = message
        super().__init__(message)


class AlertNotFoundError(CfoDomainError):
    """Raised when an alert does not exist for the requesting user."""

    def __init__(self, alert_id: UUID) -> None:
        self.alert_id = alert_id
        super().__init__(f"Alert {alert_id} not found.")
