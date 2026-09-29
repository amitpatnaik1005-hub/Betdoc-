"""Domain errors for Oracle Competitive Picks."""

class CompetitiveIntelDomainError(Exception):
    """Base error for the Competitive Intel domain."""

    def __init__(self, message: str = "SPY-BOT could not process the request.") -> None:
        self.message = message
        super().__init__(message)


class SiteNotFoundError(CompetitiveIntelDomainError):
    """Raised when a requested competitor site is not tracked."""

    def __init__(self, site_name: str, supported: tuple[str, ...]) -> None:
        self.site_name = site_name
        self.supported = supported
        super().__init__(f"Site '{site_name}' is not tracked. Supported sites: {', '.join(supported)}")
