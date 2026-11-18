"""Opt-in metadata-only lifecycle observations; never fleet admission authority."""

from collections import deque
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from threading import Lock
from time import time_ns
from typing import Any, Callable, cast

from .manager import Manager

VERSION = "runspindle.telemetry.v1"
CAPACITY = 128


class Operation(Enum):
    TICK = "platform.fleet.tick"
    CLEANUP = "platform.fleet.cleanup"
    RECONCILE = "platform.fleet.reconcile"


class Outcome(Enum):
    ACKNOWLEDGED = "acknowledged"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Observation:
    operation: Operation
    outcome: Outcome
    started_ns: int
    ended_ns: int


class Buffer:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: deque[Observation] = deque()

    def record(self, operation: Operation, outcome: Outcome, start: int) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if len(self._records) < CAPACITY:
                self._records.append(Observation(operation, outcome, start, time_ns()))
        finally:
            self._lock.release()

    def drain(self) -> tuple[Observation, ...]:
        if not self._lock.acquire(blocking=False):
            return ()
        try:
            result = tuple(self._records)
            self._records.clear()
            return result
        finally:
            self._lock.release()


class ObservedManager(Manager):
    """Explicit composition-root opt-in. Existing installed manager is unchanged.

    ACKNOWLEDGED means only the original method returned. It may have retained an
    uncertain resource; never infer cleanup success or permission to retry.
    """

    def __init__(
        self, cfg: Any, directory: Any, backend: Any = None, *, telemetry: Buffer
    ) -> None:
        if type(telemetry) is not Buffer:
            raise TypeError("invalid telemetry buffer")
        self.telemetry = telemetry
        cast(Callable[..., None], super().__init__)(cfg, directory, backend)

    def _observed(self, operation: Operation, call: Callable[[], Any]) -> Any:
        try:
            started = time_ns()
        except BaseException:
            return call()
        outcome = Outcome.UNKNOWN
        try:
            result = call()
            outcome = Outcome.ACKNOWLEDGED
            return result
        except Exception:
            outcome = Outcome.ERROR
            raise
        finally:
            try:
                Buffer.record(self.telemetry, operation, outcome, started)
            except BaseException:
                pass

    def tick(self, poll: bool = True) -> Any:
        original = cast(Callable[[bool], Any], super().tick)
        return self._observed(Operation.TICK, lambda: original(poll))

    def cleanup(self, resource: dict[str, Any], reason: str) -> None:
        original = super().cleanup
        self._observed(Operation.CLEANUP, lambda: original(resource, reason))

    def reconcile(self) -> Any:
        return self._observed(Operation.RECONCILE, super().reconcile)


def export(tracer: Any, observation: Observation) -> None:
    """Explicit off-path OpenTelemetry conversion, not called by Manager.

    Caller must supervise exporter total time/failure/shutdown and authorize its
    destination. No global provider, exporter, environment or baggage discovery.
    """
    Context = import_module("opentelemetry.context").Context

    if (
        type(observation) is not Observation
        or type(observation.operation) is not Operation
        or type(observation.outcome) is not Outcome
        or type(observation.started_ns) is not int
        or type(observation.ended_ns) is not int
        or observation.started_ns < 0
        or observation.ended_ns < 0
    ):
        raise ValueError("invalid telemetry observation")
    span = tracer.start_span(
        observation.operation.value,
        context=Context(),
        start_time=observation.started_ns,
        attributes={
            "runspindle.telemetry.version": VERSION,
            "runspindle.component": "platform",
            "runspindle.outcome": observation.outcome.value,
        },
    )
    span.end(end_time=max(observation.started_ns, observation.ended_ns))
