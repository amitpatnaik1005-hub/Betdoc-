"""Domain errors for Multi-Sport Support."""


class SportsDomainError(Exception):
    """Base error. Messages are safe to return to clients."""

    def __init__(self, message: str = "The sports engine could not process the request.") -> None:
        self.message = message
        super().__init__(message)


class SportNotFoundError(SportsDomainError):
    def __init__(self, sport_name: str) -> None:
        self.sport_name = sport_name
        super().__init__(f"Sport {sport_name!r} is not supported.")


class SportInactiveError(SportsDomainError):
    def __init__(self, sport_name: str) -> None:
        self.sport_name = sport_name
        super().__init__(f"Sport {sport_name!r} is currently deactivated.")


class InvalidSportConfigError(SportsDomainError):
    """Raised when a submitted or stored configuration does not match its sport schema."""
