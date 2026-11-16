"""Install-plan exercises using only dictionary filesystem/process/service doubles."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tools.disposable_harness.contracts import (
    TARGETS,
    canonical,
    digest,
    require,
    validate_inventory,
)


@dataclass
class FakeAdapters:
    files: dict[str, bytes] = field(default_factory=dict)
    processes: set[str] = field(default_factory=set)
    services: dict[str, str] = field(default_factory=dict)
    active: str | None = None
    migration: int = 0


def make_bundle(
    target: str, files: dict[str, bytes], *, version: str = "synthetic-1"
) -> dict[str, Any]:
    manifest = {
        "target": target,
        "version": version,
        "dependencies": {"fixture-runtime": "synthetic-v1"},
        "files": {name: digest(value) for name, value in files.items()},
        "sbom": {
            "components": ["fixture-runtime"],
            "license": "LicenseRef-Original-Synthetic-Fixture",
            "notice_sha256": digest(files["NOTICE.txt"]),
        },
        "evidence_class": "fake_install_plan",
    }
    return {
        "manifest": manifest,
        "checksum": digest(canonical(manifest)),
        "files": copy.deepcopy(files),
    }


class FakeInstaller:
    def __init__(
        self, target: str, dependencies: dict[str, str], adapters: FakeAdapters
    ) -> None:
        require(target in TARGETS, "unsupported fake target")
        self.target, self.dependencies, self.adapters = target, dependencies, adapters
        self.previous: FakeAdapters | None = None

    def validate(self, bundle: dict[str, Any]) -> dict[str, Any]:
        manifest = bundle["manifest"]
        require(
            digest(canonical(manifest)) == bundle["checksum"],
            "bundle manifest checksum",
        )
        require(manifest["target"] == self.target, "wrong architecture or OS")
        require(
            manifest["evidence_class"] == "fake_install_plan", "fake evidence boundary"
        )
        require(
            manifest["version"] in {"synthetic-1", "synthetic-2"},
            "unqualified bundle version",
        )
        require(
            manifest["dependencies"] == {"fixture-runtime": "synthetic-v1"},
            "unqualified dependencies",
        )
        require(
            all(
                self.dependencies.get(k) == v
                for k, v in manifest["dependencies"].items()
            ),
            "missing dependencies",
        )
        validate_inventory(bundle["files"], manifest["files"])
        require(
            manifest["sbom"]
            == {
                "components": ["fixture-runtime"],
                "license": "LicenseRef-Original-Synthetic-Fixture",
                "notice_sha256": digest(bundle["files"]["NOTICE.txt"]),
            },
            "SBOM/notice mismatch",
        )
        return dict(manifest)

    def restore(self, snapshot: FakeAdapters) -> None:
        self.adapters.files = copy.deepcopy(snapshot.files)
        self.adapters.processes = set(snapshot.processes)
        self.adapters.services = dict(snapshot.services)
        self.adapters.active = snapshot.active
        self.adapters.migration = snapshot.migration

    def install(
        self, bundle: dict[str, Any], checkpoint: Callable[[str], None] = lambda _: None
    ) -> None:
        manifest = self.validate(bundle)  # Admission must precede every fake effect.
        before = copy.deepcopy(self.adapters)
        try:
            checkpoint("intent")
            for name, content in sorted(bundle["files"].items()):
                self.adapters.files[name] = content
                checkpoint("file:" + name)
            checkpoint("migration")
            self.adapters.migration += 1
            manager = "launchd" if self.target.startswith("darwin/") else "systemd"
            self.adapters.services["synthetic"] = manager
            self.adapters.processes.add("synthetic")
            checkpoint("service")
            self.adapters.active = manifest["version"]
            checkpoint("activated")
            self.previous = before
        except BaseException:
            self.restore(before)
            raise

    def rollback(self) -> None:
        require(self.previous is not None, "no rollback state")
        if self.previous is not None:
            self.restore(self.previous)
        self.previous = None
