#!/usr/bin/env python3
"""Bounded disposable Temporal development lane; no implicit downloads at runtime."""

from __future__ import annotations

import argparse
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
    directory: str | pathlib.Path, architecture: str, offline: bool = False
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

    def __init__(self, prepared_dir, readiness_deadline=60):
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
        self.clients = set()
        self.readiness_deadline = float(readiness_deadline)
        if not 0 < self.readiness_deadline <= 180:
            raise ValueError("readiness deadline must be in (0,180] seconds")
        self._handlers = {}
        self._created = False

    @staticmethod
    def _image(image):
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
            docker(
                "network",
                "create",
                "--internal",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                "--label",
                "runspindle.synthetic=true",
                self.network,
            )
            docker(
                "volume", "create", "--label", "runspindle.synthetic=true", self.volume
            )
            self._start_server()
        except BaseException:
            self.close()
            raise
        return self

    def _limits(self, name, image, mounts=()):
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
        docker(*args)
        deadline = time.monotonic() + self.readiness_deadline
        while time.monotonic() < deadline:
            result = docker(
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
                docker(
                    "inspect", "--format", "{{.State.Running}}", self.server
                ).stdout.strip()
                != "true"
            ):
                raise Blocked("Temporal server exited before readiness")
            time.sleep(0.1)
        raise Blocked("Temporal readiness deadline exceeded")

    def restart(self, abrupt=False):
        if not self._created:
            raise RuntimeError("lane not running")
        if abrupt:
            docker("kill", self.server)
        else:
            docker("stop", "--time", "10", self.server, timeout=15)
        docker("rm", "--force", self.server)
        self._start_server()

    def cli(self, *args, timeout=30, check=True):
        return docker(
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
        docker(*self._limits(name, image_id, mounts), image_id, *args)
        return name

    def exec_client(self, name, args, deadline=60, check=True):
        if name not in self.clients:
            raise ValueError("client does not belong to this lane")
        return docker("exec", name, *args, timeout=deadline, check=check)

    def run_client(self, image_id, args, mounts=(), deadline=60):
        name = self.spawn_client(image_id, args, mounts)
        try:
            result = docker("wait", name, timeout=deadline)
            logs = docker("logs", name)
            if result.stdout.strip() != "0":
                raise RuntimeError(
                    f"client failed with exit {result.stdout.strip()}: {logs.stderr}"
                )
            return logs
        finally:
            removed = docker("rm", "--force", name, check=False)
            if removed.returncode == 0 or "No such container" in removed.stderr:
                self.clients.discard(name)
            else:
                # Retain ownership for context cleanup; an unacknowledged delete
                # must never turn a surviving client into an untracked process.
                raise RuntimeError(
                    "client cleanup incomplete: " + removed.stderr.strip()
                )

    def close(self):
        errors = []
        if self._created:
            for args in [
                ("rm", "--force", *sorted(self.clients), self.server),
                ("volume", "rm", "--force", self.volume),
                ("network", "rm", self.network),
            ]:
                try:
                    result = docker(*args, check=False)
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


def smoke(directory: str | pathlib.Path) -> dict[str, Any]:
    """No worker involved: independently observe persisted workflow history."""
    with Lane(directory) as lane:
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
    options = parser.parse_args()
    try:
        result = (
            prepare(options.directory, options.architecture, options.offline)
            if options.action == "prepare"
            else smoke(options.directory)
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
