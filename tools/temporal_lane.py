#!/usr/bin/env python3
"""Bounded disposable Temporal development lane; no implicit downloads at runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterable
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PINS = ROOT / "temporal/dependencies.json"


class Blocked(RuntimeError):
    """A missing or unqualified prerequisite; never a passing integration gate."""


def digest(path: str | pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def command(
    args: list[str], timeout: float = 30, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=check
    )


def docker(
    *args: object, timeout: float = 30, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return command(["docker", *map(str, args)], timeout, check)


def retention_requested(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    value = os.environ.get("RUNSPINDLE_TEMPORAL_RETAIN", "0")
    if value not in ("0", "1"):
        raise ValueError("RUNSPINDLE_TEMPORAL_RETAIN must be 0 or 1")
    return value == "1"


def diagnostic_directory(root: pathlib.Path, name: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unsafe Temporal diagnostic directory")
    parent = root / "diagnostics"
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("unsafe Temporal diagnostics parent")
    path = parent / name
    path.mkdir(mode=0o700)
    return path


def diagnostic_capture(
    directory: pathlib.Path, name: str, payload: bytes
) -> pathlib.Path:
    if pathlib.Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("unsafe Temporal diagnostic capture")
    path = directory / name
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if path.read_bytes() != payload:
        raise RuntimeError("Temporal diagnostic capture changed; originals retained")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def recorded_docker(
    directory: pathlib.Path, *args: object, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    def observed(value: bytes | str | None) -> dict[str, str]:
        if isinstance(value, bytes):
            return {"base64_original_bytes": base64.b64encode(value).decode()}
        return {"text": value or ""}

    try:
        result = docker(*args, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        diagnostic_capture(
            directory,
            "command-" + uuid.uuid4().hex + ".json",
            json.dumps(
                {
                    "argv": list(map(str, args)),
                    "error": str(error),
                    "stdout": observed(error.stdout)
                    if isinstance(error, subprocess.TimeoutExpired)
                    else {},
                    "stderr": observed(error.stderr)
                    if isinstance(error, subprocess.TimeoutExpired)
                    else {},
                },
                sort_keys=True,
            ).encode(),
        )
        raise
    diagnostic_capture(
        directory,
        "command-" + uuid.uuid4().hex + ".json",
        json.dumps(
            {
                "argv": list(map(str, args)),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            sort_keys=True,
        ).encode(),
    )
    return result


def container_observation(
    result: subprocess.CompletedProcess[str], name: str
) -> dict[str, Any] | None:
    if result.returncode:
        if "No such container" in result.stderr or "No such object" in result.stderr:
            return None
        raise RuntimeError("container observation denied or unavailable; no fallback")
    values = json.loads(result.stdout)
    if (
        not isinstance(values, list)
        or len(values) != 1
        or values[0].get("Name", "").lstrip("/") != name
    ):
        raise RuntimeError("unexpected retained container identity")
    container = values[0]
    if not isinstance(container, dict):
        raise RuntimeError("unexpected retained container record")
    return container


def require_retained_logging(container: dict[str, Any]) -> None:
    config = container.get("HostConfig", {}).get("LogConfig", {})
    options = config.get("Config")
    # Omitted max-size selects json-file's unlimited default. An explicit -1
    # is rejected by the pinned Docker daemon. Inspect merged daemon defaults
    # before start: inherited rotation or nonblocking delivery must fail closed.
    if (
        config.get("Type") != "json-file"
        or not isinstance(options, dict)
        or "max-size" in options
        or options.get("mode") != "blocking"
    ):
        raise RuntimeError("retained logs require nonrotating blocking json-file")


def require_stopped(container: dict[str, Any]) -> None:
    state = container.get("State", {})
    if (
        state.get("Running") is not False
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or state.get("Pid") != 0
        or state.get("Status") not in ("created", "exited", "dead")
        or container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"
    ):
        raise RuntimeError(
            "retained container still has execution or restart authority"
        )
    require_retained_logging(container)


def archive_container(name: str, directory: pathlib.Path) -> dict[str, Any]:
    generation = directory / ("container-" + uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    before = container_observation(
        recorded_docker(generation, "container", "inspect", name), name
    )
    if before is None:
        receipt: dict[str, Any] = {
            "name": name,
            "status": "not_created",
            "evidence": str(generation),
        }
    else:
        original_id = before.get("Id")
        if not isinstance(original_id, str) or not original_id:
            raise RuntimeError("original container ID missing; no log receipt")
        state = before.get("State", {})
        if state.get("Running") or state.get("Paused") or state.get("Restarting"):
            stopped = recorded_docker(
                generation, "stop", "--time", "10", original_id, timeout=15
            )
            if stopped.returncode:
                raise RuntimeError(
                    "retained container stop failed; no removal attempted"
                )
        after = container_observation(
            recorded_docker(generation, "container", "inspect", name), name
        )
        if after is None or after.get("Id") != original_id:
            raise RuntimeError(
                "original container disappeared or was replaced before log capture"
            )
        require_stopped(after)
        logs = recorded_docker(generation, "logs", "--timestamps", original_id)
        if logs.returncode:
            raise RuntimeError("complete retained container logs unavailable")
        stdout = diagnostic_capture(generation, "stdout.log", logs.stdout.encode())
        stderr = diagnostic_capture(generation, "stderr.log", logs.stderr.encode())
        repeated = recorded_docker(generation, "logs", "--timestamps", original_id)
        if repeated.returncode or (logs.stdout, logs.stderr) != (
            repeated.stdout,
            repeated.stderr,
        ):
            raise RuntimeError("stopped container logs changed; original retained")
        receipt = {
            "name": name,
            "status": "stopped_retained",
            "evidence": str(generation),
            "container_id": after["Id"],
            "exit_code": after["State"]["ExitCode"],
            "native_log_path": after.get("LogPath"),
            "capture_scope": "text readback; original unrotated Docker json-file logs retained",
            "stdout": str(stdout),
            "stderr": str(stderr),
            "stdout_sha256": digest(stdout),
            "stderr_sha256": digest(stderr),
        }
    diagnostic_capture(
        generation,
        "retained-container.json",
        json.dumps(receipt, sort_keys=True).encode(),
    )
    return receipt


def verify_retained_container(receipt: dict[str, Any]) -> None:
    name = receipt["name"]
    current = container_observation(
        docker("container", "inspect", name, check=False), name
    )
    if receipt["status"] == "not_created":
        if current is not None:
            raise RuntimeError("unexpected container appeared after failed creation")
        return
    if current is None or current.get("Id") != receipt["container_id"]:
        raise RuntimeError("retained original container missing or replaced")
    require_stopped(current)
    logs = docker("logs", "--timestamps", receipt["container_id"], check=False)
    if (
        logs.returncode
        or digest(receipt["stdout"]) != receipt["stdout_sha256"]
        or digest(receipt["stderr"]) != receipt["stderr_sha256"]
        or pathlib.Path(receipt["stdout"]).read_bytes() != logs.stdout.encode()
        or pathlib.Path(receipt["stderr"]).read_bytes() != logs.stderr.encode()
    ):
        raise RuntimeError(
            "retained logs do not match independent stopped-container readback"
        )


def auxiliary_identity(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    if kind == "volume":
        required = ("Name", "Driver", "Mountpoint", "CreatedAt", "Scope")
        fields: tuple[str, ...] = required + ("Labels", "Options")
    elif kind == "network":
        required = ("Name", "Id", "Created", "Driver", "Scope")
        fields = required + ("Internal", "EnableIPv6", "Labels", "Options")
        if record.get("Internal") is not True:
            raise RuntimeError("retained network no longer isolated")
    else:
        raise ValueError("unsupported auxiliary identity kind")
    if any(
        not isinstance(record.get(field), str) or not record[field]
        for field in required
    ):
        raise RuntimeError("incomplete stable auxiliary identity")
    return {field: record.get(field) for field in fields}


def retained_probe(
    image: str, arguments: list[str], root: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    if not re.fullmatch("sha256:[a-f0-9]{64}", image):
        raise Blocked("retained probe requires immutable image identity")
    name = "runspindle-temporal-probe-" + uuid.uuid4().hex
    evidence = diagnostic_directory(root, name)
    try:
        created = recorded_docker(
            evidence,
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--log-driver",
            "json-file",
            "--log-opt",
            "mode=blocking",
            "--restart",
            "no",
            "--pids-limit",
            "128",
            "--memory",
            "1g",
            "--cpus",
            "2",
            image,
            *arguments,
        )
        if created.returncode:
            raise RuntimeError("retained probe creation failed")
        observed = container_observation(
            recorded_docker(evidence, "container", "inspect", name), name
        )
        if observed is None:
            raise RuntimeError("retained probe disappeared before start")
        require_stopped(observed)
        if recorded_docker(evidence, "start", name).returncode:
            raise RuntimeError("retained probe start failed")
        waited = recorded_docker(evidence, "wait", name, timeout=60)
        if waited.returncode or not waited.stdout.strip().isdigit():
            raise RuntimeError("retained probe termination uncertain")
    finally:
        receipt = archive_container(name, evidence)
    verify_retained_container(receipt)
    if receipt["status"] != "stopped_retained":
        raise RuntimeError("retained probe never produced stopped execution evidence")
    raw_logs = recorded_docker(evidence, "logs", receipt["container_id"])
    if raw_logs.returncode:
        raise RuntimeError("retained probe raw output unavailable")
    result = subprocess.CompletedProcess(
        arguments, receipt["exit_code"], raw_logs.stdout, raw_logs.stderr
    )
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, arguments, result.stdout, result.stderr
        )
    return result


def read_pins() -> Any:
    pins = json.loads(PINS.read_text())
    cli = pins["cli"]
    if cli["version"] != "1.10.0" or not re.fullmatch("[a-f0-9]{40}", cli["revision"]):
        raise Blocked("unqualified Temporal source/version")
    for artifact in cli["artifacts"].values():
        if not re.fullmatch("[a-f0-9]{64}", artifact["sha256"]):
            raise Blocked("unqualified artifact digest")
    return pins


def build_verified_image(binary: bytes, license_bytes: bytes, architecture: str) -> str:
    """Build only verified bytes and a fixed recipe, with no executable RUN steps."""
    with tempfile.TemporaryDirectory(prefix="runspindle-verified-temporal-") as context:
        directory = pathlib.Path(context)
        for name, data, mode in [
            ("temporal", binary, 0o755),
            ("LICENSE", license_bytes, 0o644),
        ]:
            target = directory / name
            target.write_bytes(data)
            target.chmod(mode)
            os.utime(target, (0, 0))
        (directory / "Dockerfile").write_text(
            'FROM scratch\nCOPY temporal /temporal\nCOPY LICENSE /LICENSE\nENTRYPOINT ["/temporal"]\n'
        )
        tag = (
            "runspindle-temporal-verified:"
            + hashlib.sha256(
                binary + license_bytes + architecture.encode()
            ).hexdigest()[:32]
        )
        docker(
            "build",
            "--load",
            "--tag",
            tag,
            "--network",
            "none",
            "--pull=false",
            "--provenance=false",
            "--build-arg",
            "SOURCE_DATE_EPOCH=0",
            "--platform",
            f"linux/{architecture}",
            "--iidfile",
            directory / "image.id",
            directory,
            timeout=120,
        )
        # Docker's containerd image store can identify the runnable image by
        # its manifest digest while --iidfile contains the config digest. Resolve
        # our content-derived tag through the daemon; never accept caller tags.
        return docker("image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()


def verified_assets(
    directory: pathlib.Path, architecture: str, pins: dict[str, Any]
) -> tuple[bytes, bytes]:
    cli = pins["cli"]
    artifact = cli["artifacts"][architecture]
    archive = directory / artifact["filename"]
    if digest(archive) != artifact["sha256"]:
        raise Blocked("Temporal artifact checksum mismatch")
    license_bytes = (directory / "LICENSE").read_bytes()
    if hashlib.sha256(license_bytes).hexdigest() != cli["license_sha256"]:
        raise Blocked("Temporal license checksum mismatch")
    with tarfile.open(archive) as source:
        member = source.getmember("temporal")
        if not member.isfile():
            raise Blocked("expected regular Temporal executable")
        binary = source.extractfile(member).read()
    return binary, license_bytes


def prepare(
    directory: str | pathlib.Path,
    architecture: str,
    offline: bool = False,
    retain_diagnostics: bool | None = None,
) -> dict[str, Any]:
    """Explicit acquisition only. Reuses only checksum-verified downloads."""
    pins = read_pins()
    cli = pins["cli"]
    artifact = cli["artifacts"][architecture]
    directory = pathlib.Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / artifact["filename"]
    base = f"https://github.com/temporalio/cli/releases/download/v{cli['version']}"
    if not archive.exists():
        if offline:
            raise Blocked("offline Temporal archive missing")
        command(
            [
                "curl",
                "--fail",
                "--location",
                "--max-time",
                "600",
                "--retry",
                "2",
                f"{base}/{artifact['filename']}",
                "--output",
                str(archive),
            ],
            timeout=1810,
        )
    if digest(archive) != artifact["sha256"]:
        raise Blocked("Temporal artifact checksum mismatch")
    license_path = directory / "LICENSE"
    if not license_path.exists():
        if offline:
            raise Blocked("offline Temporal license missing")
        command(
            [
                "curl",
                "--fail",
                "--location",
                "--max-time",
                "60",
                f"https://raw.githubusercontent.com/temporalio/cli/{cli['revision']}/LICENSE",
                "--output",
                str(license_path),
            ],
            timeout=65,
        )
    if digest(license_path) != cli["license_sha256"]:
        raise Blocked("Temporal license checksum mismatch")
    # Do not extract arbitrary paths or links from a release archive.
    binary, license_bytes = verified_assets(directory, architecture, pins)
    (directory / "temporal").write_bytes(binary)
    (directory / "temporal").chmod(0o755)
    # Stable file metadata gives repeatable scratch layers across preparation.
    os.utime(directory / "temporal", (0, 0))
    os.utime(license_path, (0, 0))
    image = build_verified_image(binary, license_bytes, architecture)
    (directory / "image.id").write_text(image + "\n")
    if retention_requested(retain_diagnostics):
        version = retained_probe(image, ["--version"], directory).stdout.strip()
    else:
        version = docker(
            "run", "--rm", "--network", "none", "--pull", "never", image, "--version"
        ).stdout.strip()
    if "1.10.0" not in version:
        raise Blocked("unexpected CLI executable version")
    metadata = {
        "schema_version": 1,
        "image": image,
        "architecture": architecture,
        "pins_sha256": digest(PINS),
        "binary_sha256": digest(directory / "temporal"),
        "archive_sha256": digest(archive),
        "version": version,
    }
    (directory / "prepared.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


class Lane:
    """Use as a context manager so exceptions and SIGINT/SIGTERM trigger cleanup.

    Clients share only the internal network. Native host services and host ports
    are never exposed. The same private volume is reused by restart().
    """

    address = "temporal:7233"

    def __init__(
        self,
        prepared_dir: str | pathlib.Path,
        readiness_deadline: float = 60,
        retain_diagnostics: bool | None = None,
    ) -> None:
        self.directory = pathlib.Path(prepared_dir).resolve()
        self.metadata = json.loads((self.directory / "prepared.json").read_text())
        pins = read_pins()
        if self.metadata["pins_sha256"] != digest(PINS):
            raise Blocked("prepared dependency manifest changed; prepare again")
        if self.metadata["binary_sha256"] != digest(self.directory / "temporal"):
            raise Blocked("prepared executable changed")
        self.image = self.metadata["image"]
        self._image(self.image)
        binary, license_bytes = verified_assets(
            self.directory, self.metadata["architecture"], pins
        )
        if hashlib.sha256(binary).hexdigest() != self.metadata["binary_sha256"]:
            raise Blocked("prepared executable does not match pinned release archive")
        # Metadata is editable local data. Rebuild from the verified release bytes
        # without network or RUN instructions before executing any claimed image.
        expected_image = build_verified_image(
            binary, license_bytes, self.metadata["architecture"]
        )
        if self.image != expected_image:
            raise Blocked(
                "prepared image does not match pinned release bytes and recipe"
            )
        self.name = "runspindle-temporal-" + uuid.uuid4().hex
        self.network = self.name + "-net"
        self.volume = self.name + "-db"
        self.server = self.name + "-server"
        self.clients: set[str] = set()
        self.retain_diagnostics = retention_requested(retain_diagnostics)
        self.owned_containers = {self.server}
        self.archived_containers: dict[str, dict[str, Any]] = {}
        self.retention_error: str | None = None
        self.auxiliary_identities: dict[str, dict[str, Any]] = {}
        if self.retain_diagnostics:
            self.diagnostics = diagnostic_directory(self.directory, self.name)
        self.readiness_deadline = float(readiness_deadline)
        if not 0 < self.readiness_deadline <= 180:
            raise ValueError("readiness deadline must be in (0,180] seconds")
        self._handlers: dict[int, Any] = {}
        self._created = False

    def _call(
        self, *args: object, timeout: float = 30, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if not getattr(self, "retain_diagnostics", False):
            return docker(*args, timeout=timeout, check=check)
        result = recorded_docker(self.diagnostics, *args, timeout=timeout)
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, list(map(str, args)), result.stdout, result.stderr
            )
        return result

    def _observe_auxiliary(self, kind: str, name: str) -> dict[str, Any] | None:
        observed = self._call(kind, "inspect", name, check=False)
        if observed.returncode:
            lower = observed.stderr.lower()
            missing = (kind == "volume" and "no such volume" in lower) or (
                kind == "network"
                and (
                    ("network" in lower and "not found" in lower)
                    or "no such network" in lower
                )
            )
            if missing:
                return None
            raise RuntimeError(
                "retained auxiliary resource observation denied or unavailable"
            )
        values = json.loads(observed.stdout)
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
            or values[0].get("Name") != name
        ):
            raise RuntimeError("unexpected retained auxiliary identity")
        record = values[0]
        return record

    def _pin_auxiliary(
        self, kind: str, name: str, created_id: str | None = None
    ) -> None:
        record = self._observe_auxiliary(kind, name)
        if record is None:
            raise RuntimeError("newly created auxiliary resource disappeared")
        identity = auxiliary_identity(kind, record)
        if created_id is not None and identity.get("Id") != created_id:
            raise RuntimeError("created network ID differs from first observation")
        self.auxiliary_identities[kind] = identity
        diagnostic_capture(
            self.diagnostics,
            kind + "-identity.json",
            json.dumps(identity, sort_keys=True).encode(),
        )

    @staticmethod
    def _image(image: str) -> None:
        if not re.fullmatch("sha256:[a-f0-9]{64}", image):
            raise Blocked("runtime requires an immutable local image ID")
        docker("image", "inspect", image)

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._cancel)
        try:
            return self.start()
        except BaseException:
            self.close()
            raise

    def _cancel(self, signum, frame):
        raise KeyboardInterrupt(f"lane cancelled by signal {signum}")

    def __exit__(self, *exc):
        self.close()

    def start(self):
        if self._created:
            raise RuntimeError("lane already started")
        # Resource names are known before each request; cleanup handles lost replies.
        self._created = True
        try:
            created_network = self._call(
                "network",
                "create",
                "--internal",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                "--label",
                "runspindle.synthetic=true",
                self.network,
            )
            if getattr(self, "retain_diagnostics", False):
                self._pin_auxiliary(
                    "network", self.network, created_network.stdout.strip()
                )
            self._call(
                "volume", "create", "--label", "runspindle.synthetic=true", self.volume
            )
            if getattr(self, "retain_diagnostics", False):
                self._pin_auxiliary("volume", self.volume)
            self._start_server()
        except BaseException:
            self.close()
            raise
        return self

    def _limits(
        self, name: str, image: str, mounts: Iterable[dict[str, Any]] = ()
    ) -> list[str]:
        self._image(image)
        args = [
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--label",
            "runspindle.synthetic=true",
            "--env",
            "USER=synthetic",
        ]
        if getattr(self, "retain_diagnostics", False):
            args[:2] = ["create"]
            args += [
                "--restart",
                "no",
                "--log-driver",
                "json-file",
                "--log-opt",
                "mode=blocking",
            ]
        for mount in mounts:
            source = pathlib.Path(mount["source"]).resolve(strict=True)
            target = mount["target"]
            if "," in str(source) or "," in target or not target.startswith("/"):
                raise ValueError("invalid bind mount")
            args += [
                "--mount",
                f"type=bind,src={source},dst={target}"
                + (",readonly" if mount.get("read_only", True) else ""),
            ]
        return args

    def _launch(self, args: list[str], name: str) -> None:
        self._call(*args)
        if getattr(self, "retain_diagnostics", False):
            observed = container_observation(
                self._call("container", "inspect", name), name
            )
            if observed is None:
                raise RuntimeError("retained container disappeared before start")
            require_stopped(observed)
            self._call("start", name)

    def _start_server(self):
        args = self._limits(self.server, self.image)
        args += [
            "--network-alias",
            "temporal",
            "--mount",
            f"type=volume,src={self.volume},dst=/data",
            self.image,
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--headless",
            "--db-filename",
            "/data/temporal.sqlite",
            "--log-level",
            "error",
        ]
        self._launch(args, self.server)
        deadline = time.monotonic() + self.readiness_deadline
        while time.monotonic() < deadline:
            result = self._call(
                "exec",
                self.server,
                "/temporal",
                "operator",
                "cluster",
                "health",
                "--address",
                "127.0.0.1:7233",
                "--command-timeout",
                "2s",
                timeout=4,
                check=False,
            )
            if result.returncode == 0:
                return
            if (
                self._call(
                    "inspect", "--format", "{{.State.Running}}", self.server
                ).stdout.strip()
                != "true"
            ):
                raise Blocked("Temporal server exited before readiness")
            time.sleep(0.1)
        raise Blocked("Temporal readiness deadline exceeded")

    def _archive_owned(self, name: str) -> dict[str, Any]:
        try:
            receipt = archive_container(name, self.diagnostics)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            self.retention_error = "retention failed; no automatic retry: " + str(error)
            raise RuntimeError(self.retention_error) from error
        self.archived_containers[name] = receipt
        return receipt

    def restart(self, abrupt=False):
        if not self._created:
            raise RuntimeError("lane not running")
        try:
            if abrupt:
                self._call("kill", self.server)
            else:
                self._call("stop", "--time", "10", self.server, timeout=15)
        except (OSError, subprocess.SubprocessError) as error:
            if getattr(self, "retain_diagnostics", False):
                self.retention_error = (
                    "restart outcome uncertain; no automatic retry: " + str(error)
                )
            raise
        if getattr(self, "retain_diagnostics", False):
            self._archive_owned(self.server)
            self.server = self.name + "-server-" + uuid.uuid4().hex[:12]
            self.owned_containers.add(self.server)
        else:
            self._call("rm", "--force", self.server)
        self._start_server()

    def cli(self, *args, timeout=30, check=True):
        return self._call(
            "exec",
            self.server,
            "/temporal",
            "--address",
            "127.0.0.1:7233",
            *args,
            timeout=timeout,
            check=check,
        )

    def spawn_client(self, image_id, args, mounts=()):
        name = self.name + "-client-" + uuid.uuid4().hex[:12]
        self.clients.add(name)
        if getattr(self, "retain_diagnostics", False):
            self.owned_containers.add(name)
        self._launch([*self._limits(name, image_id, mounts), image_id, *args], name)
        return name

    def exec_client(self, name, args, deadline=60, check=True):
        if name not in self.clients:
            raise ValueError("client does not belong to this lane")
        return self._call("exec", name, *args, timeout=deadline, check=check)

    def run_client(self, image_id, args, mounts=(), deadline=60):
        name = self.spawn_client(image_id, args, mounts)
        try:
            result = self._call("wait", name, timeout=deadline)
            logs = self._call("logs", name)
            if result.stdout.strip() != "0":
                raise RuntimeError(
                    f"client failed with exit {result.stdout.strip()}: {logs.stderr}"
                )
            return logs
        finally:
            if getattr(self, "retain_diagnostics", False):
                self._archive_owned(name)
                self.clients.discard(name)
            else:
                removed = self._call("rm", "--force", name, check=False)
                if removed.returncode == 0 or "No such container" in removed.stderr:
                    self.clients.discard(name)
                else:
                    # Retain ownership after an unacknowledged removal.
                    raise RuntimeError(
                        "client cleanup incomplete: " + removed.stderr.strip()
                    )

    def verify_retained(self) -> dict[str, Any]:
        if not getattr(self, "retain_diagnostics", False):
            raise RuntimeError("retention verification requires explicit opt-in")
        if self.retention_error:
            raise RuntimeError(self.retention_error)
        if self.clients:
            raise RuntimeError("unreconciled tracked clients remain")
        if set(self.archived_containers) != self.owned_containers:
            raise RuntimeError("retained container inventory incomplete")
        for receipt in self.archived_containers.values():
            verify_retained_container(receipt)
        auxiliary = {}
        identity_receipts: dict[str, dict[str, Any]] = {}
        for kind, name in (("volume", self.volume), ("network", self.network)):
            original = self.auxiliary_identities.get(kind)
            record = self._observe_auxiliary(kind, name)
            if record is None:
                if original is not None:
                    raise RuntimeError(
                        "original retained auxiliary resource disappeared"
                    )
                auxiliary[kind] = "not_created"
                identity_receipts[kind] = {"baseline": "not_created"}
                continue
            identity = auxiliary_identity(kind, record)
            if original is not None and identity != original:
                raise RuntimeError("original retained auxiliary identity changed")
            auxiliary[kind] = "retained"
            identity_receipts[kind] = {
                "identity": identity,
                "baseline": "creation_observation"
                if original is not None
                else "first_observed_after_partial_creation",
            }
        return {
            "classification": "stopped_execution_retained_diagnostics",
            "artifacts": auxiliary,
            "auxiliary_identity_receipts": identity_receipts,
            "creation_identities_confirmed": set(self.auxiliary_identities)
            == {"volume", "network"},
            "containers": list(self.archived_containers),
            "evidence": str(self.diagnostics),
        }

    def close_retained(self) -> None:
        if self.retention_error:
            raise RuntimeError(self.retention_error)
        errors = []
        if self._created:
            # Preserve the documented raw-client API: names tracked before creation.
            self.owned_containers.update(self.clients)
            for name in sorted(self.owned_containers):
                if name in self.archived_containers:
                    continue
                try:
                    self.archived_containers[name] = archive_container(
                        name, self.diagnostics
                    )
                    self.clients.discard(name)
                except (
                    OSError,
                    RuntimeError,
                    subprocess.SubprocessError,
                    ValueError,
                ) as error:
                    errors.append(str(error))
        for sig, handler in self._handlers.items():
            signal.signal(sig, handler)
        self._handlers = {}
        if errors:
            self.retention_error = (
                "lane retention incomplete; no automatic retry: " + "; ".join(errors)
            )
            raise RuntimeError(self.retention_error)
        try:
            receipt = self.verify_retained()
            diagnostic_capture(
                self.diagnostics,
                "cleanup-" + uuid.uuid4().hex + ".json",
                json.dumps(receipt, sort_keys=True).encode(),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            self.retention_error = (
                "lane retention verification failed; no automatic retry: " + str(error)
            )
            raise RuntimeError(self.retention_error) from error
        self._created = False

    def close(self):
        if getattr(self, "retain_diagnostics", False):
            return self.close_retained()
        errors = []
        if self._created:
            for args in [
                ("rm", "--force", *sorted(self.clients), self.server),
                ("volume", "rm", "--force", self.volume),
                ("network", "rm", self.network),
            ]:
                try:
                    result = self._call(*args, check=False)
                    # A missing resource after a lost reply is already reconciled.
                    if result.returncode and (
                        not result.stderr.strip()
                        or not all(
                            "No such" in line or "not found" in line
                            for line in result.stderr.splitlines()
                        )
                    ):
                        errors.append(result.stderr.strip())
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append(str(exc))
        self._created = False
        for sig, handler in self._handlers.items():
            signal.signal(sig, handler)
        self._handlers = {}
        if errors:
            raise RuntimeError("lane cleanup incomplete: " + "; ".join(errors))


def smoke(
    directory: str | pathlib.Path, retain_diagnostics: bool | None = None
) -> dict[str, Any]:
    """No worker involved: independently observe persisted workflow history."""
    with Lane(directory, retain_diagnostics=retain_diagnostics) as lane:
        workflow = "synthetic-persistence-" + uuid.uuid4().hex
        lane.cli(
            "workflow",
            "start",
            "--workflow-id",
            workflow,
            "--type",
            "SyntheticUnpolled",
            "--task-queue",
            "synthetic-unpolled",
        )
        before = json.loads(
            lane.cli(
                "workflow", "show", "--workflow-id", workflow, "--output", "json"
            ).stdout
        )
        lane.restart(abrupt=True)
        after = json.loads(
            lane.cli(
                "workflow", "show", "--workflow-id", workflow, "--output", "json"
            ).stdout
        )
        if before != after:
            raise AssertionError(
                "workflow history changed across unpolled server restart"
            )
        server = json.loads(docker("inspect", lane.server).stdout)[0]
        network = json.loads(docker("network", "inspect", lane.network).stdout)[0]
        assert network["Internal"] is True
        assert (
            network["Options"]["com.docker.network.bridge.gateway_mode_ipv4"]
            == "isolated"
        )
        assert not server["HostConfig"]["PortBindings"]
        assert len(server["NetworkSettings"]["Networks"]) == 1
        assert server["HostConfig"]["ReadonlyRootfs"] is True
        observation = {
            "status": "passed",
            "evidence_class": "isolated_temporal_development_integration",
            "assertions": [
                "real readiness",
                "workflow history persisted across killed server",
                "internal network only",
                "no host port bindings",
                "read-only root",
            ],
            "workflow_events": len(after.get("events", [])),
            "image": lane.image,
        }
        resources = [lane.server, lane.network, lane.volume]
    if lane.retain_diagnostics:
        receipt = lane.verify_retained()
        if (
            receipt["artifacts"] != {"volume": "retained", "network": "retained"}
            or not receipt["creation_identities_confirmed"]
        ):
            raise AssertionError(
                "real Temporal artifacts or their creation identities were not retained"
            )
        observation["cleanup"] = receipt["classification"]
        observation["retention"] = receipt
    else:
        for kind, name in zip(("container", "network", "volume"), resources):
            if docker(kind, "inspect", name, check=False).returncode == 0:
                raise AssertionError("resource survived cleanup: " + name)
        observation["cleanup"] = "independently_absent"
    return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "smoke"))
    parser.add_argument("--directory", default="local/temporal-prepared")
    parser.add_argument("--architecture", choices=("arm64", "amd64"), default="arm64")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="require staged verified bytes; never download",
    )
    parser.add_argument(
        "--retain-diagnostics",
        action="store_true",
        default=None,
        help="retain stopped containers, original volume and verified logs",
    )
    options = parser.parse_args()
    try:
        result = (
            prepare(
                options.directory,
                options.architecture,
                options.offline,
                options.retain_diagnostics,
            )
            if options.action == "prepare"
            else smoke(options.directory, options.retain_diagnostics)
        )
        print(json.dumps(result, sort_keys=True))
    except (Blocked, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "reason": str(exc), "live_acceptance": "not_run"}
            )
        )
        raise SystemExit(78)


if __name__ == "__main__":
    main()
