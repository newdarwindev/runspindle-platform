"""Disposable QEMU lifecycle. All external mutations follow a durable reservation."""

import base64
import hashlib
import json
import os
import re
import resource as process_resource
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

from .retention import (
    capture_bytes,
    capture_file,
    file_digest,
    snapshot_guest_disk,
    sync_directory,
)

ROOT = Path("/var/lib/runspindle-fleet")
HELPER = "/opt/runspindle-fleet/tools/runner_fleet/host_helper.py"
GUEST_DISK_GIB = 24


def execute(
    argv: list[str],
    *,
    timeout: float = 60,
    input: str | None = None,
    check: bool = True,
    output_limit: int | None = None,
    retained_output_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if retained_output_directory is not None and output_limit is None:
        raise ValueError("Raw nonsecret output retention requires an explicit bound")
    if output_limit is None:
        result = subprocess.run(
            argv, input=input, text=True, capture_output=True, timeout=timeout
        )
    else:
        # Guest-controlled stdout must not become unbounded host memory/disk.
        # RLIMIT_FSIZE applies to the host SSH process, never guest workloads.
        with ExitStack() as stack:
            generation = None
            if retained_output_directory is None:
                stdout = stack.enter_context(tempfile.TemporaryFile())
                stderr = stack.enter_context(tempfile.TemporaryFile())
            else:
                generation = (
                    retained_output_directory / f"preparation-output-{uuid.uuid4().hex}"
                )
                generation.mkdir(mode=0o700)
                stdout = stack.enter_context(
                    os.fdopen(
                        os.open(
                            generation / "stdout.log",
                            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        ),
                        "w+b",
                    )
                )
                stderr = stack.enter_context(
                    os.fdopen(
                        os.open(
                            generation / "stderr.log",
                            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        ),
                        "w+b",
                    )
                )

            def bounded_files() -> None:
                process_resource.setrlimit(
                    process_resource.RLIMIT_FSIZE, (output_limit, output_limit)
                )

            completed = None
            error_kind = None
            try:
                completed = subprocess.run(
                    argv,
                    input=input,
                    text=True,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout,
                    preexec_fn=bounded_files,
                )
                if (
                    max(
                        os.fstat(stdout.fileno()).st_size,
                        os.fstat(stderr.fileno()).st_size,
                    )
                    >= output_limit
                ):
                    raise RuntimeError("Guest diagnostic output limit exceeded")
                stdout.seek(0)
                stderr.seek(0)
                result = subprocess.CompletedProcess(
                    argv,
                    completed.returncode,
                    stdout.read(output_limit).decode("utf-8", errors="replace"),
                    stderr.read(output_limit).decode("utf-8", errors="replace"),
                )
            except BaseException as error:
                error_kind = type(error).__name__
                raise
            finally:
                if generation is not None:
                    for stream in (stdout, stderr):
                        stream.flush()
                        os.fsync(stream.fileno())
                    capture_bytes(
                        generation,
                        "receipt.json",
                        json.dumps(
                            {
                                "scope": "raw nonsecret preparation output; no stdin or argv captured",
                                "exception": error_kind,
                                "returncode": completed.returncode
                                if completed is not None
                                else None,
                                "output_limit": output_limit,
                                "stdout_bytes": os.fstat(stdout.fileno()).st_size,
                                "stderr_bytes": os.fstat(stderr.fileno()).st_size,
                                "stdout_sha256": file_digest(generation / "stdout.log"),
                                "stderr_sha256": file_digest(generation / "stderr.log"),
                                "complete_within_bound": error_kind is None,
                            },
                            sort_keys=True,
                        ).encode(),
                    )
    if check and result.returncode:
        # Never embed input (registration token) or argv in an exception.
        raise RuntimeError(
            f"{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-2000:]}"
        )
    return result


def helper(command: str, slot: int | None = None, identity: str | None = None) -> Any:
    argv = ["sudo", "-n", "python3", HELPER, command]
    if slot is not None:
        argv.append(str(slot))
    if identity is not None:
        argv.append(identity)
    return json.loads(execute(argv).stdout)


def digest(path: str | os.PathLike[str]) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def frozen_preparation_text(source: Path, revision: str, relative: str) -> str:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", revision)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise RuntimeError("Exact committed preparation source required")
    return subprocess.check_output(
        ["git", "-C", str(source), "show", f"{revision}:{relative}"], text=True
    )


def image_binding(
    image: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    image_sha: str,
    *,
    candidate: bool,
) -> dict[str, str]:
    manifest = json.loads(Path(manifest_path).read_text())
    if candidate and str(image) != manifest.get("candidate_path"):
        raise RuntimeError("Candidate image path differs from recorded manifest")
    if manifest.get("sha256") != image_sha:
        raise RuntimeError("Image digest differs from recorded manifest")
    values = {}
    for field in ("controller_sha256", "recipe_sha256", "tool_closure_sha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RuntimeError("Missing exact image binding: " + field)
        values[field] = value
    source = manifest.get("source_sha")
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise RuntimeError("Missing exact image source revision")
    values["image_source_sha"] = source
    return values


class Backend:
    def __init__(
        self,
        state: Any,
        image: str | os.PathLike[str] | None = None,
        *,
        preparation_source_sha: str | None = None,
        image_manifest: str | os.PathLike[str] | None = None,
    ) -> None:
        self.state = state
        self.image = Path(image or ROOT / "images/qualified.qcow2")
        self.preparation_source_sha = preparation_source_sha
        self.image_manifest = (
            Path(image_manifest) if image_manifest is not None else None
        )

    def directory(self, resource: dict[str, Any]) -> Path:
        path = self.state.directory / "resources" / resource["id"]
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cast(Path, path)

    def slot(self, resource: dict[str, Any]) -> Path:
        return ROOT / "slots" / str(resource["slot"])

    def ssh(
        self,
        resource: dict[str, Any],
        script: str,
        timeout: float = 30,
        check: bool = True,
        *,
        retain_nonsecret_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # Raw retention is opt-in only for known nonsecret preparation scripts.
        # Registration keeps its separate redacted capture; stdin is never logged.
        directory = self.directory(resource)
        return execute(
            [
                "ssh",
                "-F",
                "/dev/null",
                "-i",
                str(directory / "key"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "UserKnownHostsFile=" + str(directory / "known_hosts"),
                "-o",
                "ConnectTimeout=3",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ClearAllForwardings=yes",
                f"runner@10.231.{resource['slot']}.2",
                "bash",
                "-s",
            ],
            input=script,
            timeout=timeout,
            check=check,
            output_limit=32 * 1024**2,
            retained_output_directory=directory if retain_nonsecret_output else None,
        )

    def copy_prepared(
        self, resource: dict[str, Any], source: str | os.PathLike[str], relative: str
    ) -> None:
        """Transfer one preverified archive through the guest's existing SSH identity."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", relative):
            raise ValueError("unsafe preparation destination")
        source = Path(source)
        if not source.is_file() or source.is_symlink():
            raise ValueError("missing preparation archive")
        directory = self.directory(resource)
        target = "/home/runner/preparation/" + relative
        parent = target.rsplit("/", 1)[0]
        self.ssh(resource, "mkdir -p " + shlex.quote(parent), timeout=15)
        result = execute(
            [
                "scp",
                "-F",
                "/dev/null",
                "-i",
                str(directory / "key"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(directory / "known_hosts"),
                "-o",
                "ConnectTimeout=3",
                "-o",
                "ClearAllForwardings=yes",
                str(source),
                f"runner@10.231.{resource['slot']}.2:{target}",
            ],
            timeout=600,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("verified preparation transfer failed")

    def create(self, resource, prepare=False, *, network_mode="provision"):
        if network_mode not in ("provision", "verify"):
            raise ValueError("Unsupported network lifecycle")
        if prepare and (
            not isinstance(self.preparation_source_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", self.preparation_source_sha)
        ):
            raise RuntimeError(
                "Preparation requires frozen source revision before effects"
            )
        directory = self.directory(resource)
        slot = self.slot(resource)
        image_sha = digest(self.image)
        if resource.get("details", {}).get(
            "automatic_fleet_v1"
        ) is True and image_sha != resource["details"].get("image_sha256"):
            raise RuntimeError("Automatic image differs from reserved qualification")
        instance = {
            "resource": resource["id"],
            "slot": resource["slot"],
            "image_sha256": image_sha,
        }
        if prepare:
            instance["preparation"] = True
        if not prepare:
            if (
                self.image.is_symlink()
                or self.image.stat().st_uid != 0
                or self.image.stat().st_mode & 0o222
            ):
                raise RuntimeError("Unsealed guest image refused")
            qualified = ROOT / "images/qualified.qcow2"
            path = (
                ROOT / "images/qualified.json"
                if self.image == qualified
                else self.state.directory / "candidate.json"
            )
            path = getattr(self, "image_manifest", None) or path
            instance.update(
                image_binding(
                    self.image, path, image_sha, candidate=self.image != qualified
                )
            )
        self.state.update(
            resource,
            "starting",
            image_sha256=image_sha,
            boot_deadline=time.time() + 240,
        )
        # Automatic callers must opt into read-only verification; legacy explicit
        # provisioning remains separate until independently reviewed deployment.
        helper("verify-network" if network_mode == "verify" else "network")
        observed = helper("observe", resource["slot"])
        if observed.get("ActiveState") in ("active", "activating", "deactivating"):
            raise RuntimeError("Unreconciled occupied slot")
        execute(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(directory / "key"),
            ]
        )
        if (slot / "disk.qcow2").exists() or (slot / "seed.iso").exists():
            raise RuntimeError("Slot contains unreconciled files")
        execute(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(self.image),
                str(slot / "disk.qcow2"),
                f"{GUEST_DISK_GIB}G",
            ]
        )
        proxy = f"http://10.231.{resource['slot']}.1:3128"
        cloud = {
            "hostname": "disposable-" + resource["id"][:12],
            "manage_etc_hosts": True,
            "ssh_pwauth": False,
            "disable_root": True,
            "apt": {"preserve_sources_list": True},
            "users": [
                {
                    "name": "runner",
                    "shell": "/bin/bash",
                    "lock_passwd": True,
                    "sudo": ["ALL=(ALL) NOPASSWD:ALL"] if prepare else [],
                    "ssh_authorized_keys": [
                        (directory / "key.pub").read_text().strip()
                    ],
                }
            ],
            "write_files": [
                {
                    "path": "/etc/runspindle-instance.json",
                    "content": json.dumps(instance),
                },
                {
                    "path": "/etc/environment",
                    "content": f"http_proxy={proxy}\nhttps_proxy={proxy}\nHTTP_PROXY={proxy}\nHTTPS_PROXY={proxy}\nno_proxy=localhost,127.0.0.1\nNO_PROXY=localhost,127.0.0.1\n",
                },
            ],
            "runcmd": [["sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=1"]],
        }
        if prepare:
            source = Path(__file__).resolve().parents[2]
            for relative in (
                "tools/temporal_lane.py",
                "temporal/dependencies.json",
                "temporal/LICENSE-CLI",
                "temporal/LICENSE-SERVER",
                "tools/runner_fleet/prepare_temporal.sh",
                "tools/runner_fleet/seal_preparation_guest.py",
                "tests/test_temporal_lane.py",
                "fleet/image-inputs.json",
                "fleet/quality-closure.json",
                "fleet/rust-closure.json",
                "fleet/builder-closure.json",
                "fleet/builder-x64.Containerfile",
                "requirements-quality.txt",
                "tools/runner_fleet/quality.py",
                "tools/runner_fleet/staging.py",
                "tools/runner_fleet/__init__.py",
            ):
                cloud["write_files"].append(
                    {
                        "path": "/opt/runspindle-image-tools/" + relative,
                        "content": frozen_preparation_text(
                            source, str(self.preparation_source_sha), relative
                        ),
                        "permissions": "0644",
                    }
                )
        network = {
            "version": 2,
            "ethernets": {
                "fleet": {
                    "match": {"macaddress": "52:54:00:23:10:0" + str(resource["slot"])},
                    "set-name": "fleet0",
                    "addresses": [f"10.231.{resource['slot']}.2/30"],
                    "routes": [
                        {"to": "default", "via": f"10.231.{resource['slot']}.1"}
                    ],
                    "dhcp4": False,
                    "dhcp6": False,
                    "accept-ra": False,
                }
            },
        }
        (directory / "user-data").write_text("#cloud-config\n" + json.dumps(cloud))
        (directory / "meta-data").write_text(
            json.dumps({"instance-id": resource["id"]})
        )
        (directory / "network-config").write_text(json.dumps(network))
        execute(
            [
                "cloud-localds",
                "--network-config=" + str(directory / "network-config"),
                str(slot / "seed.iso"),
                str(directory / "user-data"),
                str(directory / "meta-data"),
            ]
        )
        helper("start", resource["slot"], resource["id"])

    def ready(self, resource):
        result = self.ssh(
            resource,
            "test -f /var/lib/cloud/instance/boot-finished\n",
            timeout=10,
            check=False,
        )
        return result.returncode == 0

    def isolation(self, resource, check_tooling=True):
        # Both canaries must be alive from host before denial is meaningful.
        for address, port in [
            (f"10.231.{resource['slot']}.1", 18081),
            ("198.18.231.2", 18082),
        ]:
            with socket.create_connection((address, port), timeout=3):
                pass
        script = """python3 - <<'GUEST'
import json, os, pathlib, socket
checks = {}
for name, address, port in [
    ('host', 'GATEWAY', 18081), ('lan', '198.18.231.2', 18082),
    ('management', 'GATEWAY', 22), ('peer', 'PEER', 22),
    ('internet', '1.1.1.1', 443), ('dns', '1.1.1.1', 53)]:
    try:
        with socket.create_connection((address, port), timeout=1): pass
        checks[name] = False
    except OSError:
        checks[name] = True
mounts = pathlib.Path('/proc/mounts').read_text()
checks['no_host_mounts'] = not any(x in mounts for x in ['9p', 'virtiofs', '/Users/', '/home/standard'])
checks['no_management_credentials'] = not any(pathlib.Path(x).exists() for x in [
 '/home/runner/.config/gh/hosts.yml', '/home/runner/.ssh/id_ed25519'])
checks['fresh_workdir'] = not pathlib.Path('/home/runner/runner/_work').exists()
checks['ipv6_disabled'] = pathlib.Path('/proc/sys/net/ipv6/conf/all/disable_ipv6').read_text().strip() == '1'
print(json.dumps(checks))
raise SystemExit(0 if all(checks.values()) else 2)
GUEST
""".replace("GATEWAY", f"10.231.{resource['slot']}.1").replace(
            "PEER", f"10.231.{1 - resource['slot']}.2"
        )
        result = self.ssh(resource, script, timeout=25, check=False)
        (self.directory(resource) / "isolation.json").write_text(result.stdout)
        if result.returncode:
            raise RuntimeError("Guest isolation or freshness probe failed")
        checks = json.loads(result.stdout)
        if check_tooling:
            self.ssh(resource, "docker buildx inspect >/dev/null", timeout=30)
            checks["guest_docker_builder"] = True
        self.state.update(resource, "isolated", isolation=checks)

    def register(self, resource, repository, token, labels, *, no_default_labels=False):
        if type(no_default_labels) is not bool:
            raise ValueError("Invalid default-label policy")
        label_mode = " --no-default-labels" if no_default_labels else ""
        # Host account token stays in gh. Only repository registration token
        # crosses SSH stdin; neither command arguments nor diagnostic logs carry it.
        script = f"""set -e
cd /home/runner/runner
set -a
. /etc/environment
set +a
export PATH=/opt/runspindle-quality/bin:$PATH
./config.sh --unattended --ephemeral --disableupdate --url https://github.com/{repository} --token {shlex.quote(token)} --name {resource["runner_name"]}{label_mode} --labels {shlex.quote(",".join(labels))} --work _work
nohup env -u SSH_AUTH_SOCK ./run.sh > /home/runner/job.log 2>&1 < /dev/null &
echo $! > /home/runner/runner.pid
"""
        result = self.ssh(resource, script, timeout=90, check=False)
        capture_bytes(
            self.directory(resource),
            "registration.log",
            (
                result.stdout.replace(token, "[REDACTED]")
                + result.stderr.replace(token, "[REDACTED]")
            ).encode(),
        )
        if result.returncode:
            raise RuntimeError(
                "Runner registration failed; retained redacted diagnostics"
            )

    def runner_exited(self, resource):
        return (
            self.ssh(
                resource,
                'test -f /home/runner/runner.pid && ! kill -0 "$(cat /home/runner/runner.pid)" 2>/dev/null',
                timeout=10,
                check=False,
            ).returncode
            == 0
        )

    def diagnostics(self, resource):
        directory = self.directory(resource)
        serial = self.slot(resource) / "serial.log"
        if serial.exists():
            capture_file(serial, directory, "serial.log")
        try:
            result = self.ssh(
                resource,
                "cat /home/runner/job.log 2>/dev/null; tail -n 300 /home/runner/runner/_diag/*.log 2>/dev/null",
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                capture_bytes(
                    directory,
                    "guest-diagnostics.log",
                    (result.stdout + result.stderr).encode(),
                )
                archive = self.ssh(
                    resource,
                    "set -o pipefail; tar -czf - -C /home/runner runner/_diag job.log 2>/dev/null | base64 -w0",
                    timeout=25,
                    check=False,
                )
                if archive.returncode == 0:
                    capture_bytes(
                        directory,
                        "guest-diagnostics.tar.gz",
                        base64.b64decode(archive.stdout, validate=True),
                    )
            else:
                with (directory / "diagnostic-errors.log").open("a") as stream:
                    stream.write(result.stderr)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        execute(
            [
                "sudo",
                "-n",
                "journalctl",
                "-u",
                f"runspindle-vm-{resource['slot']}.service",
                "--no-pager",
                "-n",
                "200",
            ],
            check=False,
        )

    def destroy(self, resource):
        helper("stop", resource["slot"], resource["id"])
        directory = self.directory(resource)
        disk = self.slot(resource) / "disk.qcow2"
        if disk.is_symlink():
            raise RuntimeError("Linked guest disk retained; cleanup blocked")
        if disk.exists():
            expected_base = resource.get("details", {}).get("image_sha256")
            if not isinstance(expected_base, str):
                raise RuntimeError("Missing reserved guest image identity")
            snapshot_guest_disk(
                disk,
                self.image,
                directory,
                expected_base,
                lambda argv: execute(argv, timeout=600, check=False),
                maximum_size=GUEST_DISK_GIB * 1024**3,
                original_directory=ROOT / "retained" / resource["id"],
            )
        serial = self.slot(resource) / "serial.log"
        if serial.is_symlink():
            raise RuntimeError("Linked serial log retained; cleanup blocked")
        if serial.exists():
            captured = capture_file(serial, directory, "serial.log")
            expected = digest(serial)
            if digest(captured) != expected:
                raise RuntimeError("Stopped serial log changed; cleanup blocked")
            originals = ROOT / "retained" / resource["id"]
            original = originals / f"serial-original-{uuid.uuid4().hex}.log"
            if original.exists() or original.is_symlink():
                raise RuntimeError("Retained serial destination occupied")
            # Keep the original inode on the slot's mount, plus the verified
            # state capture. Never copy/delete as a cross-mount fallback.
            serial.rename(original)
            if digest(original) != expected:
                raise RuntimeError("Retained serial log changed; cleanup blocked")
            sync_directory(originals)
            sync_directory(serial.parent)
            capture_bytes(
                directory,
                "retained-serial.json",
                json.dumps({"original": str(original), "sha256": expected}).encode(),
            )
        seed = self.slot(resource) / "seed.iso"
        if seed.is_symlink():
            raise RuntimeError("Linked seed retained; cleanup blocked")
        if seed.exists():
            expected = file_digest(seed)
            originals = ROOT / "retained" / resource["id"]
            original = originals / f"seed-original-{uuid.uuid4().hex}.iso"
            seed.rename(original)
            if file_digest(original) != expected:
                raise RuntimeError("Retained seed changed; cleanup blocked")
            sync_directory(originals)
            sync_directory(seed.parent)
            capture_bytes(
                directory,
                "retained-seed.json",
                json.dumps({"original": str(original), "sha256": expected}).encode(),
            )
        if any(self.slot(resource).iterdir()):
            raise RuntimeError("Unknown slot files retained; capacity remains occupied")
        directory = self.directory(resource)
        key = directory / "key"
        if key.is_symlink():
            raise RuntimeError("Linked ephemeral key retained; cleanup blocked")
        if key.exists():
            expected = file_digest(key)
            original_key = directory / f"key-original-{uuid.uuid4().hex}"
            key.rename(original_key)
            if file_digest(original_key) != expected:
                raise RuntimeError("Retained ephemeral key changed; cleanup blocked")
            sync_directory(directory)
            capture_bytes(
                directory,
                "retained-key.json",
                json.dumps(
                    {"original": str(original_key), "sha256": expected}
                ).encode(),
            )
        # No claim of cleanup until service, files and registrations are gone.
