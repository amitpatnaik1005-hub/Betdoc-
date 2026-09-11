
"""Settlement ingestion and end-of-day reconciliation domain services.



Couples inbound bookmaker settlement callbacks (:class:`CallbackHandler`,

:class:`SettlementEvent`) with the end-of-day cross-check of the immutable

paper ledger against live bookmaker funds (:class:`EndOfDayAuditor`,

:class:`AuditReport`).

"""



from __future__ import annotations



from betdoc.domain.reconciliation.eod_auditor import (

    AuditReport,

    BookmakerAuditEntry,

    EndOfDayAuditor,

    ReconciliationStatus,

)

from betdoc.domain.reconciliation.settlement import (

    CallbackHandler,

    SettlementError,

    SettlementEvent,

    SettlementOutcome,

)



__all__ = [

    "AuditReport",

    "BookmakerAuditEntry",

    "CallbackHandler",

    "EndOfDayAuditor",

    "ReconciliationStatus",

    "SettlementError",

    "SettlementEvent",

    "SettlementOutcome",

]


