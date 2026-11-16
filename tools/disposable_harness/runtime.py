"""Bounded injected clock, transport, processes and namespace doubles.

No socket, container, system service or actual namespace API is available here.
The temporary directory is real; all endpoints and runtime resources are fake.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.disposable_harness.contracts import require


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        require(0 <= seconds <= 60, "clock bound")
        self.now += seconds


class DisposableRun:
    def __init__(
        self,
        run_id: str,
        clock: Clock,
        *,
        deadline: float = 10,
        parent: Path | None = None,
    ) -> None:
        require(run_id.isalnum() and len(run_id) <= 40, "invalid run ID")
        require(0 < deadline <= 60, "deadline bound")
        self.run_id, self.clock = run_id, clock
        self.parent = parent
        self.end = clock.now + deadline
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self._root: Path | None = None
        self.processes: set[str] = set()
        self.services: set[str] = set()
        self.volumes: set[str] = set()
        self.endpoints: dict[str, Callable[[bytes], tuple[int, bytes]]] = {}
        self.closed = False
        self.outbound_denied = 0

    def __enter__(self) -> DisposableRun:
        require(self.directory is None and not self.closed, "run cannot be reused")
        self.directory = tempfile.TemporaryDirectory(
            prefix="runspindle-synthetic-", dir=self.parent
        )
        self._root = Path(self.directory.name)
        self.volumes.add("temporary")
        return self

    @property
    def root(self) -> Path:
        if self._root is None:
            raise ValueError("run has not started")
        return self._root

    def budget(self) -> None:
        require(not self.closed and self._root is not None, "inactive run")
        if self.clock.now >= self.end:
            raise TimeoutError("synthetic run deadline")

    def register(self, name: str, handler: Callable[[bytes], tuple[int, bytes]]) -> str:
        self.budget()
        require(name.isalnum() and len(self.endpoints) < 8, "endpoint resource bound")
        endpoint = f"fixture://{self.run_id}/{name}"
        require(endpoint not in self.endpoints, "duplicate endpoint")
        self.endpoints[endpoint] = handler
        self.processes.add(name)
        self.services.add(name)
        return endpoint

    def request(self, endpoint: str, payload: bytes) -> tuple[int, bytes]:
        self.budget()
        require(len(payload) <= 65536, "payload resource bound")
        if endpoint not in self.endpoints:
            self.outbound_denied += 1
            raise PermissionError("unexpected outbound request denied")
        result = self.endpoints[endpoint](payload)
        self.budget()
        require(len(result[1]) <= 65536, "response resource bound")
        return result

    def ready(self, probe: Callable[[], bool], *, timeout: float = 2) -> None:
        require(0 < timeout <= 10, "readiness deadline bound")
        end = min(self.end, self.clock.now + timeout)
        while self.clock.now < end:
            self.budget()
            if probe():
                self.budget()
                return
            self.clock.advance(0.25)
        raise TimeoutError("synthetic readiness deadline")

    def close(self) -> None:
        self.endpoints.clear()
        self.processes.clear()
        self.services.clear()
        self.volumes.clear()
        if self.directory is not None:
            self.directory.cleanup()
        self.closed = True
        require(
            self._root is None or not self.root.exists(),
            "temporary volume cleanup incomplete",
        )

    def __exit__(self, *args: Any) -> None:
        self.close()


class DependencyDouble:
    MODES = {"healthy", "unavailable", "slow", "malformed", "auth_denied", "tls_denied"}

    def __init__(self, clock: Clock, mode: str) -> None:
        require(mode in self.MODES, "unknown dependency fault")
        self.clock, self.mode = clock, mode

    def __call__(self, payload: bytes) -> tuple[int, bytes]:
        if self.mode == "unavailable":
            raise ConnectionError("synthetic backend unavailable")
        if self.mode == "slow":
            self.clock.advance(11)
            raise TimeoutError("synthetic response deadline")
        if self.mode == "tls_denied":
            raise PermissionError("synthetic certificate validation denied")
        if self.mode == "auth_denied":
            return 401, b'{"error":"synthetic_denial"}'
        if self.mode == "malformed":
            return 200, b"{malformed"
        return 200, b'{"ready":true}'
