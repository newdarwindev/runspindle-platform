"""Dispatch exact GitHub jobs to disposable Strix guests under the fleet lock."""

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, nullcontext
import fcntl
from functools import lru_cache
import hashlib
from itertools import islice
import json
from multiprocessing import get_context
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import shlex
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

from tools.runner_fleet.direct_routing import enroll_job, queued_jobs
from tools.runner_fleet.ephemeral_registration import LABEL, configure, start_listener
from tools.runner_fleet.github_jobs import DEMO, DIRECT_REPOSITORIES, LAB, NEXT, Route, RoutingError, inventory

SUPPORT = Path("/home/standard/Projects/runspindle-platform-automatic-fleet-20261118")
LIGHT_SUPPORT = Path("/home/standard/Projects/runspindle-platform-strix-three-slot-support-20261120")
LIGHT_SUPPORT_SHA = "51a31d553af6ae0b88aac4a2e393d4daf5396f07"
LIGHT_SLOT_FLAG = Path("/etc/runspindle-fleet/enable-light-slot")
LIGHT_WORKFLOWS = {
    NEXT: "43ee0025394d8d2d4cd3d69f4822381d541b1fe4",
    LAB: "fb048fa31ecec128f2518f847eb67d04307e3705",
}
LIGHT_AGGREGATE_WORKFLOWS = {
    "newdarwindev/runspindle": "bba7664a55dc961f339579c84bdcbde86af720c9",
    "newdarwindev/runspindle-browser": "ef7080f2f8d334f5e9353e06fe187afd8e1065b1",
    "newdarwindev/runspindle-console": "dcd890f5ade00e4c9a77cf511d6cedac38303597",
    "newdarwindev/runspindle-core": "df2d28230a3388c4b7e8e007f080551bfbfcd9cb",
    "newdarwindev/runspindle-host": "1224fd80383058a9c4eafad9bfdab8410cd56933",
    "newdarwindev/runspindle-tether": "b95cf4d906807224abb27891691a3e561c0338c2",
}
# Additional Tether workflows with the same reviewed aggregate step.
LIGHT_TETHER_AGGREGATE_HEADS = {
    "d86b2440139eea7bc522eb4dfc3920cd759bec7e": "60e758b5b8ac682d7fb2c0763aac889cd8080edd",
    "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a": "cb4d52bf0714cd065b439d515591a2a568b76544",
}
FLEET_STATE = Path("/home/standard/.local/state/runspindle-fleet")
GIB = 1024**3
# Keep the established two-slot floor.
PARALLEL_DISK_RESERVE = 82 * GIB
# Reserve three fully allocated 24 GiB guest disks, 1 GiB per guest for
# overhead, and another full disk's worth of shared thin-pool headroom.
THREE_SLOT_DISK_RESERVE = (3 * (24 + 1) + 24) * GIB
LIGHT_MEMORY_LIMIT = 2560 * 1024**2
GUEST_MEMORY_LIMIT = 6656 * 1024**2
COMPACT_MEMORY_LIMIT = 4608 * 1024**2
COMPACT_PROFILE_FLAG = Path("/etc/runspindle-fleet/enable-compact-profile")
PARALLEL_HOST_MEMORY_RESERVE = 2 * GIB
VACANT_SLOT_POLL_SECONDS = 60
CAPACITY_DEFERRED = 4
CAPACITY_RECHECK = 5
MEMORY_DEFERRED = 6
CAPACITY_RECHECK_SECONDS = 15
RAM_RECHECK_SHORTFALL = 768 * 1024**2
CANARY = "newdarwindev/runspindle-strix-ci-canary"
HOST = "newdarwindev/runspindle-host"
REPOS = {CANARY: 1463722751, HOST: 1456479612}
HOST_WORKFLOW = 417698603
HOST_OTEL_LOCK_BLOB = "888f92eb0a3ed872d4f56ac50568339b7939c92e"
HOST_OTEL_VENDOR = Path("/srv/builds/cargo/host-otel-locked-888f92e/host-otel-vendor.tar.gz")
HOST_OTEL_VENDOR_SHA256 = "36fb36c76a4d3ed482c41fc0c3222df25aa68c2cbcfef23dc81c88b9debacc10"
BUILDS_UUID = "9ed2ab68-7a7c-4cf4-8c28-594742bcae0d"
DEMO_INPUTS = Path("/srv/builds/runspindle-quality/demo-guest-20261120")
DEMO_VERIFY_DIGESTS = {
    "noble-demo-media.tar": "f44877a21d3ae640c9b2acb8953e90b3e8c279f3688a36fd5cbec7e2d56e3fdb",
    "npm-playwright-1.56.1.tar.gz": "ea36ca31468469077ffd123b8da410a622eace55fedf60e5f594b843f65b6f8f",
    "playwright-chromium-1194.tar.gz": "969e2b67e4ef830f7cc5af47e16d2a77a026cfea339da9b27a861b68874cd6e0",
}
DEMO_GH_SHA256 = "540cffbf2146101a713284bc06427efbf370a40756c0356e3a0f8d1e038e7122"
TETHER = "newdarwindev/runspindle-tether"
TETHER_VISUAL_WORKFLOWS = {
    "d2c9ff0ed291789922d2627ed43a9b03e68bc812",
    "b95cf4d906807224abb27891691a3e561c0338c2",
}
TETHER_UPSTREAM_WORKFLOWS = {
    "836c1fad6aae67eed3fd61298d9ec723cfb892da",
    "d86b2440139eea7bc522eb4dfc3920cd759bec7e",
    "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a",
}
TETHER_UPSTREAM_COMPONENT_WORKFLOWS = TETHER_UPSTREAM_WORKFLOWS - {
    "836c1fad6aae67eed3fd61298d9ec723cfb892da",
}
TETHER_NODE_QUALITY_WORKFLOW = "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a"
TETHER_NODE_QUALITY_ARCHIVE_SHA = "ec040b616152ddaecc4d8324639e2097b320078aafbe3ed08bb90f92d09dda23"
TETHER_UPSTREAM_WHEELHOUSE = Path("/srv/builds/runspindle-quality/tether-upstream-wheels")
TETHER_UPSTREAM_SUMS = "9a290a4b53d612536795e7167fe9ee5988f641159982d57a1e25c9445dc04396"
TETHER_VISUAL_WHEELHOUSE = "tether-visual-cp312-linux-x64-v1"
TETHER_VISUAL_SUMS = "11ec4d2ecfa4c17ef0ab504940b45756916b9a8c1f6d279a1e6af35c229cf3a4"
BROWSER = "newdarwindev/runspindle-browser"
BROWSER_PROFILE_WHEELHOUSE = "browser-profile-wheels.rHUFxUbY"
BROWSER_PROFILE_SUMS = "157cd92e4b14e91e7fc36fb16ed97a93598f385f7df06e2bbc850e95de2d7cac"
CONSOLE = "newdarwindev/runspindle-console"
CONSOLE_UI_WORKFLOWS = {
    "08bd9447641817f4675fca665ab16818e6bf8a97",
    "dcd890f5ade00e4c9a77cf511d6cedac38303597",
}
CONSOLE_UI_CACHE = "console-ui-cache-d208a9d3.tar.gz"
CONSOLE_UI_SHA256 = "d208a9d34bebc3460dd9d18a2405d3a7a1b2e7dac4cadb5eb093db8c958e3494"
OWNER = 329000935
IMAGE = Path("/var/lib/runspindle-fleet/images/candidate-45811b5b28f5.qcow2")
IMAGE_SHA = "a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0"
MANIFEST = "registered-probe-3c27c78e84054c84a9ef2f45622834c7/candidate.json"
DEMO_LAYER_FLAG = Path("/etc/runspindle-fleet/enable-demo-layer")
DEMO_LAYER = IMAGE.with_name("demo-prepared-e7782d0f0083.qcow2")
DEMO_LAYER_SHA = "e7782d0f00834394fd7ce16b003657c7b5193a66049bb2a6b2d9fc104fe28b1a"
DEMO_LAYER_MANIFEST = "demo-prepared-e7782d0f0083.json"
DEMO_LAYER_WORKFLOW = "79ae3781482199d4920f4783bb41eba90bbcc453"
DEMO_CHECKOUT_WORKFLOW = "35689e2c7344950af7edc8df25e461693fe298ed"
DEMO_CHECKOUT_HEAD = "9f22e84b1cd7be8f87ef7719d4350df8ff176a23"
DEMO_CHECKOUT_INTEGRATION_HEAD = "20e32dd68d3bb134ca4069bbdce8be264af93444"
DEMO_CHECKOUT_MAIN_HEAD = "ddca0fb5df634b3b7ed23e88e1d8049e02f5d922"
DEMO_CHECKOUT_ARCHIVE = Path(
    "/srv/builds/strix-core-seed-20261120.5kqeoZ/demo-checkout.mnlkkxr8/demo-checkout.tar.gz"
)
DEMO_CHECKOUT_ARCHIVE_SHA = "fd6ef8914ea864400e1c0ae6f73727022b030d30f681f7e1f6f3d3fbcd7cc4be"

LAB_CHECKOUT_WORKFLOW = "f9bafffac7be986218db5809832067e71140c6cf"
LAB_CHECKOUT_HEAD = "1c670c9cca46132abc060bcd4620be6f0f9add3b"
LAB_CHECKOUT_ARCHIVE = Path(
    "/srv/builds/strix-lab-light-qualification-20261120.AJFQLr/lab-checkout.tar.gz"
)
LAB_CHECKOUT_ARCHIVE_SHA = "153c00bdf85be6ef9de8f3a5bb03ec784ab699ca83d11e2f75b92ded94d20669"

LAB_MAIN_CHECKOUT_ARCHIVE = Path(
    "/srv/builds/strix-lab-light-qualification-20261120.AJFQLr/main-checkout.miq9_0vj"
    "/lab-checkout-main.tar.gz"
)
LAB_MAIN_CHECKOUT_ARCHIVE_SHA = "78f3bf85e83bf3b3df2c7cc6b3c5843eaaccdaf1f562f4dfcf5caa64b35e4acb"

CORE_SEED_DISK_GIB = 26
CORE_SEED_DISK_EXTRA = 2 * GIB
CORE_SEED_HEADS = {
    "f74a36fa1d31ce5c1206bf96b2ac0eea79ce9a2b",
    "57d2391012adad214ce2fb4a518a73b693731e6d",
}
CORE_SEED_ARCHIVE = Path(
    "/srv/builds/strix-core-seed-20261120.5kqeoZ/native-seed-build.HbqZjkc7/"
    "native-seed-d256634009b89f22a5306cb641dc98a11d6dd9644e3834c433fcc779e6eb1000.tar.zst"
)
CORE_SEED_ARCHIVE_SHA = "9cb7f555b10c2ddb5779eb887e1c178746ca5027c7cbc0afdfa28971e62df414"

LAYER_SUPPORT = Path("/home/standard/Projects/runspindle-platform-strix-layer-retention-20261120")
LAYER_SUPPORT_SHA = "d19c429abf78605be5aed1c07cf8877befdbf126"
COMPACT_SUPPORT = Path("/home/standard/Projects/runspindle-platform-strix-retention-digest-reuse-20261120")
COMPACT_SUPPORT_SHA = "55c70f12821db0c6fe061e6fbcc976d98a2fe104"
CANARY_HEAD = "722739a047f320d029180b0322a3f2429e14924a"
CANARY_BRANCH = "ci/strix-canary-exclusive-label-20261119"
CANARY_WORKFLOW = 423463385


class CapacityUnavailable(RuntimeError):
    """The host lacks the current reserve for another guest."""


class PeerStarting(CapacityUnavailable):
    """An owned guest in this batch is still preparing for admission."""


class InsufficientMemory(CapacityUnavailable):
    pass


class NearMemoryLimit(CapacityUnavailable):
    """A small memory shortfall may clear while the current jobs run."""


class GitHubUnavailable(RuntimeError):
    """A transient GitHub API failure."""


class GitHubRateLimited(RuntimeError):
    """GitHub requested a pause before further API calls."""


class GitHubRejected(RuntimeError):
    """An API response with an explicit failure status."""

    def __init__(self, method, status):
        super().__init__(f"GitHub {method} rejected (HTTP {status})")
        self.status = status


class RunnerCleanupPending(RuntimeError):
    """GitHub has yet to release a stopped guest's runner."""


def api(method, path, expected=200, *, include=False, etag=None):
    args = ["gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "X-GitHub-Api-Version: 2026-03-10", path]
    if include:
        args.append("--include")
    if etag is not None:
        args.extend(["-H", f"If-None-Match: {etag}"])
    retries = (0, 2, 5) if method == "GET" else (0,)
    for delay in retries:
        if delay:
            time.sleep(delay)
        try:
            return api_once(args, method, include=include) if include else api_once(args, method)
        except GitHubUnavailable as error:
            last_error = error
    raise last_error


def api_once(args, method, *, include=False):
    try:
        result = subprocess.run(args, capture_output=True, timeout=18, check=False, text=True)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubUnavailable(f"GitHub {method} transport unavailable") from None
    body = result.stdout
    headers = {}
    if include and body.startswith("HTTP/"):
        raw_headers, separator, body = body.partition("\n\n")
        lines = raw_headers.splitlines()
        status = re.match(r"HTTP/\S+ (\d{3})\b", lines[0])
        if status and status[1] == "304":
            return None, None
        if not result.returncode and (not separator or not status or status[1] != "200"):
            raise GitHubUnavailable("GitHub returned an invalid queue HTTP response")
        headers = {
            key.lower(): value.strip()
            for line in lines[1:] if ":" in line
            for key, value in [line.split(":", 1)]
        }
    if result.returncode:
        match = re.search(r"\(HTTP ([0-9]{3})\)", result.stderr)
        status = int(match[1]) if match else None
        throttled = status == 429 or (
            status == 403 and "rate limit" in result.stderr.lower()
        )
        if throttled:
            raise GitHubRateLimited(f"GitHub {method} rate-limited (HTTP {status})")
        if status is None or status == 408 or 500 <= status <= 599:
            raise GitHubUnavailable(f"GitHub {method} temporarily unavailable (HTTP {status})")
        raise GitHubRejected(method, status)
    try:
        value = json.loads(body) if body.strip() else None
        if include and not isinstance(value, dict):
            raise GitHubUnavailable("GitHub returned an invalid queue HTTP body")
        return (value, headers.get("etag")) if include else value
    except ValueError:
        raise GitHubUnavailable(f"GitHub {method} returned invalid JSON") from None


def canary_job(run_id, job_id, name):
    path = f"/repos/{CANARY}/actions/runs/{run_id}"
    run = api("GET", path)
    if (
        run.get("id") != run_id
        or run.get("workflow_id") != CANARY_WORKFLOW
        or run.get("path") != ".github/workflows/strix-canary.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != CANARY_BRANCH
        or run.get("head_sha") != CANARY_HEAD
        or run.get("run_attempt") != 1
        or run.get("status") not in ("queued", "in_progress")
        or any((run.get(key) or {}).get("id") != OWNER for key in ("actor", "triggering_actor"))
        or any(
            (run.get(key) or {}).get("id") != REPOS[CANARY]
            or (run.get(key) or {}).get("private") is not True
            for key in ("repository", "head_repository")
        )
    ):
        raise RuntimeError("private canary run identity differs")
    jobs = inventory(api, path + "/attempts/1/jobs", "jobs")
    purpose = "proof" if name == "proof" else "failure"
    label = f"runspindle-strix-canary-{purpose}-{run_id}-1"
    matches = [job for job in jobs if (
        job.get("id") == job_id and job.get("name") == name
        and job.get("run_id") == run_id and job.get("head_sha") == CANARY_HEAD
        and job.get("run_attempt") == 1 and job.get("status") == "queued"
        and job.get("labels") == [label]
        and job.get("runner_id") in (0, None) and not job.get("runner_name")
    )]
    if len(matches) != 1:
        raise RuntimeError("private canary job is outside its queue state")
    return matches[0]


def valid_etag(value):
    return isinstance(value, str) and re.fullmatch(r'(?:W/)?"[0-9a-zA-Z._-]{1,128}"', value)


def observe_result(repo, run_id, job, runner, name, seconds):
    path = f"/repos/{repo}/actions/jobs/{job['id']}"
    started = time.monotonic()
    deadline = started + seconds
    etag = None
    while time.monotonic() < deadline:
        try:
            row, received_etag = api("GET", path, include=True, etag=etag)
            if row is None and etag is None:
                raise GitHubUnavailable("GitHub returned no initial job status")
        except GitHubRateLimited as error:
            print(f"{error}. Keeping the registered guest until the next observation.", flush=True)
            time.sleep(min(60, max(0, deadline - time.monotonic())))
            continue
        except GitHubUnavailable as error:
            print(str(error), flush=True)
            time.sleep(5)
            continue
        if row is None:
            interval = 5 if time.monotonic() - started < 90 else 15
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            continue
        etag = received_etag if valid_etag(received_etag) else None
        if any(row.get(key) != value for key, value in {
            "id": job["id"], "run_id": run_id, "run_attempt": job["run_attempt"],
            "head_sha": job["head_sha"], "labels": job["labels"], "name": job["name"],
        }.items()):
            raise RuntimeError("GitHub job identity changed")
        if row.get("status") == "completed":
            if row.get("runner_id") != runner or row.get("runner_name") != name:
                raise RuntimeError("terminal job was assigned to a different runner")
            return {key: row.get(key) for key in (
                "id", "run_id", "head_sha", "runner_id", "runner_name",
                "conclusion", "started_at", "completed_at", "html_url",
            )}
        if row.get("runner_id") not in (None, 0, runner):
            raise RuntimeError("job was assigned to a different runner")
        # Check short jobs more often when GitHub supports conditional reads.
        interval = 5 if etag and time.monotonic() - started < 90 else 15
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise TimeoutError("GitHub job observation timed out")


def network_read(*args):
    allowed = (
        args == ("nft", "-s", "-nn", "list", "table", "inet", "runspindle_fleet")
        or args in (
            ("ip", "-j", "-n", "runspindle-canary", "route", "show"),
            ("ip", "-j", "-n", "runspindle-canary", "-d", "addr", "show", "dev", "rscan1"),
        )
        or args in {("ip", "-j", "-d", "addr", "show", "dev", n) for n in ("rsvm0", "rsvm1", "rsvm2", "rscan0")}
        or args in {("systemctl", "is-active", f"runspindle-{n}.service")
                    for n in ("proxy-0", "proxy-1", "proxy-2", "host-canary", "lan-canary")}
    )
    if not allowed:
        raise RuntimeError("unsupported network observation")
    return subprocess.run(("sudo", "-n", *args), capture_output=True, text=True, check=True, timeout=8)


def retain_stopped(state, resource, slot_path, backend, inspect_disk, *, verified_base=None):
    """Preserve the original overlay, serial log, and seed on their own mount."""
    archive = backend.ROOT / "retained" / resource["id"] / "slot-originals"
    archive.mkdir(mode=0o700, exist_ok=True)
    archive_info = archive.lstat()
    if (
        not stat.S_ISDIR(archive_info.st_mode) or archive_info.st_uid != os.getuid()
        or stat.S_IMODE(archive_info.st_mode) != 0o700 or any(archive.iterdir())
    ):
        raise RuntimeError("unexpected original retention directory state")
    entries = []
    binding = None
    for name in ("disk.qcow2", "serial.log", "seed.iso"):
        source = slot_path / name
        if not source.exists() and not source.is_symlink():
            continue
        info = source.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_dev != archive.stat().st_dev
        ):
            raise RuntimeError("unsafe stopped guest file")
        if name == "disk.qcow2":
            options = {"verified_base": verified_base} if verified_base is not None else {}
            if resource.get("details", {}).get("image_sha256") == DEMO_LAYER_SHA:
                binding = inspect_disk(source, DEMO_LAYER, DEMO_LAYER_SHA, backend.execute,
                                       ancestor=(IMAGE, IMAGE_SHA), **options)
            else:
                if resource.get("details", {}).get("guest_disk_gib") == CORE_SEED_DISK_GIB:
                    options["overlay_limit"] = CORE_SEED_DISK_GIB * GIB
                binding = inspect_disk(source, IMAGE, IMAGE_SHA, backend.execute, **options)
            checksum = binding["overlay_sha256"]
        else:
            checksum = backend.digest(source)
        entries.append({
            "source": str(source), "destination": str(archive / name),
            "inode": info.st_ino, "size": info.st_size, "sha256": checksum,
        })
    if {item.name for item in slot_path.iterdir()} != {Path(row["source"]).name for row in entries}:
        raise RuntimeError("unknown input remains in the stopped slot")
    state.update(resource, "cleaning", original_retention={"files": entries, "backing": binding})
    for entry in entries:
        source = Path(entry["source"])
        target = Path(entry["destination"])
        info = source.lstat()
        if (info.st_ino, info.st_size) != (entry["inode"], entry["size"]) or target.exists():
            raise RuntimeError("stopped file changed after retention intent")
        source.rename(target)
        actual = target.lstat()
        if (
            (actual.st_ino, actual.st_size) != (entry["inode"], entry["size"])
            or backend.digest(target) != entry["sha256"]
        ):
            raise RuntimeError("original file changed during retention")
        backend.sync_directory(slot_path)
        backend.sync_directory(archive)
    state.update(resource, "cleaning", original_retention_complete=True)


def remove_owned_runner(repo, name, label, runner_id):
    path = f"/repos/{repo}/actions/runners"

    def owned():
        return [
            row for row in inventory(api, path, "runners")
            if row["name"] == name or row["id"] == runner_id
        ]

    matches = owned()
    if not matches:
        return
    if len(matches) != 1:
        raise RuntimeError("ambiguous runner identity during cleanup")
    row, = matches
    if (
        row["name"] != name or (runner_id is not None and row["id"] != runner_id)
        or [(item.get("name"), item.get("type")) for item in row.get("labels", [])]
        != [(label, "custom")]
    ):
        raise RuntimeError("runner identity changed during cleanup")
    if row.get("status") != "offline" or row.get("busy") is not False:
        raise RunnerCleanupPending(f"GitHub runner {row['id']} is still online or busy")
    try:
        api("DELETE", f"{path}/{row['id']}", 204)
    except (GitHubUnavailable, GitHubRejected) as error:
        if isinstance(error, GitHubRejected) and error.status not in (404, 422):
            raise
        if owned():
            raise
        return
    if owned():
        raise RunnerCleanupPending(f"GitHub runner {row['id']} remains registered")


def reconcile_stopped_direct(state, backend, helper, *, allow_pending=False):
    pending = set()
    for resource in state.active():
        try:
            reconcile_stopped_resource(state, backend, helper, resource)
        except RunnerCleanupPending as error:
            if not allow_pending:
                raise
            pending.add(f"newdarwindev/{resource['repository']}")
            print(str(error), flush=True)
    return pending


def verified_stopped_resource(state, backend, helper, resource):
    details = resource["details"]
    identity, slot = resource["id"], resource["slot"]
    repo = f"newdarwindev/{resource['repository']}"
    guest = helper("observe", slot)
    if (
        resource["phase"] != "uncertain"
        or details.get("isolated_direct_pilot") is not True
        or details.get("image_sha256") not in (
            (IMAGE_SHA, DEMO_LAYER_SHA) if repo == DEMO else (IMAGE_SHA,)
        )
        or details.get("original_retention_complete") is not True
        or resource["runner_name"] != f"runspindle-fleet-{identity}"
        or repo not in DIRECT_REPOSITORIES | {CANARY}
        or guest.get("resource_id") != identity or guest.get("ActiveState") != "inactive"
        or int(guest.get("MainPID", 0))
        or Path(f"/sys/fs/cgroup/system.slice/runspindle-vm-{slot}.service").exists()
        or any((backend.ROOT / "slots" / str(slot)).iterdir())
    ):
        raise RuntimeError("fleet slot is occupied or unresolved")
    event, = state.db.execute(
        "SELECT details FROM events WHERE resource=? AND kind='reserved'", (identity,),
    ).fetchall()
    reserved = json.loads(event[0])["job"]
    label, = reserved["labels"]
    if (
        not LABEL.fullmatch(label) or reserved["id"] != resource["requested_job"]
        or reserved["run_id"] != details["run_id"]
    ):
        raise RuntimeError("durable job identity changed during cleanup")
    return repo, reserved, label


def reconcile_stopped_resource(state, backend, helper, resource):
    repo, reserved, label = verified_stopped_resource(state, backend, helper, resource)
    identity = resource["id"]
    details = resource["details"]
    remove_owned_runner(repo, resource["runner_name"], label, details.get("runner_id"))
    state.update(resource, "destroyed", direct_recovery={
        "runner_id": details.get("runner_id"), "job_id": reserved["id"],
        "runner_absent": True, "vm_cgroup_absent": True, "originals_retained": True,
    })
    print(f"Recovered cleanup for direct runner resource {identity}", flush=True)


def stage_wheelhouse(machine, resource, wheelhouse, manifest_sha256, destination):
    """Send the pinned wheelhouse over the verified guest connection."""
    manifest = wheelhouse / "SHA256SUMS"
    if wheelhouse.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("offline wheel manifest missing")
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest_sha256:
        raise RuntimeError("offline wheel manifest changed")
    wheels = []
    for line in data.decode("ascii").splitlines():
        digest, filename = line.split("  ", 1)
        if not re.fullmatch(r"[a-z0-9_.-]+\.whl", filename):
            raise RuntimeError("invalid offline wheel name")
        path = wheelhouse / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("offline wheel missing")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise RuntimeError("offline wheel changed")
        wheels.append(path)
    for wheel in wheels:
        machine.copy_prepared(resource, wheel, f"{destination}/wheels/{wheel.name}")
    print(f"Verified offline wheels staged for {destination}", flush=True)


def stage_console_ui(machine, resource, state_directory):
    """Send the locked npm cache over the verified guest connection."""
    archive = state_directory / CONSOLE_UI_CACHE
    if archive.is_symlink() or not archive.is_file():
        raise RuntimeError("offline Console UI cache missing")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != CONSOLE_UI_SHA256:
            raise RuntimeError("offline Console UI cache changed")
    machine.copy_prepared(resource, archive, "console-ui-cache.tar.gz")
    print("Verified offline Console UI cache staged", flush=True)


def stage_host_cargo(machine, resource, job):
    """Install the pinned source replacement for the exact Host OTel lockfile."""
    lock = api("GET", f"/repos/{HOST}/contents/Cargo.lock?ref={job['head_sha']}")
    if lock.get("type") != "file" or lock.get("sha") != HOST_OTEL_LOCK_BLOB:
        return
    mounted = subprocess.check_output(
        ["findmnt", "-rn", "-M", "/srv/builds", "-o", "UUID"], text=True,
    )
    if mounted.strip() != BUILDS_UUID:
        raise RuntimeError("Host offline Cargo bundle requires the build volume")
    with HOST_OTEL_VENDOR.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != HOST_OTEL_VENDOR_SHA256:
            raise RuntimeError("Host offline Cargo bundle changed")
    machine.copy_prepared(resource, HOST_OTEL_VENDOR, "host-otel/vendor.tar.gz")
    machine.ssh(resource, f"""set -eu
cd /home/runner/preparation/host-otel
printf '%s  %s\\n' '{HOST_OTEL_VENDOR_SHA256}' vendor.tar.gz | sha256sum --check --status
tar --extract --gzip --file vendor.tar.gz --no-same-owner --no-same-permissions
mkdir -p /home/runner/.cargo
test ! -e /home/runner/.cargo/config
(
    set -C
    cat > /home/runner/.cargo/config.toml <<'EOF'
[source.crates-io]
replace-with = "host-otel-vendor"

[source.host-otel-vendor]
directory = "/home/runner/preparation/host-otel/vendor"
EOF
)
""", timeout=90)
    print("Verified offline Host Cargo inputs staged", flush=True)


def stage_demo(machine, resource, job):
    """Provision only the verified inputs for an admitted Demo Suite job."""
    if job["name"] == "publish-presentation":
        source = Path("/usr/bin/gh")
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != DEMO_GH_SHA256:
                raise RuntimeError("Demo Suite release CLI changed")
        machine.copy_prepared(resource, source, "demo/gh")
        machine.ssh(resource, f"""set -eu
cd /home/runner/preparation/demo
printf '%s  gh\\n' '{DEMO_GH_SHA256}' | sha256sum -c --status
sudo -n install -m 0755 gh /usr/local/bin/gh
""")
        return

    mounted = subprocess.check_output(
        ["findmnt", "-rn", "-M", "/srv/builds", "-o", "UUID"], text=True,
    )
    if mounted.strip() != BUILDS_UUID:
        raise RuntimeError("Demo Suite inputs require the build volume")
    for filename, expected in DEMO_VERIFY_DIGESTS.items():
        source = DEMO_INPUTS / filename
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise RuntimeError("Demo Suite preparation input changed")
        machine.copy_prepared(resource, source, f"demo/{filename}")
    checksums = "\n".join(f"{digest}  {name}" for name, digest in DEMO_VERIFY_DIGESTS.items())
    machine.ssh(resource, f"""set -eu
cd /home/runner/preparation/demo
sha256sum -c --status <<'SHA256SUMS'
{checksums}
SHA256SUMS
mkdir debs browsers npm
tar -xf noble-demo-media.tar -C debs
tar -xzf playwright-chromium-1194.tar.gz -C browsers
tar -xzf npm-playwright-1.56.1.tar.gz -C npm
sudo -n env DEBIAN_FRONTEND=noninteractive dpkg --skip-same-version --force-confold --unpack debs/*.deb
sudo -n env DEBIAN_FRONTEND=noninteractive dpkg --configure -a
# AppArmor matches the executable's resolved path. Keep its installation
# outside the runner's writable directories and retain the workflow's cache path.
sudo -n install -d -o root -g root -m 0755 /opt/runspindle-demo
sudo -n mv browsers /opt/runspindle-demo/browsers
sudo -n chown -R root:root /opt/runspindle-demo/browsers
ln -s /opt/runspindle-demo/browsers browsers
sudo -n tee /etc/apparmor.d/runspindle-demo-chromium >/dev/null <<'APPARMOR'
abi <abi/4.0>,
profile runspindle-demo-chromium /opt/runspindle-demo/browsers/chromium_headless_shell-1194/chrome-linux/headless_shell flags=(unconfined) {{
  userns,
}}
APPARMOR
sudo -n apparmor_parser --replace /etc/apparmor.d/runspindle-demo-chromium
ffmpeg -version >/dev/null
ffprobe -version >/dev/null
""", timeout=180)
    print("Verified offline Demo Suite inputs staged", flush=True)


def enabled_flag(path):
    try:
        flag = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(flag.st_mode) or flag.st_uid != 0 or flag.st_mode & 0o022:
        raise RoutingError(f"unsafe enablement file: {path.name}")
    return True


def light_slot_enabled():
    return enabled_flag(LIGHT_SLOT_FLAG)


def prepared_demo_job(enabled, repository, job, workflow_blob):
    return (
        enabled and repository == DEMO and job.get("name") == "verify"
        and (
            workflow_blob == DEMO_LAYER_WORKFLOW
            or (
                workflow_blob == DEMO_CHECKOUT_WORKFLOW
                and job.get("head_sha") in {
                    DEMO_CHECKOUT_HEAD, DEMO_CHECKOUT_INTEGRATION_HEAD, DEMO_CHECKOUT_MAIN_HEAD,
                }
            )
        )
    )


def stage_checkout(machine, resource, name, archive, expected_digest):
    mounted = subprocess.check_output(
        ["findmnt", "-rn", "-M", "/srv/builds", "-o", "UUID"], text=True,
    )
    if mounted.strip() != BUILDS_UUID:
        raise RuntimeError("checkout cache requires the build volume")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
            raise RuntimeError("checkout cache archive changed")
    target = shlex.quote(name)
    archive_name = shlex.quote(name + ".tar.gz")
    machine.copy_prepared(resource, archive, name + ".tar.gz")
    machine.ssh(resource, f"""set -eu
cd /home/runner/preparation
printf '%s  %s\n' '{expected_digest}' {archive_name} | sha256sum --check --status
mkdir -m 0700 {target}
tar -xzf {archive_name} --no-same-owner --no-same-permissions -C {target}
test -f {target}/pins.tsv
""", timeout=20)
    print(f"Verified pinned {name} archive staged", flush=True)


def stage_core_seed(machine, resource):
    mounted = subprocess.check_output(
        ["findmnt", "-rn", "-M", "/srv/builds", "-o", "UUID"], text=True,
    )
    if mounted.strip() != BUILDS_UUID:
        raise RuntimeError("Core seed requires the build volume")
    with CORE_SEED_ARCHIVE.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != CORE_SEED_ARCHIVE_SHA:
            raise RuntimeError("Core seed archive changed")
    ready = machine.ssh(resource, """
command -v zstd >/dev/null &&
    test "$(df -B1 --output=avail /home/runner | tail -1)" -ge 10468982784
""", timeout=15, check=False)
    if ready.returncode:
        print("Core seed skipped because guest extraction prerequisites are unavailable", flush=True)
        return
    machine.copy_prepared(resource, CORE_SEED_ARCHIVE, "core-seed.tar.zst")
    machine.ssh(resource, f"""set -eu
cd /home/runner/preparation
printf '%s  %s\n' '{CORE_SEED_ARCHIVE_SHA}' core-seed.tar.zst | sha256sum --check --status
test "$(df -B1 --output=avail . | tail -1)" -ge 9932111872
mkdir -m 0700 core-compiler-cache
tar --zstd -xf core-seed.tar.zst --no-same-owner -C core-compiler-cache
rm -- core-seed.tar.zst
""", timeout=60)
    print("Verified Core compiler seed staged", flush=True)


def verify_prepared_demo(machine, resource):
    checksums = "\n".join(f"{digest}  {name}" for name, digest in DEMO_VERIFY_DIGESTS.items())
    machine.ssh(resource, f"""set -eu
cd /home/runner/preparation/demo
sha256sum -c --status <<'SHA256SUMS'
{checksums}
SHA256SUMS
test -d npm
test "$(realpath browsers)" = /opt/runspindle-demo/browsers
test -x /opt/runspindle-demo/browsers/chromium_headless_shell-1194/chrome-linux/headless_shell
sudo -n apparmor_parser --replace /etc/apparmor.d/runspindle-demo-chromium
ffmpeg -version >/dev/null
ffprobe -version >/dev/null
""", timeout=30)
    print("Verified prepared Demo Suite input closure", flush=True)


def light_workflow(route):
    return (
        route is not None and route.repository in LIGHT_WORKFLOWS
        and route.workflow_blob == LIGHT_WORKFLOWS[route.repository]
    )


def light_aggregate_workflow(route, workflow_blob):
    return (
        route is not None
        and route.repository in LIGHT_AGGREGATE_WORKFLOWS
        and workflow_blob == LIGHT_AGGREGATE_WORKFLOWS[route.repository]
    )


def light_profile_job(route, job, workflow_blob):
    name = job.get("name")
    if light_workflow(route) and name == "verify":
        return True
    if (
        route is not None and route.repository == TETHER
        and workflow_blob in LIGHT_TETHER_AGGREGATE_HEADS
        and job.get("head_sha") == LIGHT_TETHER_AGGREGATE_HEADS[workflow_blob]
        and name == "validation / strix-linux-x64"
    ):
        return True
    if not light_aggregate_workflow(route, workflow_blob):
        return False
    if name == "validation / strix-linux-x64":
        return True
    return (
        route.repository == "newdarwindev/runspindle"
        and (
            job.get("head_sha") == "2bbd19cf3adcfb6382c594f96ac2637c402eaee8"
            and name in {"validation / mutation_2", "validation / mutation_3"}
            or job.get("head_sha") == "1880adfa713a6eeacbc4887722a5f11d9bea263b"
            and name == "validation / mutation_3"
        )
    )


def slot_memory_limit(slot):
    return LIGHT_MEMORY_LIMIT if slot == 2 else GUEST_MEMORY_LIMIT


def compact_profile_job(repository, job, workflow_blob):
    name = job.get("name")
    if repository == HOST and workflow_blob == "1224fd80383058a9c4eafad9bfdab8410cd56933":
        head = job.get("head_sha")
        # These exact Rust builds peaked at 3.06 GiB or less in the standard guest.
        if name == "validation / rust":
            return head in {
                "0b5bf5b74a017b2016bb2f6ab8d7d0fa07fcf40a",
                "1dbd4f9263cf81a234d5c3362d8de903dccd1818",
                "559270de822e89e5ee7306e222a9aa9a317086c4",
                "c21bc3d4b10216414d12eba4c5821b2b83bee34b",
            }
        if head == "0b5bf5b74a017b2016bb2f6ab8d7d0fa07fcf40a":
            return name == "validation / mutation_3"
        if head == "c21bc3d4b10216414d12eba4c5821b2b83bee34b":
            return name in {
                "validation / component", "validation / mutation_1",
                "validation / mutation_2", "validation / mutation_3",
            }
        if head == "559270de822e89e5ee7306e222a9aa9a317086c4":
            return name in {
                "validation / quality", "validation / mutation_2",
                "validation / mutation_3", "validation / mutation_4",
            }
        return (
            name in {
                "validation / mutation_1", "validation / mutation_2",
                "validation / mutation_3", "validation / mutation_4",
            }
            and head == "1dbd4f9263cf81a234d5c3362d8de903dccd1818"
        )
    if repository == BROWSER and workflow_blob == "ef7080f2f8d334f5e9353e06fe187afd8e1065b1":
        if name == "validation / rust":
            return job.get("head_sha") in {
                "1c50b37de05d7c653ab1dfd83c9a2ae951e8867c",
                "f27cbd8a6639d700cfde5126a047beb3e7b24a5f",
            }
        return name == "validation / quality" or (
            name == "validation / component"
            and job.get("head_sha") in {
                "e47fb7b7ab7e2efe9bc563fd127467bc588d8acc",
                "1c50b37de05d7c653ab1dfd83c9a2ae951e8867c",
                "122a02fa948eed8c98332c38eb62ffc021afeeca",
                "f27cbd8a6639d700cfde5126a047beb3e7b24a5f",
            }
        )
    if repository == TETHER:
        if workflow_blob == "836c1fad6aae67eed3fd61298d9ec723cfb892da":
            return name == "validation / quality"
        return (
            workflow_blob == "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a"
            and job.get("head_sha") == "cb4d52bf0714cd065b439d515591a2a568b76544"
            and name in {"validation / quality", "validation / component"}
        )
    if repository == "newdarwindev/runspindle" and workflow_blob == "bba7664a55dc961f339579c84bdcbde86af720c9":
        # The exact component and mutation_1 jobs peaked below 1.8 GiB.
        if job.get("head_sha") == "f05cffd2f0ea5d607a983d7d36d238a890b0310e":
            return name in {"validation / component", "validation / mutation_1"}
        if job.get("head_sha") == "93009f2709518a893a3894e908441fc84fab6a9f":
            # These partitions passed with peaks between 1.59 and 1.82 GiB.
            return name in {
                "validation / component", "validation / quality", "validation / rust",
                "validation / mutation_1", "validation / mutation_2", "validation / mutation_4",
            }
        if job.get("head_sha") == "1880adfa713a6eeacbc4887722a5f11d9bea263b":
            return name in {
                "validation / component", "validation / mutation_1", "validation / mutation_2",
                "validation / quality", "validation / rust",
            }
        if job.get("head_sha") == "2bbd19cf3adcfb6382c594f96ac2637c402eaee8":
            return name in {
                "validation / component", "validation / quality", "validation / mutation_1",
                "validation / mutation_2", "validation / mutation_3",
            }
        return (
            name in {
                "validation / component", "validation / quality", "validation / rust",
                "validation / mutation_2", "validation / mutation_3", "validation / mutation_4",
            }
            and job.get("head_sha") == "b7122b89905ebf33a4cd674cc4cf67feba033025"
        )
    if repository == "newdarwindev/runspindle-refinery" and workflow_blob == "afd9d52e77f3e858609e26e42d64ba3b3a27a8ca":
        # These partitions passed at 1520–1787 MiB on this revision.
        if job.get("head_sha") == "b107233878e8fdfb4fe7ef350a9656b35a0e40ab":
            return name in {"validation / component", "validation / quality", "validation / mutation_2"}
        if job.get("head_sha") == "b2a97ffda86040987c45db29f359b5173a512b5e":
            return name in {
                "validation / component", "validation / quality",
                "validation / mutation_1", "validation / mutation_2", "validation / strix-linux-x64",
            }
        return (
            name == "validation / mutation_2"
            and job.get("head_sha") == "c4647a243ae07f0e9fa664c4e226e1f06607aa67"
        )
    if repository == CONSOLE and workflow_blob == "dcd890f5ade00e4c9a77cf511d6cedac38303597":
        head = job.get("head_sha")
        # Both commits share a Git tree. Component, quality, and mutation_1
        # passed on the first commit with peaks of at most 2013 MiB.
        if head in {
            "f64720f147b3e494349bf6962755218b461fa801",
            "57e3ef3ee4c72e4e6f543d40f906df6f167e05e2",
        }:
            return name in {"validation / component", "validation / quality", "validation / mutation_1"}
        if head == "fd2a24dd38fd7d01646b710ae263ddc9e3ef42a1":
            return name in {"validation / component", "validation / quality"}
        if head in {"d076276e632b0d76aedb21717bc4d02899a410f4", "487a16b2e1bf4d5305bcee551a0aec995d1b3159"}:
            return name in {"validation / component", "validation / quality", "validation / mutation_1"}
        return (
            name in {"validation / quality", "validation / mutation_1"}
            and head == "ca89e2b3c1394c3d2c0941a5d27ec1f64c4876ce"
        )
    return False


def peer_ready_for_admission(peer, batch, repository, overlap_preparation):
    if peer["details"].get("direct_batch") != batch:
        return False
    return peer["phase"] == "registered" or (
        overlap_preparation and peer["phase"] == "isolated"
        and peer["repository"] != repository
    )


def peer_has_stopped(peer, guest):
    return (
        guest.get("resource_id") == peer["id"]
        and guest.get("ActiveState") in {"inactive", "failed"}
        and str(guest.get("MainPID")) == "0"
        and not Path(f"/sys/fs/cgroup/system.slice/runspindle-vm-{peer['slot']}.service").exists()
    )


def parallel_memory_group(slot, expected_limit=None):
    unit = f"runspindle-vm-{slot}.service"
    group = Path(f"/sys/fs/cgroup/system.slice/{unit}")
    if expected_limit is None:
        expected_limit = slot_memory_limit(slot)
    allowed_limits = {slot_memory_limit(slot)}
    if slot in (0, 1):
        allowed_limits.add(COMPACT_MEMORY_LIMIT)
    try:
        if (expected_limit not in allowed_limits
                or int((group / "memory.max").read_text()) != expected_limit):
            raise ValueError("unexpected guest limit")
    except (OSError, ValueError) as error:
        raise CapacityUnavailable(f"slot {slot} memory limit is unverified") from error
    return group


def reserve_guest_disk(used_slots, disk_free, extra_disk):
    """Budget a guest by occupied slot count, including a running light guest."""
    reserve = THREE_SLOT_DISK_RESERVE if len(used_slots) >= 2 else PARALLEL_DISK_RESERVE
    if disk_free < reserve + extra_disk:
        raise CapacityUnavailable(
            f"fleet and pool lack the disk reserve for {len(used_slots) + 1} slots "
            f"(available_bytes={disk_free} required_bytes={reserve + extra_disk})"
        )
    return reserve


def parallel_capacity(directory, storage_identity):
    disks = storage_identity(directory)
    result = subprocess.run(
        ["sudo", "-n", "lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
         "-o", "lv_size,data_percent,metadata_percent", "pve/data"],
        capture_output=True, text=True, check=True, timeout=20,
    )
    rows = json.loads(result.stdout)["report"][0]["lv"]
    if len(rows) != 1:
        raise ValueError("ambiguous shared thin pool")
    row, = rows
    metadata = float(row["metadata_percent"])
    used = float(row["data_percent"])
    if not (0 <= used <= 100 and 0 <= metadata < 80):
        raise ValueError("shared thin pool usage exceeds the admission limits")
    pool_free = int(int(row["lv_size"]) * (1 - used / 100))
    filesystem_free = min(disks.values())
    if min(pool_free, filesystem_free) < PARALLEL_DISK_RESERVE:
        raise CapacityUnavailable("parallel dispatch requires 82 GiB free on the fleet and pool")
    line = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:"))
    return {
        "available_memory": int(line.split()[1]) * 1024,
        "filesystem_free": filesystem_free,
        "filesystem_paths_free": disks,
        "pool_free": pool_free,
        "pool_metadata_percent": metadata,
    }


def run(args):
    if socket.gethostname().split(".")[0] != "kali-strix1":
        raise RuntimeError("expected kali-strix1")
    expanded = light_slot_enabled()
    prepared_layer = expanded and enabled_flag(DEMO_LAYER_FLAG)
    compact_enabled = expanded and enabled_flag(COMPACT_PROFILE_FLAG)
    if compact_enabled:
        support, expected = COMPACT_SUPPORT, COMPACT_SUPPORT_SHA
    elif prepared_layer:
        support, expected = LAYER_SUPPORT, LAYER_SUPPORT_SHA
    elif expanded:
        support, expected = LIGHT_SUPPORT, LIGHT_SUPPORT_SHA
    else:
        support, expected = SUPPORT, "45bb4dea6f0412a20bb04a6837e39f0d665d7e36"
    support_sha = subprocess.check_output(
        ["git", "-C", str(support), "rev-parse", "HEAD"], text=True,
    ).strip()
    if support_sha != expected or subprocess.check_output(
        ["git", "-C", str(support), "status", "--porcelain"], text=True,
    ):
        raise RuntimeError("reviewed fleet support checkout changed")
    support_path = str(support / "tools")
    if support_path not in sys.path:
        sys.path.insert(0, support_path)
    from runner_fleet import backend, host_helper
    from runner_fleet.registered_probe import capacity, storage_identity
    from runner_fleet.original_retention import inspect_linked_disk
    from runner_fleet.single_probe import (
        IsolatedMetadata, STATE_DIRECTORY, manager_stopped,
        validate_state_directory, verify_guest_lifetime, verify_host_lifetime_policy,
    )
    from runner_fleet.state import State

    installed_helper = backend.helper
    compact_identity = None
    host_helper.run = network_read

    def read_only_helper(command, *values):
        if command == "verify-network":
            return host_helper.verify_network()
        if command not in ("observe", "start", "stop"):
            raise RuntimeError("unsupported fleet helper operation")
        if command == "start" and compact_identity is not None and values[1] == compact_identity:
            command = "start-compact"
        return installed_helper(command, *values)

    backend.helper = read_only_helper
    route = getattr(args, "route", None)
    if args.lane == "host":
        route = Route(HOST, REPOS[HOST], HOST_WORKFLOW, args.workflow_blob)
    repo = route.repository if route else CANARY

    def lookup():
        if repo == CANARY:
            return canary_job(args.run_id, args.job_id, args.name), None
        attempt, jobs = queued_jobs(api, route, args.run_id, actor_ids={OWNER})
        found = [job for job in jobs if job["id"] == args.job_id]
        if len(found) != 1:
            raise RuntimeError("requested direct job is not queued")
        return found[0], attempt.workflow_blob

    manager_stopped()
    verify_host_lifetime_policy()
    validate_state_directory(STATE_DIRECTORY)
    state = State(STATE_DIRECTORY)
    runners_path = f"/repos/{repo}/actions/runners"
    try:
        batch = getattr(args, "batch", None)
        with (nullcontext() if batch else state.lock()), ExitStack() as admission:
            manager_stopped()
            if batch:
                lock = admission.enter_context((STATE_DIRECTORY / "direct-admission.lock").open("a"))
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.batch_stopping.is_set():
                    return 0
                occupied = []
                active = []
                for peer in state.active():
                    if expanded and peer["phase"] == "uncertain":
                        try:
                            reconcile_stopped_resource(state, backend, installed_helper, peer)
                        except RunnerCleanupPending:
                            if repo == f"newdarwindev/{peer['repository']}":
                                raise CapacityUnavailable("repository runner cleanup is pending") from None
                            occupied.append(peer)
                    else:
                        occupied.append(peer)
                        if expanded and peer["phase"] == "cleaning" and peer_has_stopped(
                            peer, installed_helper("observe", peer["slot"]),
                        ):
                            continue
                        active.append(peer)
                unready = [
                    peer for peer in active
                    if not peer_ready_for_admission(
                        peer, batch, repo.removeprefix("newdarwindev/"), expanded,
                    )
                ]
                if unready and all(
                    peer["details"].get("direct_batch") == batch
                    and peer["phase"] in {"reserved", "starting", "isolated"}
                    for peer in unready
                ):
                    raise PeerStarting("an owned parallel guest is still preparing")
                if unready:
                    raise CapacityUnavailable("parallel guest ownership or registration is unresolved")
            else:
                pending = reconcile_stopped_direct(
                    state, backend, installed_helper,
                    allow_pending=expanded and args.lane == "cleanup",
                )
                occupied = active = []
            if args.lane == "cleanup":
                return pending
            routes_path = getattr(args, "routes_path", None)
            if routes_path is not None and route not in load_routes(routes_path):
                raise RoutingError("repository route changed before guest admission")
            if IMAGE.lstat().st_nlink != 1:
                raise RoutingError("guest backing image must have one link for original retention")
            job, workflow_blob = lookup()
            light_job = expanded and batch and light_profile_job(route, job, workflow_blob)
            eligible_slots = (2, 0, 1) if light_job else (0, 1)
            used_slots = {peer["slot"] for peer in occupied}
            slot = next((choice for choice in eligible_slots if choice not in used_slots), None)
            if slot is None:
                raise CapacityUnavailable("no eligible guest slot is available")
            try:
                budget = (
                    parallel_capacity(STATE_DIRECTORY, storage_identity) if batch
                    else capacity(STATE_DIRECTORY)
                )
            except (ValueError, subprocess.SubprocessError) as error:
                raise CapacityUnavailable("runner capacity is temporarily unavailable") from error
            disk_free = min(budget["pool_free"], budget["filesystem_free"]) if batch else 0
            extra_disk = CORE_SEED_DISK_EXTRA * sum(
                peer["details"].get("guest_disk_gib") == CORE_SEED_DISK_GIB for peer in active
            )
            if batch:
                disk_reserve = reserve_guest_disk(used_slots, disk_free, extra_disk)
            core_seed = (
                compact_enabled and batch
                and repo == "newdarwindev/runspindle-core"
                and job["name"] == "validation / rust"
                and job.get("head_sha") in CORE_SEED_HEADS
                and disk_free >= disk_reserve + extra_disk + CORE_SEED_DISK_EXTRA
            )
            # Reserve the guest cgroup limit in full, as well as the
            # peer's unused limit, and 2 GiB for the host in parallel mode.
            compact = (
                compact_enabled and batch and slot in (0, 1)
                and (light_job or compact_profile_job(repo, job, workflow_blob))
            )
            memory_limit = COMPACT_MEMORY_LIMIT if compact else slot_memory_limit(slot)
            memory_needed = memory_limit + PARALLEL_HOST_MEMORY_RESERVE
            for peer in active:
                guest = installed_helper("observe", peer["slot"])
                if expanded and peer_has_stopped(peer, guest):
                    # Cleanup still owns this slot but its stopped VM can no
                    # longer grow. Capacity was observed before the stop.
                    continue
                if guest.get("resource_id") != peer["id"] or guest.get("ActiveState") != "active":
                    raise CapacityUnavailable("parallel peer is no longer active")
                try:
                    # MemAvailable already includes reclaimable cache. Credit
                    # only the peer's resident anonymous memory here.
                    peer_limit = peer["details"].get("memory_limit_bytes", slot_memory_limit(peer["slot"]))
                    memory = parallel_memory_group(peer["slot"], peer_limit) / "memory.stat"
                    peer_memory = next(
                        int(line.split()[1]) for line in memory.read_text().splitlines()
                        if line.startswith("anon ")
                    )
                except (FileNotFoundError, StopIteration, ValueError) as error:
                    raise CapacityUnavailable("parallel peer memory is unavailable") from error
                memory_needed += max(0, peer_limit - peer_memory)
            if batch and budget["available_memory"] < memory_needed:
                available = budget["available_memory"]
                shortage = memory_needed - available
                error = NearMemoryLimit if shortage <= RAM_RECHECK_SHORTFALL else InsufficientMemory
                raise error(
                    "parallel dispatch would consume the 2 GiB host RAM reserve "
                    f"(slot={slot} repository={repo} job={job['id']} "
                    f"available_bytes={available} required_bytes={memory_needed})"
                )
            host_helper.verify_network()
            owned = {r["runner_name"]: r["details"].get("runner_id") for r in active
                     if r["repository"] == repo.removeprefix("newdarwindev/")}
            if any(owned.get(row["name"]) != row["id"]
                   for row in inventory(api, runners_path, "runners")):
                raise RoutingError("another runner is already registered")
            prepared = prepared_demo_job(prepared_layer, repo, job, workflow_blob)
            image, image_sha, manifest = IMAGE, IMAGE_SHA, MANIFEST
            if prepared:
                if backend.digest(IMAGE) != IMAGE_SHA or DEMO_LAYER.lstat().st_nlink != 1:
                    raise RoutingError("prepared Demo backing identity differs")
                image, image_sha, manifest = DEMO_LAYER, DEMO_LAYER_SHA, DEMO_LAYER_MANIFEST
            reservation = {"capacity": 3, "eligible_slots": (slot,)} if expanded and batch else {}
            resource = state.reserve(job | {"repository": repo.removeprefix("newdarwindev/")}, details={
                "isolated_direct_pilot": True, "image_sha256": image_sha, "run_id": args.run_id,
                "direct_batch": batch, "memory_limit_bytes": memory_limit,
                **({"guest_disk_gib": CORE_SEED_DISK_GIB} if core_seed else {}),
            }, **reservation)
            slot, identity, name = resource["slot"], resource["id"], resource["runner_name"]
            if compact:
                compact_identity = identity
            directory = STATE_DIRECTORY / "resources" / identity
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / "candidate.json").open("xb") as stream:
                stream.write((STATE_DIRECTORY / manifest).read_bytes())
            machine = backend.Backend(IsolatedMetadata(state, directory), image)
            timing = {}
            report = {
                "resource": identity, "slot": slot, "repository": repo, "capacity": budget,
                "job_name": job["name"], "workflow_blob": workflow_blob,
                "timing_seconds": timing,
            }
            runner_id = None
            started = checkpoint = time.monotonic()

            def elapsed(stage):
                nonlocal checkpoint
                now = time.monotonic()
                timing[stage] = round(now - checkpoint, 3)
                checkpoint = now
            try:
                proof = getattr(args, "verified_image", None)
                if image == IMAGE and proof is not None:
                    machine.create(resource, network_mode="verify", verified_image=proof)
                else:
                    machine.create(resource, network_mode="verify")
                verify_guest_lifetime(slot)
                if batch:
                    parallel_memory_group(slot, memory_limit)
                deadline = time.monotonic() + 220
                while not machine.ready(resource):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("disposable guest did not become ready")
                    time.sleep(2)
                elapsed("boot_and_ready")
                machine.isolation(resource, check_tooling=False)
                elapsed("isolation")
                print("Guest online and isolation verified", flush=True)
                if batch and expanded:
                    # Cross-repository input preparation cannot conflict with
                    # the exact repository runner-registration inventory.
                    admission.close()

                if (
                    repo == TETHER
                    and workflow_blob in TETHER_VISUAL_WORKFLOWS | TETHER_UPSTREAM_WORKFLOWS
                    and job["name"] in {"validation / component", "validation / quality"}
                ):
                    stage_wheelhouse(
                        machine, resource, STATE_DIRECTORY / TETHER_VISUAL_WHEELHOUSE,
                        TETHER_VISUAL_SUMS, "tether-visual",
                    )
                if (
                    repo == TETHER and (
                        (workflow_blob in TETHER_UPSTREAM_WORKFLOWS
                         and job["name"] == "validation / quality")
                        or (workflow_blob in TETHER_UPSTREAM_COMPONENT_WORKFLOWS
                            and job["name"] == "validation / component")
                    )
                ):
                    stage_wheelhouse(
                        machine, resource, TETHER_UPSTREAM_WHEELHOUSE,
                        TETHER_UPSTREAM_SUMS, "tether-upstream",
                    )
                if (
                    repo == TETHER and workflow_blob == TETHER_NODE_QUALITY_WORKFLOW
                    and job["name"] == "validation / quality"
                ):
                    archive = STATE_DIRECTORY / "downloads/quality-node-modules.tar.gz"
                    with archive.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != TETHER_NODE_QUALITY_ARCHIVE_SHA:
                            raise RuntimeError("Tether Node quality archive changed")
                    machine.copy_prepared(resource, archive, archive.name)
                if (
                    repo == BROWSER
                    and job["name"] in {"validation / component", "validation / quality"}
                ):
                    stage_wheelhouse(
                        machine, resource, STATE_DIRECTORY / BROWSER_PROFILE_WHEELHOUSE,
                        BROWSER_PROFILE_SUMS, "browser-profile",
                    )
                if (
                    repo == CONSOLE and workflow_blob in CONSOLE_UI_WORKFLOWS
                    and job["name"] == "validation / quality"
                ):
                    stage_console_ui(machine, resource, STATE_DIRECTORY)
                if repo == HOST and (
                    job["name"] == "validation / rust"
                    or job["name"].startswith("validation / mutation_")
                ):
                    stage_host_cargo(machine, resource, job)
                if prepared:
                    verify_prepared_demo(machine, resource)
                elif repo == DEMO:
                    stage_demo(machine, resource, job)
                if repo == DEMO and workflow_blob == DEMO_CHECKOUT_WORKFLOW and job["name"] == "verify":
                    stage_checkout(
                        machine, resource, "demo-checkout",
                        DEMO_CHECKOUT_ARCHIVE, DEMO_CHECKOUT_ARCHIVE_SHA,
                    )
                elif repo == LAB and workflow_blob == LAB_CHECKOUT_WORKFLOW and job["name"] == "verify":
                    archive, digest = LAB_MAIN_CHECKOUT_ARCHIVE, LAB_MAIN_CHECKOUT_ARCHIVE_SHA
                    if job.get("head_sha") == LAB_CHECKOUT_HEAD:
                        archive, digest = LAB_CHECKOUT_ARCHIVE, LAB_CHECKOUT_ARCHIVE_SHA
                    stage_checkout(machine, resource, "lab-checkout", archive, digest)
                if core_seed:
                    stage_core_seed(machine, resource)
                elapsed("input_preparation")

                def verify_guest():
                    if batch and args.batch_stopping.is_set():
                        raise RuntimeError("manager draining before runner listener startup")
                    active = installed_helper("observe", slot)
                    if active.get("resource_id") != identity or active.get("ActiveState") != "active":
                        raise RuntimeError("disposable guest ownership changed")
                    verify_guest_lifetime(slot)

                def record_runner(value):
                    nonlocal runner_id
                    runner_id = value
                    state.update(resource, "registered", runner_id=value)
                    print(f"GitHub runner {value} registered for job {args.job_id} in slot {slot}", flush=True)

                def ssh(script):
                    try:
                        return machine.ssh(resource, script, timeout=100, check=False)
                    except subprocess.TimeoutExpired as error:
                        raise RuntimeError(
                            f"runner enrollment SSH exceeded {error.timeout:g} seconds"
                        ) from error

                if repo == CANARY:
                    def admit():
                        verify_guest()
                        # GitHub can update queue metadata while the immutable
                        # requested identity and unassigned status stay valid.
                        lookup()

                    record_runner(configure(
                        api, ssh, repo, REPOS[repo], name, job["labels"][0], admit=admit,
                    ))
                    start_listener(ssh, admit=admit)
                else:
                    enroll_job(
                        api, api, ssh, route, args.run_id, args.job_id, name,
                        actor_ids={OWNER}, verify_guest=verify_guest, record_runner=record_runner,
                    )
                elapsed("enrollment")
                admission.close()
                report["job"] = observe_result(
                    repo, args.run_id, job, runner_id, name, 210 if repo == CANARY else 960,
                )
                elapsed("job_observation")
                print(json.dumps(report["job"]), flush=True)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                report["error"] = str(error)[:400]
                print(report["error"], flush=True)
            finally:
                checkpoint = time.monotonic()
                errors = []
                try:
                    current = installed_helper("observe", slot)
                    if current.get("resource_id") == identity:
                        memory_peak = Path(
                            f"/sys/fs/cgroup/system.slice/runspindle-vm-{slot}.service/memory.peak"
                        )
                        try:
                            report["guest_memory_peak_bytes"] = int(memory_peak.read_text())
                        except (OSError, ValueError):
                            pass
                        installed_helper("stop", slot, identity)
                    current = installed_helper("observe", slot)
                    if int(current.get("MainPID", 0)) or Path(
                        f"/sys/fs/cgroup/system.slice/runspindle-vm-{slot}.service"
                    ).exists():
                        raise RuntimeError("guest cgroup remains")
                except Exception as error:
                    errors.append(f"guest stop: {type(error).__name__}")
                if not errors:
                    try:
                        if current.get("resource_id") == identity:
                            retain_stopped(
                                state, resource, machine.slot(resource), backend, inspect_linked_disk,
                                verified_base=getattr(args, "verified_image", None) if compact_enabled else None,
                            )
                        elif any(machine.slot(resource).iterdir()):
                            raise RuntimeError("unowned files remain in slot")
                    except Exception as error:
                        errors.append(f"retention: {type(error).__name__}")
                    try:
                        remove_owned_runner(repo, name, job["labels"][0], runner_id)
                    except Exception as error:
                        errors.append(f"runner cleanup: {type(error).__name__}")
                elapsed("cleanup")
                timing["total"] = round(time.monotonic() - started, 3)
                report["cleanup"] = "verified" if not errors else "uncertain"
                report["cleanup_errors"] = errors
                state.update(resource, "destroyed" if not errors else "uncertain", pilot_result=report)
                print(json.dumps(report), flush=True)
            wanted = "failure" if repo == CANARY and args.name == "failure-proof" else "success"
            if errors or "job" not in report:
                return 2
            return 0 if report["job"]["conclusion"] == wanted else 1
    finally:
        state.db.close()
        backend.helper = installed_helper


def drain_host(args):
    """Serve the eight jobs of one authenticated Host push, one guest at a time."""
    route = Route(HOST, REPOS[HOST], HOST_WORKFLOW, args.workflow_blob)
    path = f"/repos/{HOST}/actions/runs/{args.run_id}"
    completed = set()
    deadline = time.monotonic() + 150 * 60
    wait_for_job = time.monotonic() + 90
    while time.monotonic() < deadline:
        execution = api("GET", path)
        if execution.get("status") == "completed":
            return 0 if execution.get("conclusion") == "success" and len(completed) == 8 else 1
        _, jobs = queued_jobs(api, route, args.run_id, actor_ids={OWNER})
        if not jobs:
            if time.monotonic() >= wait_for_job:
                raise TimeoutError("Host workflow did not make another job available")
            time.sleep(3)
            continue
        first = min(jobs, key=lambda job: (job["name"] != "validation / component", job["id"]))
        args.job_id = first["id"]
        if args.job_id in completed:
            raise RuntimeError("job reappeared after pilot reconciliation")
        if run(args) == 2:
            return 2
        completed.add(args.job_id)
        wait_for_job = time.monotonic() + 90
    raise TimeoutError("Host run exceeded its elapsed budget")


@lru_cache(maxsize=128)
def discovery_workflow_blob(read_api, path):
    workflow = read_api("GET", path)
    sha = workflow.get("sha") if isinstance(workflow, dict) else None
    if (
        not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or workflow.get("type") != "file"
    ):
        raise RoutingError("workflow file identity unavailable during discovery")
    return sha


def discovery_api(method, path):
    # Revalidate discovery job lists with GitHub on every lookup. Admission
    # uses the original API and fetches full, unconditional responses.
    if method == "GET" and re.fullmatch(
        r"/repos/[^/]+/[^/]+/contents/\.github/workflows/[^/?]+\?ref=[0-9a-f]{40}",
        path,
    ):
        return {"type": "file", "sha": discovery_workflow_blob(api, path)}
    if method == "GET" and re.fullmatch(
        r"/repos/[^/]+/[^/]+/actions/runs/[0-9]+/attempts/[0-9]+/jobs\?per_page=100&page=[0-9]+",
        path,
    ):
        return queue_listing_api(method, path, field="jobs")
    if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/[0-9]+", path):
        return discovery_run_api(method, path)
    return api(method, path)


_queue_listing_cache = OrderedDict()
_queue_listing_cache_lock = Lock()
_DISCOVERY_JOB_FIELDS = (
    "id", "name", "run_id", "run_attempt", "head_sha", "labels", "status",
    "runner_id", "runner_name",
)


def revalidate_discovery(method, path, project):
    with _queue_listing_cache_lock:
        entry = _queue_listing_cache.get(path)
    response, etag = api(method, path, include=True, etag=entry[0] if entry else None)
    if response is None:
        if entry is None:
            raise GitHubUnavailable("GitHub returned no queue listing")
        etag, projected = entry
    else:
        projected = project(response)
    with _queue_listing_cache_lock:
        if valid_etag(etag):
            _queue_listing_cache[path] = etag, projected
            _queue_listing_cache.move_to_end(path)
            if len(_queue_listing_cache) > 192:
                _queue_listing_cache.popitem(last=False)
        else:
            _queue_listing_cache.pop(path, None)
    return projected


def discovery_run_api(method, path):
    def project(run):
        fields = ("id", "workflow_id", "path", "event", "status", "head_sha", "head_branch", "run_attempt")
        row = {key: run.get(key) for key in fields}
        for key, keys in (
            ("actor", ("id",)),
            ("triggering_actor", ("id",)),
            ("repository", ("id", "full_name", "private")),
            ("head_repository", ("id", "full_name", "private")),
        ):
            item = run.get(key)
            row[key] = {field: item.get(field) for field in keys} if isinstance(item, dict) else None
        return row

    return revalidate_discovery(method, path, project)


def queue_listing_api(method, path, *, field="workflow_runs"):
    def project(response):
        rows = response.get(field) if isinstance(response, dict) else None
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise RoutingError("GitHub returned an invalid queue listing")
        fields = _DISCOVERY_JOB_FIELDS if field == "jobs" else ("id", "event", "status")
        return {
            "total_count": response.get("total_count"),
            field: [{key: row.get(key) for key in fields} for row in rows],
        }

    return revalidate_discovery(method, path, project)


def next_queued_job(
    routes, cursor, exclude=(), *, light_only=False, active_single_runs=(), compact_only=False,
):
    """Poll one rotation, preserving FIFO order within each repository."""
    # Leave capacity for terminal observations and runner removal.
    quota_reset_delay = github_rate_limit_delay(0, reserve=320)
    if quota_reset_delay:
        print(
            "GitHub queue discovery paused at the 320-request quota reserve. "
            f"Expected reset in {quota_reset_delay} seconds.",
            flush=True,
        )
        return None
    for offset in range(len(routes)):
        index = (cursor + offset) % len(routes)
        route = routes[index]
        if light_only and not (
            light_workflow(route) or light_aggregate_workflow(route, route.workflow_blob)
        ):
            continue
        path = f"/repos/{route.repository}/actions/workflows/{route.workflow_id}/runs"
        run_ids = set()
        def runs_with_status(status):
            return inventory(queue_listing_api, path, "workflow_runs", {"event": "push", "status": status})

        try:
            # Join all readers before the scheduler can fork a guest worker.
            with ThreadPoolExecutor(max_workers=3) as readers:
                for rows in readers.map(runs_with_status, ("pending", "queued", "in_progress")):
                    run_ids.update(row["id"] for row in rows)
        except RoutingError as error:
            print(f"Using recent runs after incomplete workflow inventory for {route.repository}: {error}", flush=True)
            run_ids.clear()
        def candidate_for_run(run_id):
            try:
                # Admission rereads the exact attempt under the fleet lock.
                attempt, jobs = queued_jobs(
                    discovery_api, route, run_id, actor_ids={OWNER}, confirm_attempt=False,
                )
            except RoutingError:
                return None
            eligible = [
                job["id"] for job in jobs
                if (index, run_id, job["id"]) not in exclude
                and (not light_only or light_profile_job(route, job, attempt.workflow_blob))
                and (
                    not compact_only
                    or light_profile_job(route, job, attempt.workflow_blob)
                    or compact_profile_job(route.repository, job, attempt.workflow_blob)
                )
            ]
            return (index, run_id, min(eligible)) if eligible else None

        for scan_recent in (False, True):
            candidates = run_ids
            if scan_recent:
                # The status index sometimes omits runnable pushes, even
                # when it lists older runs with no eligible jobs left.
                try:
                    recent = queue_listing_api("GET", f"{path}?per_page=20")["workflow_runs"]
                except RoutingError as error:
                    print(f"Skipping invalid recent runs for {route.repository}: {error}", flush=True)
                    break
                candidates = {
                    row["id"] for row in recent
                    if type(row.get("id")) is int and row["id"] > 0
                    and row.get("event") == "push"
                    and row.get("status") in {"pending", "queued", "in_progress"}
                } - run_ids
            ordered = (run_id for run_id in sorted(candidates) if (index, run_id) not in active_single_runs)
            if not light_only:
                # Limit an opportunistic compact scan to the first few runs
                # in each listing. Normal discovery still scans the full queue.
                if compact_only:
                    ordered = islice(ordered, 3)
                for run_id in ordered:
                    if candidate := candidate_for_run(run_id):
                        return candidate
                continue
            # A light scan can cross many runs waiting for dependencies.
            # Resolve up to three in flight while considering answers in FIFO
            # order. Stop after the first batch with a usable candidate.
            batch = tuple(islice(ordered, 3))
            if batch:
                with ThreadPoolExecutor(max_workers=3) as readers:
                    while batch:
                        for candidate in readers.map(candidate_for_run, batch):
                            if candidate:
                                return candidate
                        batch = tuple(islice(ordered, 3))
    return None


def load_routes(routes_path):
    routes = [Route(**row) for row in json.loads(routes_path.read_text())]
    if not routes or len({route.repository_id for route in routes}) != len(routes):
        raise ValueError("expected unique approved repository routes")
    return routes


def light_slot_reserved(routes_path):
    """Read the slot assignment rather than inferring it from the job's route."""
    database = routes_path.parent / "fleet.sqlite"
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(
                "SELECT 1 FROM resources WHERE slot = 2 AND phase != 'destroyed' LIMIT 1"
            ).fetchone() is not None
    except sqlite3.OperationalError as error:
        raise RoutingError("fleet ledger unavailable for slot selection") from error


def last_direct_repository(routes_path):
    """Resume round-robin ordering after the most recent reconciled guest."""
    database = routes_path.parent / "fleet.sqlite"
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT repository FROM resources WHERE phase = 'destroyed' "
                "AND json_extract(details, '$.isolated_direct_pilot') = 1 "
                "ORDER BY updated DESC, id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError as error:
        raise RoutingError("fleet ledger unavailable for queue rotation") from error
    return f"newdarwindev/{row[0]}" if row else None


def run_worker(args):
    # The parent drains workers on shutdown. A direct SIGTERM still unwinds cleanup.
    def interrupted(*_):
        raise SystemExit(2)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(run(args))
    except GitHubRateLimited as error:
        print(str(error), flush=True)
        raise SystemExit(3) from error
    except (PeerStarting, NearMemoryLimit) as error:
        print(str(error), flush=True)
        raise SystemExit(CAPACITY_RECHECK) from error
    except InsufficientMemory as error:
        print(str(error), flush=True)
        raise SystemExit(MEMORY_DEFERRED) from error
    except CapacityUnavailable as error:
        print(str(error), flush=True)
        raise SystemExit(CAPACITY_DEFERRED) from error
    except (RoutingError, RunnerCleanupPending, GitHubUnavailable) as error:
        print(str(error), flush=True)
        raise SystemExit(2) from error


def run_parallel(routes_path, routes, first, stopping, *, verified_image=None):
    # Workers inherit the fleet lock, keeping other controllers excluded even
    # if the watcher dies. Each worker opens its own SQLite connection.
    if routes_path.parent != FLEET_STATE or FLEET_STATE.resolve(strict=True) != FLEET_STATE:
        raise RoutingError("parallel dispatch requires the canonical fleet state directory")
    capacity = 3 if light_slot_enabled() else 2
    batch = uuid4().hex
    process_context = get_context("fork")
    batch_stopping = process_context.Event()
    workers = {}
    light_workers = set()
    attempted = set()
    statuses = set()
    candidate = first
    general_index = first[0]
    # Give dedicated one-job workflows the first chance to fill the light
    # slot. Later polls resume rotation from the last light candidate.
    after_first = range(first[0] + 1, first[0] + 1 + len(routes))
    light_index = next(
        (index - 1 for index in after_first if light_workflow(routes[index % len(routes)])),
        first[0],
    )
    next_poll = 0
    light_fallback = False
    memory_alternative_tried = False
    deferred_memory_candidate = None
    with (FLEET_STATE / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            while candidate or workers:
                slot_freed = False
                for worker in tuple(workers):
                    if not worker.is_alive():
                        worker.join()
                        code = worker.exitcode
                        deferred = code in (CAPACITY_DEFERRED, CAPACITY_RECHECK, MEMORY_DEFERRED)
                        statuses.add(CAPACITY_DEFERRED if deferred else code)
                        finished = workers.pop(worker)
                        light_workers.discard(worker)
                        if deferred:
                            attempted.discard(finished)
                            if (
                                capacity == 3 and len(workers) == 1
                                and not light_workers and not light_workflow(routes[finished[0]])
                            ):
                                light_fallback = True
                            now = time.monotonic()
                            if (
                                code == MEMORY_DEFERRED and capacity == 3
                                and len(workers) == 2 and not memory_alternative_tried
                            ):
                                # A different workload can fit the same free slot.
                                # Try one alternative before returning to normal polling.
                                memory_alternative_tried = True
                                deferred_memory_candidate = finished
                                next_poll = 0
                            elif code == CAPACITY_RECHECK:
                                next_poll = now + CAPACITY_RECHECK_SECONDS
                            else:
                                next_poll = max(next_poll, now + VACANT_SLOT_POLL_SECONDS)
                        elif code in (0, 1):
                            slot_freed = True
                if slot_freed:
                    next_poll = 0
                    memory_alternative_tried = False
                    deferred_memory_candidate = None
                if stopping.is_set() or 3 in statuses:
                    batch_stopping.set()
                    break
                if candidate:
                    index, run_id, job_id = candidate
                    args = argparse.Namespace(
                        lane="direct", route=routes[index], run_id=run_id, job_id=job_id,
                        name=None, routes_path=routes_path, batch=batch, batch_stopping=batch_stopping,
                        verified_image=verified_image,
                    )
                    worker = process_context.Process(target=run_worker, args=(args,))
                    worker.start()
                    workers[worker] = candidate
                    if light_workflow(routes[index]):
                        light_workers.add(worker)
                    if capacity == 3 and len(workers) == 2:
                        next_poll = 0
                    attempted.add(candidate)
                    candidate = None
                elif workers and len(workers) < capacity and (
                    light_fallback or time.monotonic() >= next_poll
                ):
                    light_only = len(workers) == 1 and light_fallback
                    if len(workers) == 2:
                        possible_light_worker = light_workers or any(
                            light_aggregate_workflow(routes[index], routes[index].workflow_blob)
                            for index, _, _ in workers.values()
                        )
                        light_only = not possible_light_worker or not light_slot_reserved(routes_path)
                    light_fallback = False
                    cursor = light_index if light_only else general_index
                    queue_options = {"light_only": True} if light_only else {}
                    if light_workers:
                        # Lab and Next have only one job per run attempt. A
                        # rerun can be discovered after this worker releases it.
                        queue_options["active_single_runs"] = {
                            workers[worker][:2] for worker in light_workers
                        }
                    excluded = attempted
                    if deferred_memory_candidate is not None:
                        excluded = attempted | {deferred_memory_candidate}
                        if not light_only and enabled_flag(COMPACT_PROFILE_FLAG):
                            queue_options["compact_only"] = True
                        deferred_memory_candidate = None
                    candidate = next_queued_job(
                        routes, (cursor + 1) % len(routes), exclude=excluded, **queue_options,
                    )
                    if candidate:
                        if light_only:
                            light_index = candidate[0]
                        else:
                            general_index = candidate[0]
                    next_poll = time.monotonic() + VACANT_SLOT_POLL_SECONDS
                    if candidate:
                        continue
                stopping.wait(1)
        finally:
            # A poll failure or shutdown lets running jobs finish and prevents
            # a worker still waiting for admission from booting a guest.
            batch_stopping.set()
            for worker in workers:
                worker.join()
                statuses.add(worker.exitcode)
        return statuses, routes[general_index].repository


def github_rate_limit_delay(fallback, *, reserve=0):
    """Wait through a known primary quota reset, with backoff as a fallback."""
    try:
        response = api("GET", "/rate_limit")
    except (GitHubRateLimited, GitHubUnavailable, GitHubRejected):
        return fallback
    resources = response.get("resources") if isinstance(response, dict) else None
    core = resources.get("core") if isinstance(resources, dict) else None
    remaining = core.get("remaining") if isinstance(core, dict) else None
    if type(remaining) is int and 0 <= remaining <= reserve:
        reset = core.get("reset")
        if type(reset) is int:
            delay = reset - int(time.time()) + 5
            if 0 < delay <= 3605:
                return delay
    return fallback


def watch(routes_path, stopping, *, reuse_boot_digest=False):
    """Dispatch exact jobs from approved workflows into eligible slots."""
    previous_repository = None
    needs_cursor = True
    rate_limit_delay = 300
    verified_image = None
    while not stopping.is_set():
        try:
            pending_repositories = run(argparse.Namespace(lane="cleanup"))
            routes = [
                route for route in load_routes(routes_path)
                if route.repository not in pending_repositories
            ]
            if needs_cursor:
                previous_repository = last_direct_repository(routes_path)
                needs_cursor = False
            cursor = next(
                ((index + 1) % len(routes) for index, route in enumerate(routes)
                 if route.repository == previous_repository),
                0,
            )
            candidate = next_queued_job(routes, cursor)
            if stopping.is_set():
                break
            if candidate is None:
                rate_limit_delay = 300
                stopping.wait(60)
                continue
            if reuse_boot_digest and light_slot_enabled() and enabled_flag(COMPACT_PROFILE_FLAG):
                from runner_fleet.backend import VerifiedBootImage

                if verified_image is not None:
                    try:
                        verified_image.check(IMAGE)
                    except (OSError, ValueError):
                        verified_image = None
                if verified_image is None:
                    started = time.monotonic()
                    try:
                        verified_image = VerifiedBootImage.verify(IMAGE, IMAGE_SHA)
                    except (OSError, ValueError) as error:
                        raise RoutingError(f"Sealed boot image verification failed: {error}") from error
                    print(f"Verified sealed boot image in {time.monotonic() - started:.3f} seconds", flush=True)
            else:
                verified_image = None
            statuses, previous_repository = run_parallel(
                routes_path, routes, candidate, stopping, verified_image=verified_image,
            )
            if 3 in statuses:
                raise GitHubRateLimited("GitHub rate-limited a parallel worker")
            # A completed job freed capacity. Retry a deferred admission
            # immediately unless the batch also encountered a dispatch error.
            if statuses & {0, 1}:
                statuses.discard(CAPACITY_DEFERRED)
            status = 2 if statuses - {0, 1} else 0
        except GitHubRateLimited as error:
            delay = github_rate_limit_delay(rate_limit_delay)
            print(f"{error}. Retrying in {delay} seconds.", flush=True)
            stopping.wait(delay)
            rate_limit_delay = min(rate_limit_delay * 2, 3600)
            continue
        except (BlockingIOError, CapacityUnavailable, RoutingError,
                RunnerCleanupPending, GitHubUnavailable) as error:
            print(str(error), flush=True)
            stopping.wait(60)
            continue
        rate_limit_delay = 300
        if status == 2:
            stopping.wait(60)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("canary", "host", "watch"))
    parser.add_argument("run_id", type=int, nargs="?")
    parser.add_argument("job_id", type=int, nargs="?")
    parser.add_argument("--name", choices=("proof", "failure-proof"), default="proof")
    parser.add_argument("--workflow-blob")
    parser.add_argument("--routes", type=Path)
    args = parser.parse_args()
    if args.lane == "watch":
        if not args.routes:
            parser.error("watch requires an approved route file")
    elif args.run_id is None:
        parser.error("the pilot requires an exact run ID")
    if args.job_id is None and args.lane == "canary":
        parser.error("the canary requires an exact job ID")
    if args.lane == "host" and not args.workflow_blob:
        parser.error("the Host pilot requires the approved workflow blob")

    def interrupted(*_):
        # Exit through the cleanup blocks even if SIGTERM arrives during I/O.
        raise SystemExit(0)

    if args.lane == "watch":
        stopping = Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        return watch(args.routes, stopping, reuse_boot_digest=True)
    signal.signal(signal.SIGTERM, interrupted)
    return drain_host(args) if args.job_id is None else run(args)


if __name__ == "__main__":
    sys.exit(main())
