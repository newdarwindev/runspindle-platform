#!/usr/bin/env python3
"""Build and seal a pinned image in a reserved, isolated preparation guest."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import Any

from tools.runner_fleet import backend as _backend
from tools.runner_fleet import model as _model
from tools.runner_fleet import state as _state
from tools.runner_fleet.identity import binding
from tools.runner_fleet.retention import capture_bytes, capture_file
from tools.runner_fleet.staging import verify as verify_staged

# Existing process/state helpers exchange dynamic JSON records. Their boundary
# remains explicit; new staged closure and identity validation stay fully typed.
Backend: Any = _backend.Backend
ROOT = _backend.ROOT
digest: Any = _backend.digest
execute: Any = _backend.execute
helper: Any = _backend.helper
SOURCE = _model.ROOT
State: Any = _state.State
config: Any = _model.config


def require_build_capacity(root: Path, reserve_gib: int) -> dict[str, int]:
    # Slot overlay + sealed candidate + independent retained guest snapshot.
    # Eight GiB also cover incremental receipts and preparation growth.
    required = reserve_gib + 3 * _backend.GUEST_DISK_GIB + 8
    available = shutil.disk_usage(root).free
    if available < required * 1024**3:
        raise SystemExit(
            f"Image build needs {required} GiB including reserve and retention"
        )
    return {"available_bytes": available, "required_gib": required}


def require_build_runtime(
    memory_gib: int,
    reserve_gib: int,
    capacity: int,
    meminfo: Path = Path("/proc/meminfo"),
) -> dict[str, int]:
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise SystemExit("Existing principal lacks KVM access; no privilege fallback")
    available = next(
        (
            int(line.split()[1]) * 1024
            for line in meminfo.read_text().splitlines()
            if line.startswith("MemAvailable:")
        ),
        0,
    )
    required = (reserve_gib + memory_gib * capacity) * 1024**3
    if available < required:
        raise SystemExit("Insufficient memory including configured host reserve")
    return {"available_bytes": available, "required_bytes": required}


def current_reservation(state: Any, resource: dict[str, Any]) -> dict[str, Any]:
    matching = [
        row
        for row in state.active()
        if row["id"] == resource["id"] and row["slot"] == resource["slot"]
    ]
    if len(matching) != 1:
        raise RuntimeError("Build reservation is missing or ambiguous; no cleanup")
    current: dict[str, Any] = matching[0]
    return current


def require_build_storage(root: Path, state_directory: Path) -> dict[str, int]:
    if not root.is_mount() or not state_directory.is_mount():
        raise SystemExit("Approved dedicated fleet and state mounts are required")
    device = root.stat().st_dev
    if state_directory.stat().st_dev != device or device == Path("/").stat().st_dev:
        raise SystemExit(
            "Fleet/state must share dedicated storage; no stale root fallback"
        )
    return {"device": device}


def verify_build_source(source_sha: str, source_binding: dict[str, str]) -> None:
    observed_sha = subprocess.check_output(
        ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    ).strip()
    if observed_sha != source_sha or dirty or binding(SOURCE) != source_binding:
        raise RuntimeError(
            "Build source drift; guest/artifacts retained without qualification"
        )


def main():
    cfg = config()
    require_build_capacity(ROOT, cfg["hosts"]["strix"]["minimum_disk_gib"])
    require_build_runtime(
        cfg["lanes"]["strix-linux-x64"]["memory_gib"],
        cfg["hosts"]["strix"]["reserve_memory_gib"],
        cfg["hosts"]["strix"]["capacity"],
    )
    state_directory = Path.home() / ".local/state/runspindle-fleet"
    require_build_storage(ROOT, state_directory)
    state = State(state_directory)
    pins = json.loads((SOURCE / "fleet/image-inputs.json").read_text())
    source_sha = subprocess.check_output(
        ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True
    ).strip()
    if subprocess.check_output(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    ).strip():
        raise SystemExit("Image preparation requires a clean exact source checkout")
    source_binding = binding(SOURCE)
    download = state.directory / "downloads/ubuntu.img"
    download.parent.mkdir(mode=0o700, exist_ok=True)
    with state.lock():
        if state.active():
            raise SystemExit("Existing reservations must be reconciled first")
        if not download.exists():
            execute(
                [
                    "curl",
                    "-fL",
                    "--retry",
                    "2",
                    pins["ubuntu"]["url"],
                    "-o",
                    str(download),
                ],
                timeout=600,
            )
        if digest(download) != pins["ubuntu"]["sha256"]:
            raise SystemExit("Pinned Ubuntu checksum mismatch; no guest booted")
        verify_staged(SOURCE, download.parent)
        base = ROOT / "images/ubuntu-base.qcow2"
        if (
            base.is_symlink()
            or not base.is_file()
            or base.stat().st_uid != 0
            or base.stat().st_mode & 0o222
            or digest(base) != pins["ubuntu"]["sha256"]
        ):
            raise SystemExit(
                "Existing sealed pinned Ubuntu base required; no file replaced"
            )
        resource = state.reserve({"repository": "runspindle-platform", "id": -2})
        backend = Backend(state, base, preparation_source_sha=source_sha)
        try:
            backend.create(resource, prepare=True)
            deadline = time.monotonic() + 240
            while not backend.ready(resource):
                if time.monotonic() > deadline:
                    raise RuntimeError("Preparation boot timeout")
                time.sleep(2)
            backend.isolation(resource, check_tooling=False)
            staged = [
                "requirements-quality.txt",
                "quality-node/package.json",
                "quality-node/package-lock.json",
                "quality-node-modules.tar.gz",
                "actions-runner-linux-x64-2.338.2.tar.gz",
                "rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz",
                "node.tar.xz",
                pins["temporal"]["filename"],
                "LICENSE-CLI",
                "core-source.tar.gz",
                "host-source.tar.gz",
                "cargo-closure.tar.gz",
                "builder-closure.tar.gz",
            ]
            staged.extend(
                "quality-wheels/" + row["filename"]
                for row in json.loads(
                    (SOURCE / pins["quality"]["closure"]).read_text()
                )["wheels"]
            )
            for relative in staged:
                backend.copy_prepared(resource, download.parent / relative, relative)
            closure = pins["offline_rust_dependencies"]
            exports = "".join(
                f"export {name}={shlex.quote(closure[key])}\n"
                for name, key in (
                    ("RUNSPINDLE_CORE_SHA", "core"),
                    ("RUNSPINDLE_HOST_SHA", "host"),
                )
            )
            verify_build_source(source_sha, source_binding)
            preparation_script = _backend.frozen_preparation_text(
                SOURCE, source_sha, "tools/runner_fleet/prepare_image.sh"
            )
            result = backend.ssh(
                resource,
                exports + preparation_script,
                timeout=3600,
                check=False,
                retain_nonsecret_output=True,
            )
            log = capture_bytes(
                backend.directory(resource),
                "image-build.log",
                (result.stdout + result.stderr).encode(),
            )
            manifests = [
                line.removeprefix("RUNSPINDLE_IMAGE=")
                for line in result.stdout.splitlines()
                if line.startswith("RUNSPINDLE_IMAGE=")
            ]
            if len(manifests) != 1 or result.returncode not in (0, 255):
                raise RuntimeError(
                    "Image build did not emit one terminal package/license manifest"
                )
            manifest = json.loads(manifests[0])
            verify_build_source(source_sha, source_binding)
            deadline = time.monotonic() + 60
            while helper("observe", resource["slot"]).get("ActiveState") in (
                "active",
                "activating",
            ):
                if time.monotonic() > deadline:
                    raise RuntimeError("Image preparation did not shut down")
                time.sleep(2)
            helper("stop", resource["slot"], resource["id"])
            candidate = ROOT / "images" / f"candidate-{source_sha[:12]}.qcow2"
            if candidate.exists():
                raise RuntimeError("Exact-source candidate image already exists")
            execute(
                [
                    "sudo",
                    "-n",
                    "qemu-img",
                    "convert",
                    "-O",
                    "qcow2",
                    str(backend.slot(resource) / "disk.qcow2"),
                    str(candidate),
                ],
                timeout=600,
            )
            execute(["sudo", "-n", "chown", "root:root", str(candidate)])
            execute(["sudo", "-n", "chmod", "0444", str(candidate)])
            if candidate.stat().st_uid != 0 or candidate.stat().st_mode & 0o222:
                raise RuntimeError("Candidate image was not sealed read-only")
            manifest = {
                "sha256": digest(candidate),
                "candidate_path": str(candidate),
                "inputs": pins,
                "guest": manifest,
                "build_log_sha256": digest(log),
                "source_sha": source_sha,
                **source_binding,
            }
            verify_build_source(source_sha, source_binding)
            path = state.directory / "candidate.json"
            if path.exists() or path.is_symlink():
                capture_file(path, state.directory, "candidate-before-build.json")
            capture_bytes(
                state.directory,
                f"candidate-{source_sha}.json",
                json.dumps(manifest, indent=2).encode(),
            )
            path.write_text(json.dumps(manifest, indent=2))
            resource = current_reservation(state, resource)
            backend.destroy(resource)
            state.update(
                resource,
                "destroyed",
                cleanup_verified=True,
                built_image_sha256=manifest["sha256"],
            )
            print(
                json.dumps(
                    {
                        "status": "candidate",
                        "image_sha256": manifest["sha256"],
                        "qualification": "pending",
                    }
                )
            )
        except BaseException as error:
            state.update(resource, "uncertain", error=str(error))
            raise


if __name__ == "__main__":
    main()
