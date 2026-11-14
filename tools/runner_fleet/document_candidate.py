"""Source-only, unqualified two-guest document candidate controller.

Plan mode only parses and prints JSON. Execution requires separate operator
authorization; it never drains/stops/restores the manager or registers a runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import quote

from . import backend as legacy
from . import state as legacy_state
from .backend import Backend as LegacyBackend
from .documents import load as load_documents
from .documents import verify as verify_documents
from .identity import binding
from .model import ROOT as SOURCE
from .supervision import UncertainProcess, alarm, supervise

Backend: Any = legacy.Backend
State: Any = legacy_state.State
execute: Any = legacy.execute
helper: Any = legacy.helper
digest: Any = legacy.digest
ROOT = legacy.ROOT
PHASES = {
    "verify": 300,
    "prepare": 1500,
    "seal": 600,
    "prepare-cleanup": 300,
    "validate": 900,
    "validate-cleanup": 300,
    "final": 300,
}
TOTAL = 4200
SOURCE_NAMES = (
    "root",
    "core",
    "host",
    "browser",
    "tether",
    "refinery",
    "console",
    "platform",
    "feedback-tether",
    "feedback-refinery",
)


def exact(value: object, width: int) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is not None
    )


def absolute(value: object) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("An explicit absolute path is required")
    path = Path(value)
    if str(path) != value or ".." in path.parts:
        raise ValueError("Noncanonical path refused")
    return path


def validate_plan(value: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "operation_id",
        "execution_source_sha",
        "binding",
        "state_directory",
        "downloads",
        "source_bundles",
        "sources",
        "base_image",
        "base_manifest",
        "base_sha256",
        "base_manifest_sha256",
        "helper_sha256",
        "host_python",
        "minimum_free_bytes",
        "protected",
        "network_rules_sha256",
        "document_closure_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Unexpected or missing plan fields")
    p = cast(dict[str, Any], value)
    if (
        type(p["schema_version"]) is not int
        or p["schema_version"] != 1
        or not exact(p["operation_id"], 32)
    ):
        raise ValueError("Invalid operation identity")
    if not exact(p["execution_source_sha"], 40):
        raise ValueError("Missing exact execution source")
    if (
        not isinstance(p["binding"], dict)
        or set(p["binding"])
        != {"controller_sha256", "recipe_sha256", "tool_closure_sha256"}
        or not all(exact(v, 64) for v in p["binding"].values())
    ):
        raise ValueError("Exact execution binding required")
    for field in (
        "base_sha256",
        "base_manifest_sha256",
        "helper_sha256",
        "network_rules_sha256",
        "document_closure_sha256",
    ):
        if not exact(p[field], 64):
            raise ValueError("Invalid immutable input hash")
    for field in (
        "state_directory",
        "downloads",
        "source_bundles",
        "base_image",
        "base_manifest",
    ):
        absolute(p[field])
    if (
        Path(p["base_image"]) != ROOT / "images/qualified.qcow2"
        or Path(p["base_manifest"]) != ROOT / "images/qualified.json"
    ):
        raise ValueError(
            "Only the exact existing qualified image can be an overlay base"
        )
    if p["host_python"] not in ("3.12.14", "3.14.8"):
        raise ValueError("Host interpreter must be explicitly reviewed")
    if (
        type(p["minimum_free_bytes"]) is not int
        or p["minimum_free_bytes"] < 56 * 1024**3
    ):
        raise ValueError("At least 56 GiB free is required for overlays and candidate")
    if not isinstance(p["protected"], dict) or not p["protected"]:
        raise ValueError("Existing image and candidate inventory must be approved")
    for name, sha in p["protected"].items():
        path = absolute(name)
        if not (
            path.is_relative_to(ROOT / "images")
            or path == Path(p["state_directory"]) / "candidate.json"
        ) or not exact(sha, 64):
            raise ValueError("Invalid protected inventory")
    if not isinstance(p["sources"], dict) or set(p["sources"]) != set(SOURCE_NAMES):
        raise ValueError("Exact root, seven components, and H10 sources required")
    for row in p["sources"].values():
        if (
            not isinstance(row, dict)
            or set(row) != {"commit", "sha256", "bytes"}
            or not exact(row["commit"], 40)
            or not exact(row["sha256"], 64)
            or type(row["bytes"]) is not int
            or not 1 <= row["bytes"] <= 1024**3
        ):
            raise ValueError("Invalid source bundle identity")
    return p


def paths(p: dict[str, Any]) -> tuple[Path, Path, Path]:
    operation = Path(p["state_directory"]) / "document-operations" / p["operation_id"]
    image = ROOT / "images" / ("document-" + p["operation_id"]) / "candidate.qcow2"
    return operation, image, operation / "candidate.json"


def plan_summary(p: dict[str, Any]) -> dict[str, Any]:
    operation, image, manifest = paths(p)
    return {
        "status": "plan_only",
        "qualification": "not_run",
        "operation_id": p["operation_id"],
        "phase_caps_seconds": PHASES,
        "overall_seconds": TOTAL,
        "maximum_guests_total": 2,
        "maximum_active_guests": 1,
        "operation_directory": str(operation),
        "candidate_image": str(image),
        "candidate_manifest": str(manifest),
        "downloads": False,
        "registration": False,
        "queue_admission": False,
        "promotion": False,
        "restoration": False,
        "github_checks": "not_executed",
    }


def regular(path: Path, sha: str, size: int | None = None) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Linked input refused")
    if (
        not path.is_file()
        or (size is not None and path.stat().st_size != size)
        or digest(path) != sha
    ):
        raise ValueError("Missing or changed pinned input")


def protected(p: dict[str, Any]) -> None:
    found = {
        str(path)
        for path in (ROOT / "images").rglob("*")
        if not path.is_dir() or path.is_symlink()
    }
    _, image, _ = paths(p)
    found.discard(str(image))
    candidate = Path(p["state_directory"]) / "candidate.json"
    if candidate.exists() or candidate.is_symlink():
        found.add(str(candidate))
    if found != set(p["protected"]):
        raise RuntimeError("Existing image inventory changed")
    for name, sha in p["protected"].items():
        regular(Path(name), sha)


def stopped() -> None:
    result = execute(
        [
            "systemctl",
            "show",
            "runspindle-fleet.service",
            "--property=ActiveState,SubState",
        ]
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if values.get("ActiveState") != "inactive" or values.get("SubState") != "dead":
        raise RuntimeError(
            "Manager must already be stopped; controller will not change it"
        )


def existing_network(p: dict[str, Any]) -> None:
    """Fresh observations only; denied probes fail before guest creation."""
    rules = execute(
        [
            "sudo",
            "-n",
            "nft",
            "--stateless",
            "list",
            "table",
            "inet",
            "runspindle_fleet",
        ]
    ).stdout
    if hashlib.sha256(rules.encode()).hexdigest() != p["network_rules_sha256"]:
        raise RuntimeError("Existing isolation rules differ from the reviewed rules")
    for unit in (
        "runspindle-proxy-0",
        "runspindle-proxy-1",
        "runspindle-host-canary",
        "runspindle-lan-canary",
    ):
        result = execute(
            ["systemctl", "show", unit + ".service", "--property=ActiveState,SubState"]
        )
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if values != {"ActiveState": "active", "SubState": "running"}:
            raise RuntimeError("Existing isolation services are not running")
    for slot in range(2):
        result = execute(["ip", "-j", "-d", "addr", "show", "dev", f"rsvm{slot}"])
        rows = json.loads(result.stdout)
        if (
            len(rows) != 1
            or "UP" not in rows[0].get("flags", [])
            or rows[0].get("linkinfo", {}).get("info_kind") != "tun"
            or not any(
                a.get("local") == f"10.231.{slot}.1" and a.get("prefixlen") == 30
                for a in rows[0].get("addr_info", [])
            )
        ):
            raise RuntimeError("Existing dedicated TAP configuration differs")


def idle_slots() -> None:
    for slot in range(2):
        row = helper("observe", slot)
        if (
            row.get("ActiveState") not in ("inactive", "failed")
            or int(row.get("MainPID", -1)) != 0
        ):
            raise RuntimeError("Live slot is not independently idle")
        if any((ROOT / "slots" / str(slot)).iterdir()):
            raise RuntimeError("Existing slot files require reconciliation")
        account = execute(["pgrep", "-u", f"runspindle-vm{slot}"], check=False)
        if account.returncode != 1:
            raise RuntimeError("Dedicated guest account is not independently idle")


def read_idle_state(directory: Path) -> None:
    """Read a fresh DB+WAL working copy; SQLite never opens fleet originals.

    SQLite mode=ro may create WAL/SHM sidecars. Copy bytes only into a private
    temporary directory, include committed WAL frames, and reject source drift.
    This is transient query material, not a retained backup or immutable=1 read.
    """
    database = directory / "fleet.sqlite"
    paths_to_read = (database, directory / "fleet.sqlite-wal")

    def capture() -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        total = 0
        for path in paths_to_read:
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                raise RuntimeError("Linked fleet state cannot be observed safely")
            if path != database and not path.exists():
                continue
            if not path.is_file():
                raise RuntimeError("Fleet state is missing or not regular")
            total += path.stat().st_size
            if total > 64 * 1024**2:
                raise RuntimeError("Fleet state exceeds bounded read snapshot")
            with path.open("rb") as stream:
                data = stream.read(64 * 1024**2 - sum(map(len, result.values())) + 1)
            if sum(map(len, result.values())) + len(data) > 64 * 1024**2:
                raise RuntimeError("Fleet state grew beyond bounded read snapshot")
            result[path.name] = data
        return result

    before = capture()
    with tempfile.TemporaryDirectory(prefix="runspindle-state-read-") as temporary:
        root = Path(temporary)
        for name, data in before.items():
            path = root / name
            path.write_bytes(data)
            path.chmod(0o600)
        uri = "file:" + quote(str(root / database.name)) + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            control = dict(
                db.execute(
                    "SELECT key,value FROM control WHERE key IN ('drained','error')"
                )
            )
            active = db.execute(
                "SELECT count(*) FROM resources WHERE phase != 'destroyed'"
            ).fetchone()[0]
        finally:
            db.close()
        if capture() != before:
            raise RuntimeError("Fleet state changed during read-only verification")
        if json.loads(control.get("drained", "false")) is not True or json.loads(
            control.get("error", "null")
        ):
            raise RuntimeError("Existing state must already be drained and healthy")
        if active:
            raise RuntimeError("Existing resources require reconciliation")


def verify_source(p: dict[str, Any]) -> None:
    head = execute(["git", "-C", str(SOURCE), "rev-parse", "HEAD"]).stdout.strip()
    dirty = execute(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(SOURCE),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ]
    ).stdout.strip()
    if dirty or head != p["execution_source_sha"] or binding(SOURCE) != p["binding"]:
        raise RuntimeError("Execution source is not the exact clean reviewed tree")


def verify(p: dict[str, Any]) -> None:
    import platform

    verify_source(p)
    if (
        platform.python_version() != p["host_python"]
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
    ):
        raise RuntimeError("Wrong controller interpreter or architecture")
    regular(Path(legacy.HELPER), p["helper_sha256"])
    helper_stat = Path(legacy.HELPER).stat()
    if helper_stat.st_uid != 0 or helper_stat.st_mode & 0o022:
        raise RuntimeError(
            "Privileged helper must be root owned and not group/world writable"
        )
    operation, image, manifest = paths(p)
    if any(
        path.exists() or path.is_symlink()
        for path in (operation, image.parent, manifest)
    ):
        raise RuntimeError("Operation identity/output already exists; no retry")
    regular(Path(p["base_image"]), p["base_sha256"])
    regular(Path(p["base_manifest"]), p["base_manifest_sha256"])
    image_stat = Path(p["base_image"]).stat()
    if image_stat.st_uid != 0 or image_stat.st_mode & 0o222:
        raise RuntimeError("Base must be sealed root-owned image")
    info = json.loads(
        execute(["qemu-img", "info", "--output=json", p["base_image"]]).stdout
    )
    if (
        info.get("format") != "qcow2"
        or info.get("backing-filename")
        or type(info.get("virtual-size")) is not int
        or not 0 < info["virtual-size"] <= 24 * 1024**3
    ):
        raise RuntimeError("Base must be a standalone qcow2 no larger than 24 GiB")
    base = json.loads(Path(p["base_manifest"]).read_text())
    if (
        base.get("manifest_kind") is not None
        or base.get("sha256") != p["base_sha256"]
        or not isinstance(base.get("guest"), dict)
    ):
        raise RuntimeError(
            "Base manifest must describe the exact ordinary qualified image"
        )
    cast(Callable[..., dict[str, str]], legacy.image_binding)(
        Path(p["base_image"]),
        Path(p["base_manifest"]),
        p["base_sha256"],
        candidate=False,
    )
    protected(p)
    stopped()
    existing_network(p)
    read_idle_state(Path(p["state_directory"]))
    idle_slots()
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("KVM access is unavailable")
    memory = (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    if memory < 18 * 1024**3:
        raise RuntimeError("Existing 18 GiB free-memory gate failed")
    for directory in (ROOT / "images", ROOT / "slots", Path(p["state_directory"])):
        if shutil.disk_usage(directory).free < p["minimum_free_bytes"]:
            raise RuntimeError("Insufficient approved temporary disk headroom")
    for name, row in p["sources"].items():
        regular(
            Path(p["source_bundles"]) / (name + ".bundle"), row["sha256"], row["bytes"]
        )
    receipt = verify_documents(SOURCE, Path(p["downloads"]), "linux/x86_64")
    if receipt["closure_sha256"] != p["document_closure_sha256"]:
        raise RuntimeError("Approved document closure differs")


def validation_script(p: dict[str, Any]) -> str:
    commands = [
        "set -euo pipefail",
        "export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0",
        "export GIT_ALLOW_PROTOCOL=file CARGO_NET_OFFLINE=true PIP_NO_INDEX=1",
        "export http_proxy='' https_proxy='' HTTP_PROXY='' HTTPS_PROXY='' ALL_PROXY='' all_proxy=''",
        "export PATH=/opt/runspindle-quality/bin:$PATH",
        "mkdir /home/runner/document-validation",
        "cd /home/runner/document-validation",
    ]
    for name in SOURCE_NAMES:
        target = (
            "root"
            if name == "root"
            else (
                "root/local/feedback-sources/" + name.removeprefix("feedback-")
                if name.startswith("feedback-")
                else "root/components/" + name
            )
        )
        bundle = "/home/runner/preparation/" + name + ".bundle"
        row = p["sources"][name]
        commands += [
            f"echo '{row['sha256']}  {bundle}' | sha256sum --check --strict",
            "git -c core.hooksPath=/dev/null clone --no-checkout --no-local "
            + shlex.quote(bundle)
            + " "
            + shlex.quote(target),
            "git -C "
            + shlex.quote(target)
            + " -c core.hooksPath=/dev/null checkout --detach "
            + row["commit"],
            "git -C " + shlex.quote(target) + " remote remove origin",
            'test "$(git -C '
            + shlex.quote(target)
            + ' rev-parse HEAD)" = '
            + row["commit"],
        ]
    commands += ["cd root"]
    for name in SOURCE_NAMES[1:8]:
        commands += [
            f"test \"$(git ls-tree HEAD components/{name} | awk '{{print $3}}')\" = {p['sources'][name]['commit']}"
        ]
    commands += [
        "python3 tools/feedback_hero.py --tether local/feedback-sources/tether --refinery local/feedback-sources/refinery",
        "export RUNSPINDLE_FEEDBACK_HERO_TETHER=$PWD/local/feedback-sources/tether",
        "export RUNSPINDLE_FEEDBACK_HERO_REFINERY=$PWD/local/feedback-sources/refinery",
        "/opt/runspindle-documents/bin/python tools/check_document_runtime.py",
        "/opt/runspindle-documents/bin/python tools/validate.py",
        "/opt/runspindle-documents/bin/python -O tools/validate.py",
        "git diff --exit-code",
        "echo RUNSPINDLE_DOCUMENT_VALIDATION=passed",
    ]
    return "\n".join(commands) + "\n"


DOCUMENT_SOURCES = (
    "tools/runner_fleet/__init__.py",
    "tools/runner_fleet/documents.py",
    "tools/runner_fleet/quality.py",
    "fleet/image-inputs.json",
    "fleet/document-closure.json",
    "requirements-documents.txt",
    "fleet/DOCUMENT_NOTICES.md",
    "tools/runner_fleet/prepare_document_overlay.sh",
)


class DocumentBackend(LegacyBackend):
    """Never reapplies shared networking; every creation observes it afresh."""

    def __init__(self, state: Any, p: dict[str, Any], *, validation: bool) -> None:
        _, image, manifest = paths(p)
        if validation:
            value = json.loads(manifest.read_text())
            expected = {
                "operation_id": p["operation_id"],
                "source_sha": p["execution_source_sha"],
                "base_image_sha256": p["base_sha256"],
                "base_manifest_sha256": p["base_manifest_sha256"],
                "document_closure_sha256": p["document_closure_sha256"],
                "validation_sources": p["sources"],
                **p["binding"],
            }
            if any(value.get(k) != v for k, v in expected.items()):
                raise RuntimeError(
                    "Explicit overlay manifest differs from this operation"
                )
        cast(Callable[..., None], super().__init__)(
            state,
            image if validation else Path(p["base_image"]),
            candidate_manifest=manifest,
            document_overlay=validation,
        )
        self.plan = p

    def ensure_network(self) -> None:
        stopped()
        existing_network(self.plan)
        idle_slots()

    def preparation_sources(self) -> tuple[str, ...]:
        return DOCUMENT_SOURCES


def prepare_overlay(p: dict[str, Any], backend: Any, resource: dict[str, Any]) -> None:
    verify_source(p)
    regular(Path(p["base_image"]), p["base_sha256"])
    regular(Path(p["base_manifest"]), p["base_manifest_sha256"])
    verify_documents(SOURCE, Path(p["downloads"]), "linux/x86_64")
    backend.create(resource, prepare=True)
    boot = time.monotonic() + 240
    while not backend.ready(resource):
        if time.monotonic() >= boot:
            raise RuntimeError("Preparation guest boot deadline")
        time.sleep(2)
    backend.isolation(resource, check_tooling=False)
    operation, _, _ = paths(p)
    base = json.loads(Path(p["base_manifest"]).read_text())
    context = {
        "operation_id": p["operation_id"],
        "base_image_sha256": p["base_sha256"],
        "base_manifest_sha256": p["base_manifest_sha256"],
        "document_closure_sha256": p["document_closure_sha256"],
        "execution_source_sha": p["execution_source_sha"],
        "preparation_source_sha256": {
            name: digest(SOURCE / name) for name in DOCUMENT_SOURCES
        },
    }
    for filename, data in (
        ("base-guest.json", base["guest"]),
        ("overlay-context.json", context),
    ):
        path = operation / filename
        with path.open("x") as stream:
            stream.write(json.dumps(data, sort_keys=True) + "\n")
        backend.copy_prepared(resource, path, filename)
    backend.copy_prepared(
        resource,
        Path(p["downloads"]) / "requirements-documents.txt",
        "requirements-documents.txt",
    )
    for row in load_documents(SOURCE)["wheels"]:
        filename = "document-wheels/" + row["filename"]
        backend.copy_prepared(resource, Path(p["downloads"]) / filename, filename)
    result = backend.ssh(
        resource,
        (SOURCE / "tools/runner_fleet/prepare_document_overlay.sh").read_text(),
        timeout=1500,
        check=False,
    )
    (operation / "preparation-output.log").write_text(result.stdout + result.stderr)
    prefix = "RUNSPINDLE_DOCUMENT_OVERLAY="
    manifests = [
        line[len(prefix) :]
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    ]
    if result.returncode != 0 or len(manifests) != 1:
        raise RuntimeError("Offline preparation did not acknowledge completion")
    guest = json.loads(manifests[0])
    if (
        guest.get("manifest_kind") != "document-overlay-v1"
        or guest.get("base_guest") != base["guest"]
        or any(guest.get(k) != v for k, v in context.items())
        or guest.get("documents", {}).get("closure_sha256")
        != p["document_closure_sha256"]
    ):
        raise RuntimeError("Prepared overlay manifest differs from approved inputs")
    with (operation / "guest.json").open("x") as stream:
        json.dump(guest, stream, sort_keys=True)


def seal_overlay(p: dict[str, Any], backend: Any, resource: dict[str, Any]) -> None:
    verify_source(p)
    operation, image, manifest_path = paths(p)
    result = backend.ssh(
        resource,
        "set -euo pipefail\nsudo cloud-init clean --logs --machine-id\nsudo rm -f /etc/ssh/ssh_host_* /home/runner/.ssh/authorized_keys\nsudo rm -rf /home/runner/preparation\necho RUNSPINDLE_OVERLAY_HYGIENE=complete\nsudo shutdown -h now\n",
        timeout=120,
        check=False,
    )
    if (
        result.returncode not in (0, 255)
        or result.stdout.splitlines().count("RUNSPINDLE_OVERLAY_HYGIENE=complete") != 1
    ):
        raise RuntimeError("Image hygiene completion is not acknowledged")
    until = time.monotonic() + 60
    while True:
        observed = helper("observe", resource["slot"])
        if observed.get("resource_id") != resource["id"]:
            raise RuntimeError("Preparation ownership changed")
        if (
            observed.get("ActiveState") in ("inactive", "failed")
            and int(observed.get("MainPID", -1)) == 0
        ):
            break
        if time.monotonic() >= until:
            raise RuntimeError("Preparation shutdown not observed")
        time.sleep(2)
    safe_slot_files(backend.slot(resource))
    helper("stop", resource["slot"], resource["id"])
    if (
        execute(
            ["pgrep", "-u", f"runspindle-vm{resource['slot']}"], check=False
        ).returncode
        != 1
    ):
        raise RuntimeError("Preparation process remains before conversion")
    # Atomic namespace creation, not an exists-check followed by overwriting an
    # image. Only this owned namespace may receive output. No shared file changes.
    execute(["sudo", "-n", "mkdir", "-m", "0755", str(image.parent)])
    execute(
        [
            "sudo",
            "-n",
            "qemu-img",
            "convert",
            "-O",
            "qcow2",
            str(backend.slot(resource) / "disk.qcow2"),
            str(image),
        ],
        timeout=600,
    )
    execute(["sudo", "-n", "chmod", "0444", str(image)])
    if image.is_symlink() or image.stat().st_uid != 0 or image.stat().st_mode & 0o222:
        raise RuntimeError("Overlay output is not sealed")
    manifest = {
        "manifest_kind": "document-overlay-v1",
        "schema_version": 1,
        "operation_id": p["operation_id"],
        "candidate_path": str(image),
        "sha256": digest(image),
        "source_sha": p["execution_source_sha"],
        **p["binding"],
        "base_image_sha256": p["base_sha256"],
        "base_manifest_sha256": p["base_manifest_sha256"],
        "document_closure_sha256": p["document_closure_sha256"],
        "guest": json.loads((operation / "guest.json").read_text()),
        "preparation_source_sha256": json.loads((operation / "guest.json").read_text())[
            "preparation_source_sha256"
        ],
        "validation_sources": p["sources"],
        "qualification": "not_run",
        "full_recipe_rebuild": False,
        "ordinary_fleet_eligible": False,
    }
    with manifest_path.open("x") as stream:
        json.dump(manifest, stream, indent=2)


def safe_slot_files(slot: Path) -> None:
    if any(
        path.name not in {"disk.qcow2", "seed.iso", "serial.log"}
        or path.is_symlink()
        or not path.is_file()
        for path in slot.iterdir()
    ):
        raise RuntimeError("Unexpected slot files retained for reconciliation")


def owned_cleanup(backend: Any, resource: dict[str, Any]) -> None:
    before = helper("observe", resource["slot"])
    if (
        before.get("resource_id") != resource["id"]
        or before.get("ActiveState")
        not in ("active", "activating", "deactivating", "inactive", "failed")
        or int(before.get("MainPID", -1)) < 0
    ):
        # Partial create before ownership is durable cannot be guessed safe.
        raise RuntimeError("Owned cleanup cannot establish matching helper identity")
    slot = backend.slot(resource)
    expected = {"disk.qcow2", "seed.iso", "serial.log"}
    # The fixed host helper removes symlinks while returning ownership. Reject
    # unknown entries first; never claim it preserves evidence it would delete.
    safe_slot_files(slot)
    helper("stop", resource["slot"], resource["id"])
    after = helper("observe", resource["slot"])
    if (
        after.get("resource_id") != resource["id"]
        or after.get("ActiveState") not in ("inactive", "failed")
        or int(after.get("MainPID", -1)) != 0
    ):
        raise RuntimeError("Owned guest cleanup is not independently verified")
    if (
        execute(
            ["pgrep", "-u", f"runspindle-vm{resource['slot']}"], check=False
        ).returncode
        != 1
    ):
        raise RuntimeError("Dedicated guest account still has processes")
    safe_slot_files(slot)
    serial = slot / "serial.log"
    if serial.exists():
        shutil.copyfile(serial, backend.directory(resource) / "serial.log")
    for name in expected:
        (slot / name).unlink(missing_ok=True)
    (backend.directory(resource) / "key").unlink(missing_ok=True)
    if any(slot.iterdir()):
        raise RuntimeError("Owned slot cleanup remains uncertain")


def worker(p: dict[str, Any], phase: str, resource: dict[str, Any] | None) -> None:
    # Only log sinks are capped by the parent. A process-wide file-size limit
    # would also cap the image produced by qemu-img and must not be inherited.
    if phase == "verify":
        verify(p)
        return
    if phase == "final":
        verify_source(p)
        protected(p)
        stopped()
        existing_network(p)
        read_idle_state(Path(p["state_directory"]))
        idle_slots()
        return
    if resource is None:
        raise ValueError("Owned reservation required")
    state = State(Path(p["state_directory"]))
    operation, image, manifest = paths(p)
    try:
        expected_stage = (
            "prepare" if phase in ("prepare", "seal", "prepare-cleanup") else "validate"
        )
        rows = [row for row in state.active() if row["id"] == resource["id"]]
        if (
            len(rows) != 1
            or rows[0]["slot"] != resource["slot"]
            or rows[0]["details"].get("document_operation") != p["operation_id"]
            or rows[0]["details"].get("stage") != expected_stage
            or rows[0]["details"].get("cleanup_verified") is not False
            or json.loads((operation / "plan.json").read_text()) != p
        ):
            raise RuntimeError("Worker does not have the matching durable reservation")
        backend: Any = DocumentBackend(state, p, validation=phase == "validate")
        if phase.endswith("-cleanup"):
            owned_cleanup(backend, resource)
        elif phase == "prepare":
            prepare_overlay(p, backend, resource)
        elif phase == "seal":
            seal_overlay(p, backend, resource)
        elif phase == "validate":
            verify_source(p)
            backend.create(resource)
            boot = time.monotonic() + 240
            while not backend.ready(resource):
                if time.monotonic() >= boot:
                    raise RuntimeError("Validation guest boot deadline")
                time.sleep(2)
            backend.isolation(resource)
            for name, row in p["sources"].items():
                path = Path(p["source_bundles"]) / (name + ".bundle")
                regular(path, row["sha256"], row["bytes"])
                backend.copy_prepared(resource, path, path.name)
            result = backend.ssh(
                resource, validation_script(p), timeout=900, check=False
            )
            (operation / "validation.log").write_text(result.stdout + result.stderr)
            if (
                result.returncode
                or result.stdout.splitlines().count(
                    "RUNSPINDLE_DOCUMENT_VALIDATION=passed"
                )
                != 1
            ):
                raise RuntimeError("Required root checks failed")
        else:
            raise ValueError("Unknown bounded phase")
    finally:
        state.db.close()


def phase(
    p: dict[str, Any],
    name: str,
    resource: dict[str, Any] | None,
    end: float,
    *,
    cleanup: bool = False,
) -> None:
    operation, _, _ = paths(p)
    payload = json.dumps({"plan": p, "phase": name, "resource": resource})
    with alarm(end, cancellation=not cleanup):
        supervise(
            [
                sys.executable,
                "-B",
                str(SOURCE / "tools/document_candidate.py"),
                "--worker",
            ],
            payload,
            operation / (name + ".log"),
            end,
        )


def initial_verify(p: dict[str, Any], end: float) -> None:
    """No fleet writes or State constructor; supervisor owns only a temporary log."""
    with tempfile.TemporaryDirectory(
        prefix="runspindle-document-verification-"
    ) as directory:
        log = Path(directory) / "verify.log"
        with alarm(end):
            try:
                supervise(
                    [
                        sys.executable,
                        "-B",
                        str(SOURCE / "tools/document_candidate.py"),
                        "--worker",
                    ],
                    json.dumps({"plan": p, "phase": "verify", "resource": None}),
                    log,
                    end,
                )
            except RuntimeError as error:
                detail = log.read_text()[-2000:] if log.exists() else "no worker output"
                raise RuntimeError(
                    "Read-only prerequisites failed: " + detail
                ) from error


def run(p: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    end = started + TOTAL
    with alarm(end):
        return run_bounded(p, started, end)


def run_bounded(p: dict[str, Any], started: float, end: float) -> dict[str, Any]:
    operation, _, _ = paths(p)
    # Verification precedes State and fleet writes, under process supervision.
    initial_verify(p, min(end - 600, started + PHASES["verify"]))
    state: Any = None
    current: dict[str, Any] | None = None
    failure: BaseException | None = None
    uncertain_process = False
    try:
        with alarm(min(end - 600, time.monotonic() + 10)):
            state = State(Path(p["state_directory"]))
        with state.lock():
            with alarm(min(end - 600, time.monotonic() + 10)):
                stopped()
                if (
                    state.get("drained") is not True
                    or state.get("error")
                    or state.active()
                ):
                    raise RuntimeError("State changed after read-only verification")
                operation.mkdir(parents=True, mode=0o700, exist_ok=False)
                (operation / "plan.json").write_text(json.dumps(p, indent=2) + "\n")
            for stage, job in (("prepare", -3), ("validate", -4)):
                stage_end = min(end - 600, time.monotonic() + PHASES[stage])
                try:
                    with alarm(min(stage_end, time.monotonic() + 10)):
                        current = state.reserve(
                            {"repository": "runspindle-platform", "id": job},
                            details={
                                "document_operation": p["operation_id"],
                                "stage": stage,
                                "execution_source_sha": p["execution_source_sha"],
                            },
                        )
                        # Durable unknown status before any phase can mutate the host.
                        state.update(current, "uncertain", cleanup_verified=False)
                    phase(
                        p,
                        stage,
                        current,
                        stage_end,
                    )
                    if stage == "prepare":
                        phase(
                            p,
                            "seal",
                            current,
                            min(end - 600, time.monotonic() + PHASES["seal"]),
                        )
                except BaseException as error:
                    failure = error
                    uncertain_process = isinstance(error, UncertainProcess)
                finally:
                    if current is not None:
                        name = stage + "-cleanup"
                        cleanup_end = min(end - 300, time.monotonic() + PHASES[name])
                        try:
                            if uncertain_process:
                                raise UncertainProcess(
                                    "Retaining capacity: phase process termination is uncertain"
                                )
                            phase(
                                p,
                                name,
                                current,
                                cleanup_end,
                                cleanup=True,
                            )
                            with alarm(
                                min(cleanup_end, time.monotonic() + 5),
                                cancellation=False,
                            ):
                                state.update(
                                    current, "destroyed", cleanup_verified=True
                                )
                            current = None
                        except BaseException as error:
                            failure = error
                            # The pre-effect uncertain record survives SIGKILL too.
                            with alarm(
                                min(end, time.monotonic() + 5), cancellation=False
                            ):
                                state.update(
                                    current,
                                    "uncertain",
                                    cleanup_verified=False,
                                    error=type(error).__name__,
                                )
                if failure is not None:
                    break
            # Never start a second guest while a previous reservation is unknown.
            try:
                phase(
                    p,
                    "final",
                    None,
                    min(end, time.monotonic() + PHASES["final"]),
                    cleanup=True,
                )
            except BaseException as error:
                failure = error
            # reserve() may commit then lose its response. A local None is not
            # evidence that no capacity was acquired. Re-read durable occupancy.
            try:
                with alarm(min(end, time.monotonic() + 5), cancellation=False):
                    remaining = state.active()
                capacity_uncertain = bool(remaining)
                if capacity_uncertain:
                    failure = RuntimeError("Durable capacity remains occupied")
            except BaseException as error:
                capacity_uncertain = True
                failure = error
            result = {
                **plan_summary(p),
                "status": "passed" if failure is None else "failed",
                "qualification": "unregistered_candidate_evidence_only",
                "seconds": time.monotonic() - started,
                "cleanup_verified": not capacity_uncertain,
                "remaining_capacity_uncertain": capacity_uncertain,
            }
            with alarm(end, cancellation=False):
                (operation / "result.json").write_text(
                    json.dumps(result, indent=2) + "\n"
                )
            if failure is not None:
                raise RuntimeError(
                    "Candidate failed or is uncertain; inspect private operation evidence"
                ) from failure
            return result
    finally:
        if state is not None:
            state.db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        value = json.loads(sys.stdin.read(65537))
        p = validate_plan(value["plan"])
        worker(p, value["phase"], value["resource"])
    elif args.plan is not None:
        p = validate_plan(json.loads(args.plan.read_text()))
        print(json.dumps(run(p) if args.execute else plan_summary(p), indent=2))
    else:
        parser.error("--plan is required; execution additionally requires --execute")


if __name__ == "__main__":
    main()
