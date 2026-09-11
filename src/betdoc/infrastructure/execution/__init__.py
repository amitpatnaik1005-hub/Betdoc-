
"""Order execution layer for BetDoc.



Defines the bookmaker-agnostic execution contract (:class:`ExecutionClient`),

the normalised result envelope (:class:`ExecutionResult`), the execution

status taxonomy (:class:`ExecutionStatus`), and :class:`ExecutionError`.

"""



from __future__ import annotations



from betdoc.infrastructure.execution.client import (

    ExecutionClient,

    ExecutionError,

    ExecutionResult,

    ExecutionStatus,

)



__all__ = [

    "ExecutionClient",

    "ExecutionError",

    "ExecutionResult",

    "ExecutionStatus",

]


