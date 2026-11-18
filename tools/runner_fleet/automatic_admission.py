"""Source-only automatic admission integration, deliberately without a run loop.

No installed entrypoint invokes this coordinator. Full cleanup/base lifetime and
restart qualification must be integrated before activation. Unknown resources
stay occupied; no fallback to old manager/generic labels/snapshot cleanup exists.
Dependencies below are trusted host adapters, never guest-supplied callbacks.
"""

from __future__ import annotations

from typing import Any, Callable

from .automatic_catalog import QualifiedRecord, load_catalog
from .automatic_policy import capacity_one
from .backend import Backend
from .manager import trusted


class AutomaticAdmission:
    def __init__(
        self,
        state: Any,
        capacity_probe: Callable[[], dict[str, int]],
        catalog: Callable[[], tuple[QualifiedRecord, ...]] = load_catalog,
        backend_factory: Callable[..., Any] = Backend,
    ):
        self.state, self.capacity_probe, self.catalog, self.backend_factory = (
            state,
            capacity_probe,
            catalog,
            backend_factory,
        )

    def admit(
        self,
        job: dict[str, Any],
        *,
        workflow_sha256: str,
        validation_manifest_sha256: str,
    ) -> dict[str, Any] | None:
        """Caller obtains both workflow/manifest hashes from exact GitHub head.

        This verifies requested work, not actual scheduler assignment. Catalog
        installation must audit authority of ALL eligible matching workflows;
        labels/pre-job hooks are not a security barrier.
        """
        with self.state.lock():
            if self.state.get("drained", True) or self.state.get("error"):
                return None
            repository = job.get("repository")
            if (
                job.get("status") != "queued"
                or type(job.get("id")) is not int
                or job["id"] <= 0
                or not trusted(job, "newdarwindev/" + str(repository))
            ):
                raise ValueError("Untrusted requested job")
            matches = [
                record
                for record in self.catalog()
                if record.route.matches(repository, job.get("labels"))
            ]
            if len(matches) != 1:
                raise ValueError("No exact qualified image route")
            record = matches[0]
            if (
                workflow_sha256 != record.workflow_sha256
                or validation_manifest_sha256 != record.route.validation_manifest_sha256
            ):
                raise ValueError("Requested workflow inputs differ")
            record.verify()
            observation = self.capacity_probe()
            if not capacity_one(occupied=len(self.state.active()), **observation):
                return None
            resource = self.state.reserve(
                job,
                single_strix=True,
                details={
                    "automatic_fleet_v1": True,
                    "image_sha256": record.route.image_sha256,
                    "image_path": str(record.image_path),
                    "manifest_path": str(record.manifest_path),
                    "manifest_sha256": record.manifest_sha256,
                    "evidence_sha256": record.evidence_sha256,
                    "route_labels": record.route.labels(),
                    "suite": record.route.suite,
                    "workflow_sha256": workflow_sha256,
                    "validation_manifest_sha256": validation_manifest_sha256,
                    "retention_allowance": observation["retention_allowance"],
                },
            )
            try:
                backend = self.backend_factory(
                    self.state, record.image_path, image_manifest=record.manifest_path
                )
                backend.create(resource, network_mode="verify")
            except BaseException as error:
                self.state.update(
                    resource, "uncertain", automatic_error=type(error).__name__
                )
                raise
            return resource

    def register_ready(self, resource: dict[str, Any], registration_token: str) -> None:
        """Exact ready/isolation gate; no generic labels or credential logging.

        A token must come from the matching authorized repository credential path.
        No token acquisition, retry, cleanup, service loop or default activation is
        provided here. Recovery remains blocked pending the separate reviewed lane.
        """
        with self.state.lock():
            if self.state.get("drained", True) or self.state.get("error"):
                raise ValueError("Admission drained before registration")
            active = [r for r in self.state.active() if r["id"] == resource["id"]]
            if len(active) != 1 or active[0]["phase"] != "starting":
                raise ValueError("Exact starting reservation required")
            current = active[0]
            details = current["details"]
            if details.get("automatic_fleet_v1") is not True:
                raise ValueError("Wrong lifecycle owner")
            matches = [
                r
                for r in self.catalog()
                if r.route.repository == current["repository"]
                and r.route.labels() == details.get("route_labels")
            ]
            if len(matches) != 1:
                raise ValueError("Catalog route disappeared")
            record = matches[0]
            record.verify()
            expected = {
                "image_path": str(record.image_path),
                "manifest_path": str(record.manifest_path),
                "manifest_sha256": record.manifest_sha256,
                "evidence_sha256": record.evidence_sha256,
            }
            if any(details.get(k) != v for k, v in expected.items()):
                raise ValueError("Reserved catalog binding changed")
            backend = self.backend_factory(
                self.state, record.image_path, image_manifest=record.manifest_path
            )
            if not backend.ready(current):
                raise ValueError("Guest not ready")
            backend.isolation(current)
            self.state.update(current, "registering")
            try:
                backend.register(
                    current,
                    "newdarwindev/" + current["repository"],
                    registration_token,
                    record.route.labels(),
                    no_default_labels=True,
                )
                self.state.update(current, "running")
            except BaseException as error:
                self.state.update(
                    current, "uncertain", automatic_error=type(error).__name__
                )
                raise
