"""Background daemon bridging the Kafka event stream to Bayesian inference.



Exposes :class:`BetDocDaemon`, its configuration object, and the CPU-bound

inference entrypoint that executes inside the process pool.

"""



from __future__ import annotations

from betdoc.infrastructure.daemon.worker import (
    BetDocDaemon,
    DaemonConfig,
    DeadLetterQueue,
    run_inference_job,
)

__all__ = [

    "BetDocDaemon",

    "DaemonConfig",

    "DeadLetterQueue",

    "run_inference_job",

]
