"""Read-only host probes. Missing or unverifiable prerequisites block install."""

import os
from datetime import datetime, timezone
import platform
import re
import shutil
import subprocess
from pathlib import Path


def run(argv, timeout=20):
    return subprocess.run(
        argv, text=True, capture_output=True, timeout=timeout, check=False
    )


def inspect_host(cfg, host, state):
    checks = []

    def record(name, ok, detail):
        checks.append(
            {"check": name, "status": "passed" if ok else "blocked", "detail": detail}
        )

    system, machine = platform.system(), platform.machine()
    expected = ("Linux", "x86_64") if host == "strix" else ("Darwin", "arm64")
    record(
        "host_identity",
        (system, machine) == expected,
        f"observed {system}/{machine}; required {expected[0]}/{expected[1]}",
    )
    tools = (
        ["gh", "qemu-system-x86_64", "qemu-img", "nft"]
        if host == "strix"
        else ["gh", "limactl", "tart"]
    )
    for tool in tools:
        record("tool:" + tool, shutil.which(tool) is not None, tool)
    if host == "strix":
        record(
            "kvm",
            os.access("/dev/kvm", os.R_OK | os.W_OK),
            "/dev/kvm must be readable and writable",
        )
    if host == "macbook" and system == "Darwin":
        try:
            hv = run(["sysctl", "-n", "kern.hv_support"])
            record(
                "virtualization",
                hv.returncode == 0 and hv.stdout.strip() == "1",
                "Apple Hypervisor framework support",
            )
        except (OSError, subprocess.TimeoutExpired):
            record("virtualization", False, "cannot probe Apple Hypervisor framework")
    try:
        free_disk = shutil.disk_usage(state).free / 1024**3
        record(
            "disk",
            free_disk >= cfg["hosts"][host]["minimum_disk_gib"],
            f"{free_disk:.1f} GiB free",
        )
    except OSError:
        record("disk", False, "cannot inspect state volume")
    try:
        if system == "Linux":
            mem = dict(
                line.split(":", 1)
                for line in Path("/proc/meminfo").read_text().splitlines()
            )
            available = int(mem["MemAvailable"].split()[0]) / 1024**2
        elif system == "Darwin":
            # Exclude speculative/compressed pages; conservatively count only
            # free and inactive pages as reclaimable.
            vm = run(["vm_stat"])
            lines = vm.stdout.splitlines()
            page = int(re.search(r"page size of (\d+) bytes", lines[0]).group(1))
            counts = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            available = (
                sum(
                    int(counts.get(key, "0").strip().rstrip("."))
                    for key in ("Pages free", "Pages inactive")
                )
                * page
                / 1024**3
            )
        else:
            available = 0
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        AttributeError,
        subprocess.TimeoutExpired,
    ):
        available = 0
    lanes = [lane for lane in cfg["lanes"].values() if lane["host"] == host]
    required = (
        cfg["hosts"][host]["reserve_memory_gib"]
        + max(lane["memory_gib"] for lane in lanes) * cfg["hosts"][host]["capacity"]
    )
    record(
        "memory",
        available >= required,
        f"{available:.1f} GiB available; {required} GiB required including reserve",
    )
    if shutil.which("gh"):
        for repo in cfg["repositories"]:
            try:
                result = run(
                    [
                        "gh",
                        "api",
                        f'repos/{cfg["owner"]}/{repo}',
                        "--jq",
                        ".permissions.admin",
                    ]
                )
                record(
                    "admin:" + repo,
                    result.returncode == 0 and result.stdout.strip() == "true",
                    (
                        "repository administration access verified"
                        if result.stdout.strip() == "true"
                        else "administration access unavailable"
                    ),
                )
            except (OSError, subprocess.TimeoutExpired):
                record("admin:" + repo, False, "GitHub probe unavailable or timed out")
    # These must be exercised by the real service/backend. A JSON assertion or
    # an interactive gh session must never stand in for isolation verification.
    for name, detail in {
        "background_credentials": "not exercised under systemd/launchd; interactive gh authentication is insufficient",
        "pinned_images": "no verified guest image/toolchain/runner lock has been prepared",
        "licenses": "guest image and installed tool licenses not recorded",
        "network_isolation": "no host-enforced allowlist and denied host/LAN canary evidence",
        "disposable_backend": "QEMU/Lima/Tart lifecycle drivers are not implemented",
    }.items():
        record(name, False, detail)
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "status": (
            "blocked" if any(c["status"] == "blocked" for c in checks) else "passed"
        ),
        "checks": checks,
    }
