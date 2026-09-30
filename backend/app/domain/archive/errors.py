"""Domain errors for The Archive."""


class ArchiveDomainError(Exception):
    """Base error for The Archive."""

    def __init__(self, message: str = "ARCHIVE could not process the request.") -> None:
        self.message = message
        super().__init__(message)


class TableNotFoundError(ArchiveDomainError):
    """Raised when a table is unknown, not materialised, or not browsable."""

    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        super().__init__(f"Table {table_name!r} is not available in the Archive.")
