"""Asynchronous inference daemon.



Responsibility: consume ``match_state_updates``, convert each payload into a

strictly-typed Polars frame, run Bayesian inference, and commit the Kafka

offset only once that has succeeded.



Three constraints dominate the design.



**The consumer loop must never block.** aiokafka drives heartbeats from the

same event loop as the application code. ``max.poll.interval.ms`` defaults to

five minutes, but a single blocking call longer than the heartbeat interval

still risks the broker declaring this member dead and rebalancing the

partition to another consumer, which then reprocesses everything since the

last commit. So no CPU-bound work runs on the loop, ever.



**PyMC holds the GIL.** ``asyncio.to_thread`` is *not* sufficient for NUTS.

PyTensor releases the GIL inside individual compiled ops, but the sampler's

per-draw control flow, tuning logic and trace bookkeeping are Python-level,

so a threaded sampler still starves the loop for seconds at a time. Inference

therefore runs in a :class:`concurrent.futures.ProcessPoolExecutor`.



**Nothing stateful can cross the process boundary.** A live ``pm.Model`` holds

PyTensor graphs and compiled C modules that do not pickle, and

``InferenceData`` pickles poorly and enormously. The boundary is therefore

narrow and boring by design: Arrow IPC bytes and a plain dict go in, a plain

``dict[str, float]`` comes out. The child rebuilds its own engine and loads

its posterior from an on-disk artifact.

"""



from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

__all__ = ["BetDocDaemon", "DaemonConfig", "DeadLetterQueue", "run_inference_job"]



_LOG: Final[logging.Logger] = logging.getLogger("betdoc.daemon")



# Budget for any single awaited step on the consumer loop. Exceeding it means

# CPU work has leaked back onto the loop and is logged as a hard defect.

LOOP_BUDGET_SECONDS: Final[float] = 0.005



DEFAULT_TOPIC: Final[str] = "match_state_updates"

DEFAULT_GROUP: Final[str] = "betdoc-inference"





@dataclass(frozen=True, slots=True)

class DaemonConfig:

    """Runtime configuration, resolved from the environment."""



    bootstrap_servers: str = "redpanda:29092"

    topic: str = DEFAULT_TOPIC

    group_id: str = DEFAULT_GROUP

    dlq_path: Path = Path("var/dlq/match_state_updates.dlq.jsonl")

    artifact_dir: Path = Path("var/artifacts")

    inference_workers: int = 2

    inference_timeout_seconds: float = 300.0

    max_poll_records: int = 32

    session_timeout_ms: int = 45_000

    heartbeat_interval_ms: int = 3_000

    # 10 minutes: a cold PyMC fit legitimately exceeds the 5-minute default,

    # and being evicted mid-fit costs the whole sampling run.

    max_poll_interval_ms: int = 600_000

    shutdown_timeout_seconds: float = 45.0

    log_level: str = "INFO"



    @classmethod

    def from_env(cls, env: Mapping[str, str] | None = None) -> DaemonConfig:

        """Build configuration from environment variables.



        Raises

        ------

        ValueError

            If a numeric variable is present but unparseable. Failing at

            startup is strictly better than silently falling back to a

            default that changes the delivery semantics.

        """

        source = os.environ if env is None else env



        def integer(key: str, default: int) -> int:

            raw = source.get(key)

            if raw is None or raw == "":

                return default

            try:

                return int(raw)

            except ValueError as error:

                raise ValueError(f"{key} must be an integer, got {raw!r}") from error



        def number(key: str, default: float) -> float:

            raw = source.get(key)

            if raw is None or raw == "":

                return default

            try:

                return float(raw)

            except ValueError as error:

                raise ValueError(f"{key} must be numeric, got {raw!r}") from error



        return cls(

            bootstrap_servers=source.get("BETDOC_KAFKA_BOOTSTRAP", "redpanda:29092"),

            topic=source.get("BETDOC_KAFKA_TOPIC", DEFAULT_TOPIC),

            group_id=source.get("BETDOC_KAFKA_GROUP", DEFAULT_GROUP),

            dlq_path=Path(

                source.get(

                    "BETDOC_DLQ_PATH", "var/dlq/match_state_updates.dlq.jsonl"

                )

            ),

            artifact_dir=Path(source.get("BETDOC_ARTIFACT_DIR", "var/artifacts")),

            inference_workers=integer("BETDOC_INFERENCE_WORKERS", 2),

            inference_timeout_seconds=number("BETDOC_INFERENCE_TIMEOUT", 300.0),

            max_poll_records=integer("BETDOC_MAX_POLL_RECORDS", 32),

            shutdown_timeout_seconds=number("BETDOC_SHUTDOWN_TIMEOUT", 45.0),

            log_level=source.get("BETDOC_LOG_LEVEL", "INFO").upper(),

        )





class DeadLetterQueue:

    """Append-only JSONL sink for messages that cannot be processed.



    Poison messages are the normal case, not the exception: a single upstream

    schema change can make every record in a partition unparseable. Writing

    them aside and advancing the offset keeps the consumer live; crashing

    would produce an infinite restart loop that never drains the partition and

    blocks every well-formed message behind it.

    """



    __slots__ = ("_lock", "_path", "_written")



    def __init__(self, path: Path) -> None:

        self._path: Path = Path(path)

        self._lock: asyncio.Lock = asyncio.Lock()

        self._written: int = 0

        self._path.parent.mkdir(parents=True, exist_ok=True)



    @property

    def path(self) -> Path:

        """Filesystem location of the dead letter log."""

        return self._path



    @property

    def written(self) -> int:

        """Count of records quarantined during this process lifetime."""

        return self._written



    async def publish(

        self,

        raw_value: bytes | None,

        *,

        reason: str,

        topic: str,

        partition: int,

        offset: int,

        key: bytes | None = None,

    ) -> None:

        """Quarantine one record with enough context to replay it later."""

        record: dict[str, Any] = {

            "quarantined_at": datetime.now(tz=UTC).isoformat(),

            "reason": reason,

            "topic": topic,

            "partition": partition,

            "offset": offset,

            "key": key.decode("utf-8", errors="replace") if key else None,

            "payload": (

                raw_value.decode("utf-8", errors="replace") if raw_value else None

            ),

        }

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))



        async with self._lock:

            # Offloaded: a synchronous write to a full or slow disk would

            # block the consumer loop for far longer than the 5ms budget.

            await asyncio.to_thread(self._append, line)

            self._written += 1



        _LOG.warning(

            "quarantined %s[%d]@%d to DLQ: %s", topic, partition, offset, reason

        )



    def _append(self, line: str) -> None:

        """Blocking append with an fsync, executed on a worker thread."""

        with self._path.open("a", encoding="utf-8") as stream:

            stream.write(line + "\n")

            stream.flush()

            os.fsync(stream.fileno())


# ==============================================================================

# Child-process side.


# Everything below runs in a ProcessPoolExecutor worker, never on the event

# loop. Module-level state is per-child and is initialised exactly once by

# _initialise_worker, so the cost of importing PyMC and compiling PyTensor

# graphs is paid once per child rather than once per message.

# ==============================================================================



_ENGINE: Any = None

_ARTIFACT_DIR: Path | None = None





class InferenceJobError(RuntimeError):

    """Error raised inside a pool worker and sent back to the parent.



    Deliberately a single-argument exception. Richer domain errors such as

    ``ModelUpdateError`` carry keyword-only fields and ``__slots__``, and

    ``BaseException`` pickling only round-trips ``args`` — the parent would

    receive a ``TypeError`` from the unpickler instead of the real failure.

    Flattening to a message string here means the parent always learns what

    actually went wrong.

    """





def _initialise_worker(artifact_dir: str, log_level: str) -> None:

    """One-time setup for a pool worker process.



    Heavy scientific imports happen here rather than at module import time so

    that the parent daemon process never loads PyMC, PyTensor or ArviZ. The

    parent stays small, starts fast, and cannot be destabilised by a PyTensor

    compile lock.

    """

    global _ENGINE, _ARTIFACT_DIR



    # Each child is one sampling process. BLAS threading inside it would

    # oversubscribe the container's CPU quota; these mirror the Dockerfile.

    for variable in (

        "OMP_NUM_THREADS",

        "OPENBLAS_NUM_THREADS",

        "MKL_NUM_THREADS",

        "NUMEXPR_NUM_THREADS",

    ):

        os.environ[variable] = "1"



    # SIGINT/SIGTERM are handled exclusively by the parent's asyncio handlers.

    # A child that also traps them would raise KeyboardInterrupt mid-sample and

    # return a corrupt trace instead of letting the parent drain cleanly.

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    signal.signal(signal.SIGTERM, signal.SIG_DFL)



    logging.basicConfig(

        level=getattr(logging, log_level, logging.INFO),

        format="%(asctime)s %(levelname)s [worker:%(process)d] %(name)s: %(message)s",

        stream=sys.stderr,

    )



    from betdoc.domain.modeling.inference_engine import InferenceEngine



    _ARTIFACT_DIR = Path(artifact_dir)

    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    _ENGINE = InferenceEngine()



    _LOG.info("inference worker %d initialised", os.getpid())





def _artifact_paths(archetype_value: str) -> tuple[Path, Path]:

    """Return the ``(posterior, index)`` artifact paths for an archetype."""

    if _ARTIFACT_DIR is None:

        raise InferenceJobError("worker was not initialised; artifact dir is unset")

    safe = archetype_value.lower()

    return (

        _ARTIFACT_DIR / f"{safe}.posterior.nc",

        _ARTIFACT_DIR / f"{safe}.index.json",

    )





def _persist_posterior(model: Any, archetype_value: str) -> None:

    """Write a fitted posterior and its participant index to disk.



    NetCDF is ArviZ's native format and round-trips coordinates and sample

    stats faithfully. Pickling ``InferenceData`` would be smaller to write and

    far more fragile to read: it embeds xarray internals and breaks across

    library upgrades.

    """

    posterior_path, index_path = _artifact_paths(archetype_value)

    tmp_posterior = posterior_path.with_suffix(".nc.tmp")



    model.posterior.to_netcdf(str(tmp_posterior))

    os.replace(tmp_posterior, posterior_path)




    index = {name: position for position, name in enumerate(model.participants)}

    index_path.write_text(

        json.dumps({"participants": list(model.participants), "index": index}),

        encoding="utf-8",

    )





def _restore_posterior(model: Any, archetype_value: str) -> bool:

    """Load a persisted posterior back into an untrained archetype.



    Returns ``False`` when no artifact exists yet.



    KNOWN WART, flagged rather than hidden: this assigns to ``_idata`` and

    ``_index`` on the Group 9 archetype, which are private. There is currently

    no public restore API on ``BayesianArchetype``. The correct fix is a

    ``load_posterior(path, index)`` classmethod in

    ``betdoc.domain.modeling.archetypes.base``; until that exists this

    coupling is real and will break if those attribute names change.

    """

    posterior_path, index_path = _artifact_paths(archetype_value)

    if not posterior_path.is_file() or not index_path.is_file():

        return False



    import arviz as az



    idata = az.from_netcdf(str(posterior_path))
    sidecar = json.loads(index_path.read_text(encoding="utf-8"))
    index = {name: int(code) for name, code in sidecar["index"].items()}

    if hasattr(model, 'load_posterior'):
        model.load_posterior(idata, index)
    else:
        model._idata = idata
        model._index = index
    return True





def run_inference_job(frame_ipc: bytes, spec: Mapping[str, Any]) -> dict[str, Any]:

    """Execute one CPU-bound inference job inside a pool worker.



    Parameters

    ----------

    frame_ipc:

        The match-state frame serialised as Arrow IPC. Arrow is used rather

        than pickle because it is zero-copy on read, version-stable, and

        cannot execute code on deserialisation.

    spec:

        Plain JSON-safe job description: ``archetype``, ``action``

        (``train`` or ``predict``), and ``params`` for the prediction call.



    Returns

    -------

    dict[str, Any]

        Scalar results only. No model objects cross back over the boundary.

    """

    if _ENGINE is None:

        raise InferenceJobError("inference worker was not initialised")



    from betdoc.domain.modeling.types import ModelUpdateError, SportArchetype



    started = time.perf_counter()

    try:

        archetype = SportArchetype(str(spec["archetype"]))

        action = str(spec.get("action", "predict"))

        frame = pl.read_ipc(io.BytesIO(frame_ipc))



        model = _ENGINE.archetype_for(archetype)



        if action == "train":

            asyncio.run(_ENGINE.train_sport(archetype, frame))

            _persist_posterior(model, archetype.value)

            diagnostics = model.diagnostics

            payload: dict[str, Any] = {

                "action": "train",

                "archetype": archetype.value,

                "rows": frame.height,

            }

            if diagnostics is not None:

                payload.update(diagnostics.to_dict())

            payload["duration_seconds"] = time.perf_counter() - started

            return payload



        if action == "predict":

            if not model.is_trained and not _restore_posterior(model, archetype.value):

                raise InferenceJobError(

                    f"no posterior available for {archetype.value}; "

                    f"train it before requesting a price"

                )

            params = dict(spec.get("params") or {})

            result = _ENGINE.predict(archetype, **params)

            return {

                "action": "predict",

                "archetype": archetype.value,

                "duration_seconds": time.perf_counter() - started,

                **{key: float(value) for key, value in result.items()},

            }



        raise InferenceJobError(f"unsupported action {action!r}")



    except ModelUpdateError as error:

        raise InferenceJobError(f"{error.code}: {error}") from None

    except InferenceJobError:

        raise

    except Exception as error:

        raise InferenceJobError(f"{type(error).__name__}: {error}") from None





# ==============================================================================

# Parent-process side: the daemon.

# ==============================================================================





@dataclass(slots=True)

class _Metrics:

    """In-process counters surfaced on shutdown and by the health endpoint."""



    consumed: int = 0

    processed: int = 0

    quarantined: int = 0

    retried: int = 0

    loop_budget_breaches: int = 0

    last_offset_committed: int = -1

    started_at: float = field(default_factory=time.monotonic)



    def snapshot(self) -> dict[str, float]:

        """Return a flat, loggable view of the counters."""

        return {

            "consumed": float(self.consumed),

            "processed": float(self.processed),

            "quarantined": float(self.quarantined),

            "retried": float(self.retried),

            "loop_budget_breaches": float(self.loop_budget_breaches),

            "last_offset_committed": float(self.last_offset_committed),

            "uptime_seconds": time.monotonic() - self.started_at,

        }





class BetDocDaemon:

    """Kafka-to-inference bridge with at-least-once delivery semantics."""



    def __init__(

        self,

        config: DaemonConfig | None = None,

        *,

        max_inference_retries: int = 3,

    ) -> None:

        self._config: DaemonConfig = config or DaemonConfig.from_env()

        self._dlq: DeadLetterQueue = DeadLetterQueue(self._config.dlq_path)

        self._metrics: _Metrics = _Metrics()

        self._stop: asyncio.Event = asyncio.Event()

        self._consumer: Any = None

        self._executor: ProcessPoolExecutor | None = None

        self._max_retries: int = max_inference_retries

        self._shutting_down: bool = False



    @property

    def config(self) -> DaemonConfig:

        """Active configuration."""

        return self._config



    @property

    def metrics(self) -> dict[str, float]:

        """Current counter snapshot."""

        return self._metrics.snapshot()



    async def start(self) -> None:

        """Boot the daemon and block until shutdown completes."""

        logging.basicConfig(

            level=getattr(logging, self._config.log_level, logging.INFO),

            format="%(asctime)s %(levelname)s [daemon] %(name)s: %(message)s",

            stream=sys.stdout,

        )

        _LOG.info(

            "starting daemon: topic=%s group=%s brokers=%s workers=%d",

            self._config.topic,

            self._config.group_id,

            self._config.bootstrap_servers,

            self._config.inference_workers,

        )



        self._install_signal_handlers()

        self._executor = self._create_executor()

        self._consumer = await self._create_consumer()



        try:

            await self.run_loop()

        finally:

            await self.graceful_shutdown()



    def _install_signal_handlers(self) -> None:

        """Trap SIGINT and SIGTERM on the event loop.



        ``loop.add_signal_handler`` is used rather than ``signal.signal``

        because the latter runs the handler on an arbitrary stack between

        bytecodes, where awaiting an offset commit is not possible. The loop

        variant schedules a normal coroutine, so shutdown can finish the

        in-flight message and commit before exiting.

        """

        loop = asyncio.get_running_loop()

        for signal_number in (signal.SIGINT, signal.SIGTERM):

            try:

                loop.add_signal_handler(

                    signal_number,

                    lambda number=signal_number: self._request_stop(number),

                )

            except NotImplementedError:

                # Non-POSIX platform; fall back to the default disposition.

                _LOG.warning(

                    "loop signal handlers unavailable for %s on this platform",

                    signal_number,

                )



    def _request_stop(self, signal_number: int) -> None:

        """Signal callback: ask the loop to wind down."""

        name = signal.Signals(signal_number).name

        if self._stop.is_set():

            _LOG.warning("%s received again; shutdown already in progress", name)

            return

        _LOG.info("%s received; draining current message then stopping", name)

        self._stop.set()



    def _create_executor(self) -> ProcessPoolExecutor:

        """Create the inference pool using the ``spawn`` start method.



        ``fork`` is unsafe here and the failure is nondeterministic: forking a

        process that already holds an asyncio epoll fd, OpenBLAS thread pool

        and PyTensor compile lock duplicates all of them into the child, where

        they deadlock on first use. ``spawn`` pays a slower startup once per

        child in exchange for a clean interpreter.

        """

        context = mp.get_context("spawn")

        return ProcessPoolExecutor(

            max_workers=max(1, self._config.inference_workers),

            mp_context=context,

            initializer=_initialise_worker,

            initargs=(str(self._config.artifact_dir), self._config.log_level),

        )



    async def _create_consumer(self) -> Any:

        """Construct and start the Kafka consumer.



        aiokafka is imported lazily so that pool workers, which re-import this

        module under ``spawn``, do not pay for a Kafka client they never use.

        """

        from aiokafka import AIOKafkaConsumer



        consumer = AIOKafkaConsumer(

            self._config.topic,

            bootstrap_servers=self._config.bootstrap_servers,

            group_id=self._config.group_id,

            # At-least-once requires manual commits. With auto-commit the

            # broker advances the offset on a timer, so a crash mid-inference

            # silently drops the message: the offset says done, the work never

            # happened, and no alert fires.

            enable_auto_commit=False,

            auto_offset_reset="earliest",

            max_poll_records=self._config.max_poll_records,

            session_timeout_ms=self._config.session_timeout_ms,

            heartbeat_interval_ms=self._config.heartbeat_interval_ms,

            max_poll_interval_ms=self._config.max_poll_interval_ms,

            # Raw bytes: deserialisation is done explicitly so a malformed

            # payload becomes a DLQ entry instead of an exception thrown from

            # inside the consumer's internal fetcher task.

            value_deserializer=None,

            key_deserializer=None,

        )

        await consumer.start()

        _LOG.info("consumer started; assigned partitions: %s", consumer.assignment())

        return consumer



    async def run_loop(self) -> None:

        """Main consume/infer/commit loop."""

        consumer = self._consumer

        if consumer is None:

            raise RuntimeError("run_loop called before the consumer was created")



        while not self._stop.is_set():

            batches = await consumer.getmany(

                timeout_ms=500, max_records=self._config.max_poll_records

            )

            if not batches:

                continue



            for topic_partition, records in batches.items():

                for record in records:

                    if self._stop.is_set():

                        _LOG.info(

                            "stop requested mid-batch; %s will be reprocessed "

                            "after restart",

                            topic_partition,

                        )

                        return



                    self._metrics.consumed += 1

                    committed = await self._handle_record(record)

                    if committed:

                        await self._commit(topic_partition, record.offset)



    async def _commit(self, topic_partition: Any, offset: int) -> None:

        """Commit ``offset + 1`` for a single partition.



        Committing per record rather than per batch bounds duplicate

        reprocessing after an unclean stop to exactly one message. The cost is

        one round trip per message, which is negligible next to a PyMC fit.

        """

        consumer = self._consumer

        if consumer is None:

            return

        from aiokafka.structs import OffsetAndMetadata



        started = time.perf_counter()

        try:

            await consumer.commit({topic_partition: OffsetAndMetadata(offset + 1, "")})

            self._metrics.last_offset_committed = offset

        except Exception as error:

            # A failed commit means the message will be redelivered. The work

            # was idempotent at the model level, so redelivery is safe and a

            # warning is the correct response, not a crash.

            _LOG.warning("offset commit failed for %s@%d: %s", topic_partition, offset, error)

        self._observe_budget("commit", time.perf_counter() - started)



    async def _handle_record(self, record: Any) -> bool:

        """Process one record. Returns whether its offset may be committed.



        Returning ``True`` for a poison message is intentional: the offset

        advances past a payload that will never succeed, and the payload is

        preserved in the DLQ for replay. Returning ``False`` leaves the offset

        where it is so a transient failure is retried after restart.

        """

        try:

            payload = json.loads(record.value.decode("utf-8"))

        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as error:

            await self._quarantine(record, f"undecodable payload: {error}")

            return True



        if not isinstance(payload, dict):

            await self._quarantine(record, "payload is not a JSON object")

            return True



        try:

            frame, spec = self._build_job(payload)

        except (KeyError, TypeError, ValueError) as error:

            await self._quarantine(record, f"schema violation: {error}")

            return True



        frame_ipc = frame.write_ipc(None).getvalue()



        for attempt in range(1, self._max_retries + 1):

            try:

                result = await self._dispatch(frame_ipc, spec)

            except InferenceJobError as error:

                # Modelling failures are deterministic: retrying an identical

                # divergent fit yields an identical divergent fit. Quarantine

                # immediately rather than burning the retry budget.

                await self._quarantine(record, f"inference rejected: {error}")

                return True

            except TimeoutError:

                _LOG.warning(

                    "inference timed out after %.0fs (attempt %d/%d)",

                    self._config.inference_timeout_seconds,

                    attempt,

                    self._max_retries,

                )

            except Exception as error:

                _LOG.warning(

                    "inference dispatch failed (attempt %d/%d): %s",

                    attempt,

                    self._max_retries,

                    error,

                )

            else:

                self._metrics.processed += 1

                _LOG.info(

                    "processed %s[%d]@%d archetype=%s in %.2fs",

                    record.topic,

                    record.partition,

                    record.offset,

                    spec["archetype"],

                    float(result.get("duration_seconds", 0.0)),

                )

                return True



            self._metrics.retried += 1

            if attempt < self._max_retries:

                await asyncio.sleep(min(2.0**attempt, 10.0))



        await self._quarantine(

            record, f"inference failed after {self._max_retries} attempts"

        )

        return True



    async def _dispatch(

        self, frame_ipc: bytes, spec: Mapping[str, Any]

    ) -> dict[str, Any]:

        """Run inference in the process pool under a hard timeout."""

        if self._executor is None:

            raise RuntimeError("executor is not running")



        loop = asyncio.get_running_loop()

        started = time.perf_counter()

        future = loop.run_in_executor(

            self._executor, run_inference_job, frame_ipc, dict(spec)

        )

        try:

            result = await asyncio.wait_for(

                future, timeout=self._config.inference_timeout_seconds

            )

        finally:

            # The await itself must be near-instant; the CPU work happens in

            # the child. A breach here means work leaked onto the loop.

            self._observe_budget("dispatch_await", 0.0)

            _LOG.debug("dispatch wall time %.2fs", time.perf_counter() - started)

        return result



    def _build_job(

        self, payload: Mapping[str, Any]

    ) -> tuple[pl.DataFrame, dict[str, Any]]:

        """Convert a state update into a typed Polars frame plus a job spec.



        The frame is constructed with an explicit schema rather than inferred.

        Inference on a single-row dict would type ``score_a`` as ``Int64`` on

        one message and ``Float64`` on the next (whenever a feed emits ``1.0``

        instead of ``1``), and the archetype's strict cast would then reject

        one of them for no visible reason.

        """

        from betdoc.domain.modeling.types import MATCH_SCHEMA, SportArchetype



        rows = payload.get("history")

        if rows is None:

            rows = [payload]

        if not isinstance(rows, list) or not rows:

            raise ValueError("history must be a non-empty list when present")



        sport = payload.get("sport")

        archetype = (

            SportArchetype(str(payload["archetype"]))

            if payload.get("archetype")

            else SportArchetype.for_sport(str(sport))

        )



        normalised: list[dict[str, Any]] = []

        for row in rows:

            if not isinstance(row, dict):

                raise TypeError("every history entry must be a JSON object")

            normalised.append(

                {

                    "match_id": str(row["match_id"]),

                    "timestamp": self._parse_timestamp(row["timestamp"]),

                    "participant_a": str(row["participant_a"]),

                    "participant_b": str(row["participant_b"]),

                    "score_a": int(row["score_a"]),

                    "score_b": int(row["score_b"]),

                }

            )



        frame = pl.DataFrame(normalised, schema=MATCH_SCHEMA, strict=True)



        spec: dict[str, Any] = {

            "archetype": archetype.value,

            "action": str(payload.get("action", "predict")),

            "params": dict(payload.get("params") or {}),

        }

        return frame, spec



    @staticmethod

    def _parse_timestamp(raw: Any) -> datetime:

        """Parse an ISO-8601 or epoch timestamp into tz-aware UTC."""

        if isinstance(raw, (int, float)):

            return datetime.fromtimestamp(float(raw), tz=UTC)

        text = str(raw).strip().replace("Z", "+00:00")

        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:

            return parsed.replace(tzinfo=UTC)

        return parsed.astimezone(UTC)



    async def _quarantine(self, record: Any, reason: str) -> None:

        """Route a record to the DLQ and bump the counter."""

        await self._dlq.publish(

            record.value,

            reason=reason,

            topic=record.topic,

            partition=record.partition,

            offset=record.offset,

            key=record.key,

        )

        self._metrics.quarantined += 1



    def _observe_budget(self, stage: str, elapsed: float) -> None:

        """Record a loop-budget breach.



        Anything that holds the loop beyond ~5ms delays aiokafka's heartbeat

        task. Sustained breaches end in a rebalance and duplicate processing,

        so they are counted and logged rather than left to be discovered from

        consumer-group churn.

        """

        if elapsed > LOOP_BUDGET_SECONDS:

            self._metrics.loop_budget_breaches += 1

            _LOG.warning(

                "loop budget breached in %s: %.1fms > %.1fms",

                stage,

                elapsed * 1000.0,

                LOOP_BUDGET_SECONDS * 1000.0,

            )



    async def graceful_shutdown(self) -> None:

        """Stop the consumer and pool, releasing every resource exactly once."""

        if self._shutting_down:

            return

        self._shutting_down = True

        self._stop.set()

        _LOG.info("shutting down: %s", self._metrics.snapshot())



        if self._consumer is not None:

            with contextlib.suppress(Exception):

                # stop() leaves the group cleanly, which triggers an immediate

                # rebalance instead of making the other members wait out the

                # full session timeout.

                await asyncio.wait_for(

                    self._consumer.stop(),

                    timeout=self._config.shutdown_timeout_seconds / 2.0,

                )

            self._consumer = None



        if self._executor is not None:

            # wait=True lets an in-flight sample finish; cancel_futures=True

            # discards work that never started. Killing a running child would

            # orphan its PyTensor compile lock in the shared compiledir and

            # stall the next container start.

            await asyncio.to_thread(

                self._executor.shutdown, wait=True, cancel_futures=True

            )

            self._executor = None



        _LOG.info(

            "shutdown complete; %d record(s) quarantined at %s",

            self._dlq.written,

            self._dlq.path,

        )





def main() -> int:

    """Console entrypoint: ``python -m betdoc.infrastructure.daemon.worker``."""

    # Must be set before any pool is created, and force=True because a parent

    # import may already have selected fork.

    with contextlib.suppress(RuntimeError):

        mp.set_start_method("spawn", force=True)



    try:

        config = DaemonConfig.from_env()

    except ValueError as error:

        print(f"invalid daemon configuration: {error}", file=sys.stderr)

        return 2



    daemon = BetDocDaemon(config)

    try:

        asyncio.run(daemon.start())

    except KeyboardInterrupt:

        # Reachable only if a signal arrives before handlers are installed.

        _LOG.info("interrupted before signal handlers were ready")

        return 130

    return 0





if __name__ == "__main__":

    raise SystemExit(main())
