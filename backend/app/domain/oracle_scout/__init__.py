"""Scout Oracle (ASHOKA) domain: context-aware sidebar Q&A."""

from app.domain.oracle_scout.errors import ScoutDomainError
from app.domain.oracle_scout.manager import OracleScoutManager

__all__ = ["OracleScoutManager", "ScoutDomainError"]
