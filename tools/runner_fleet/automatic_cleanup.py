"""Source-only owned cleanup: no flattened backup, data deletion or base GC.

Requires reviewed installed helper (stopped slot becomes operator-private) and
canonical manager state. No run-loop/entrypoint invokes this module currently.
Interrupted moves retain capacity for explicit exact-intent recovery; this is not
automatic or reconstructive repair. All completed records continue protecting their backing base.
"""

from __future__ import annotations

import re
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import backend
from .candidate import time_budget
from .manager import gh, pages
from .model import REPOSITORIES
from .original_retention import (
    inspect_linked_disk,
    retain_originals,
    resume_originals,
    verify_backing_binding,
)


@contextmanager
def cleanup_signals() -> Iterator[None]:
    previous = {
        s: signal.signal(s, signal.SIG_IGN)
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def base_references(state: Any) -> dict[str, str]:
    """Inventory automatic-owner base references, including released slots."""
    result = {}
    for resource in state.all_resources():
        details = resource["details"]
        if details.get("automatic_fleet_v1") is not True:
            continue
        path, sha = details.get("image_path"), details.get("image_sha256")
        if (
            type(path) is not str
            or not Path(path).is_absolute()
            or Path(path).resolve() != Path(path)
            or type(sha) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
        ):
            raise ValueError("Unknown backing reference; all image GC blocked")
        if path in result and result[path] != sha:
            raise ValueError("Conflicting immutable base references")
        result[path] = sha
    return result


def assert_unreferenced_image(state: Any, path: Path) -> None:
    # Legacy/probe retention has a different schema. Never silently assume it
    # carries no backing references; a separate reviewed inventory is required.
    if any(
        r["details"].get("automatic_fleet_v1") is not True
        for r in state.all_resources()
    ):
        raise ValueError("Legacy backing inventory unresolved; all GC blocked")
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Noncanonical image query; GC blocked")
    if str(path) in base_references(state):
        raise ValueError("Image retained by canonical resource record")


def stop_owned(resource: dict[str, Any]) -> None:
    backend.helper("stop", resource["slot"], resource["id"])
    observed = backend.helper("observe", resource["slot"])
    if (
        observed.get("resource_id") != resource["id"]
        or observed.get("ActiveState") not in ("inactive", "failed")
        or int(observed.get("MainPID", -1)) != 0
    ):
        raise ValueError("Owned guest termination uncertain")


def remove_owned(resource: dict[str, Any]) -> None:
    path = "repos/newdarwindev/" + resource["repository"] + "/actions/runners"
    for runner in pages(path, "runners"):
        if runner["name"] == resource["runner_name"]:
            gh(path + "/" + str(int(runner["id"])), "DELETE")
    if any(r["name"] == resource["runner_name"] for r in pages(path, "runners")):
        raise ValueError("Owned registration remains")


class AutomaticCleanup:
    def __init__(
        self,
        state: Any,
        *,
        root: Path = backend.ROOT,
        stop: Callable[[dict[str, Any]], None] = stop_owned,
        remove: Callable[[dict[str, Any]], None] = remove_owned,
    ):
        self.state, self.root, self.stop, self.remove = state, root, stop, remove

    def finish(self, identity: str) -> dict[str, Any]:
        return self._finish(identity, recover=False)

    def recover(self, identity: str) -> dict[str, Any]:
        """Reconcile only the exact canonical persisted no-copy intent."""
        return self._finish(identity, recover=True)

    def _finish(self, identity: str, *, recover: bool) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("Exact resource identity required")
        with self.state.lock(), cleanup_signals():
            resources = [r for r in self.state.active() if r["id"] == identity]
            if (
                len(resources) != 1
                or resources[0]["details"].get("automatic_fleet_v1") is not True
            ):
                raise ValueError("Exact automatic owner required")
            resource = resources[0]
            if (
                resource.get("repository") not in REPOSITORIES
                or resource.get("runner_name") != "runspindle-fleet-" + identity
                or type(resource.get("slot")) is not int
                or resource["slot"] != 0
            ):
                raise ValueError("Corrupt owned resource binding")
            errors = []
            try:
                self.state.update(resource, "cleaning")
            except BaseException as error:
                errors.append("state:" + type(error).__name__)
            # Stop and deregistration remain independent even if state write fails.
            for name, operation in [("stop", self.stop), ("registration", self.remove)]:
                try:
                    with time_budget(60):
                        operation(resource)
                except BaseException as error:
                    errors.append(name + ":" + type(error).__name__)
            if errors:
                self.state.update(resource, "uncertain", cleanup_error_types=errors)
                return {"status": "uncertain", "errors": errors}
            try:
                with time_budget(240):
                    details = resource["details"]
                    if ("original_retention" in details) != recover:
                        raise ValueError(
                            "Explicit recovery must match existing retention intent"
                        )
                    base = Path(details["image_path"])
                    base_references(self.state)
                    slot = self.root / "slots" / str(resource["slot"])
                    directory = self.state.directory / "resources" / identity
                    files = {
                        "disk": slot / "disk.qcow2",
                        "seed": slot / "seed.iso",
                        "serial": slot / "serial.log",
                        "key": directory / "key",
                    }

                    def persist(phase: str, receipt: dict[str, Any]) -> None:
                        self.state.update(
                            resource,
                            "retaining" if phase == "intent" else "retained_stopped",
                            original_retention=receipt,
                            retention_phase=phase,
                        )

                    if recover:
                        receipt = details["original_retention"]
                        binding = receipt["backing"]
                        if (
                            binding.get("base_path") != str(base)
                            or binding.get("base_sha256") != details["image_sha256"]
                        ):
                            raise ValueError(
                                "Recovery base differs from canonical reservation"
                            )
                        receipt = resume_originals(
                            files, self.root / "retained" / identity, receipt, persist
                        )
                    else:
                        binding = inspect_linked_disk(
                            files["disk"],
                            base,
                            details["image_sha256"],
                            lambda argv: backend.execute(argv, timeout=60, check=False),
                        )
                        receipt = retain_originals(
                            files, self.root / "retained" / identity, binding, persist
                        )

                    if any(slot.iterdir()):
                        raise ValueError("Unexpected slot files remain")
                    verify_backing_binding(binding)
                    # Record remains in the same table after releasing the slot.
                    self.state.update(
                        resource,
                        "destroyed",
                        cleanup_verified=True,
                        original_retention=receipt,
                        retention_phase="complete",
                    )
                    return {
                        "status": "retained",
                        "resource": identity,
                        "base_protected": True,
                    }
            except BaseException as error:
                self.state.update(
                    resource, "uncertain", cleanup_error_types=[type(error).__name__]
                )
                return {"status": "uncertain", "errors": [type(error).__name__]}
