"""The Archive domain: database health, table inventory and read-only browsing."""

from app.domain.archive.errors import ArchiveDomainError, TableNotFoundError
from app.domain.archive.manager import ArchiveManager

__all__ = ["ArchiveDomainError", "ArchiveManager", "TableNotFoundError"]
