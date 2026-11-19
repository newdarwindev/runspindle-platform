"""Run one exact GitHub job in a disposable Strix VM under the fleet lock."""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time

from tools.runner_fleet.direct_routing import enroll_job, queued_jobs
from tools.runner_fleet.ephemeral_registration import LABEL, configure, start_listener
from tools.runner_fleet.github_jobs import DIRECT_REPOSITORIES, Route, RoutingError, inventory

SUPPORT = Path("/home/standard/Projects/runspindle-platform-automatic-fleet-20261118")
CANARY = "newdarwindev/runspindle-strix-ci-canary"
HOST = "newdarwindev/runspindle-host"
REPOS = {CANARY: 1463722751, HOST: 1456479612}
HOST_WORKFLOW = 417698603
OWNER = 329000935
IMAGE = Path("/var/lib/runspindle-fleet/images/candidate-45811b5b28f5.qcow2")
IMAGE_SHA = "a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0"
MANIFEST = "registered-probe-3c27c78e84054c84a9ef2f45622834c7/candidate.json"
CANARY_HEAD = "722739a047f320d029180b0322a3f2429e14924a"
CANARY_BRANCH = "ci/strix-canary-exclusive-label-20261119"
CANARY_WORKFLOW = 423463385


class CapacityUnavailable(RuntimeError):
    """The host lacks the current reserve for another guest."""


class GitHubUnavailable(RuntimeError):
    """A transient GitHub API failure."""


class GitHubRejected(RuntimeError):
    """An API response with an explicit failure status."""

    def __init__(self, method, status):
        super().__init__(f"GitHub {method} rejected (HTTP {status})")
        self.status = status


class RunnerCleanupPending(RuntimeError):
    """GitHub has yet to release a stopped guest's runner."""


def api(method, path, expected=200):
    args = ["gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "X-GitHub-Api-Version: 2026-03-10", path]
    retries = (0, 2, 5) if method == "GET" else (0,)
    for delay in retries:
        if delay:
            time.sleep(delay)
        try:
            return api_once(args, method)
        except GitHubUnavailable as error:
            last_error = error
    raise last_error


def api_once(args, method):
    try:
        result = subprocess.run(args, capture_output=True, timeout=18, check=False, text=True)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubUnavailable(f"GitHub {method} transport unavailable") from None
    if result.returncode:
        match = re.search(r"\(HTTP ([0-9]{3})\)", result.stderr)
        status = int(match[1]) if match else None
        throttled = status == 429 or (
            status == 403 and "rate limit" in result.stderr.lower()
        )
        if throttled:
            raise RuntimeError(f"GitHub {method} rate-limited (HTTP {status})")
        if status is None or status == 408 or 500 <= status <= 599:
            raise GitHubUnavailable(f"GitHub {method} temporarily unavailable (HTTP {status})")
        raise GitHubRejected(method, status)
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
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


def observe_result(repo, run_id, job, runner, name, seconds):
    path = f"/repos/{repo}/actions/jobs/{job['id']}"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            row = api("GET", path)
        except GitHubUnavailable as error:
            print(str(error), flush=True)
            time.sleep(5)
            continue
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
        time.sleep(3)
    raise TimeoutError("GitHub job observation timed out")


def network_read(*args):
    allowed = (
        args == ("nft", "-s", "-nn", "list", "table", "inet", "runspindle_fleet")
        or args in (
            ("ip", "-j", "-n", "runspindle-canary", "route", "show"),
            ("ip", "-j", "-n", "runspindle-canary", "-d", "addr", "show", "dev", "rscan1"),
        )
        or args in {("ip", "-j", "-d", "addr", "show", "dev", n) for n in ("rsvm0", "rsvm1", "rscan0")}
        or args in {("systemctl", "is-active", f"runspindle-{n}.service")
                    for n in ("proxy-0", "proxy-1", "host-canary", "lan-canary")}
    )
    if not allowed:
        raise RuntimeError("unsupported network observation")
    return subprocess.run(("sudo", "-n", *args), capture_output=True, text=True, check=True, timeout=8)


def retain_stopped(state, resource, slot_path, backend, inspect_disk):
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
        entries.append({
            "source": str(source), "destination": str(archive / name),
            "inode": info.st_ino, "size": info.st_size, "sha256": backend.digest(source),
        })
    if {item.name for item in slot_path.iterdir()} != {Path(row["source"]).name for row in entries}:
        raise RuntimeError("unknown input remains in the stopped slot")
    disk = slot_path / "disk.qcow2"
    binding = inspect_disk(disk, IMAGE, IMAGE_SHA, backend.execute) if disk.exists() else None
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


def reconcile_stopped_direct(state, backend, helper):
    resources = state.active()
    if not resources:
        return
    if len(resources) != 1:
        raise RuntimeError("multiple fleet slots require reconciliation")
    resource, = resources
    details = resource["details"]
    identity, slot = resource["id"], resource["slot"]
    repo = f"newdarwindev/{resource['repository']}"
    guest = helper("observe", slot)
    if (
        resource["phase"] != "uncertain"
        or details.get("isolated_direct_pilot") is not True
        or details.get("image_sha256") != IMAGE_SHA
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
    remove_owned_runner(repo, resource["runner_name"], label, details.get("runner_id"))
    state.update(resource, "destroyed", direct_recovery={
        "runner_id": details.get("runner_id"), "job_id": reserved["id"],
        "runner_absent": True, "vm_cgroup_absent": True, "originals_retained": True,
    })
    print(f"Recovered cleanup for direct runner resource {identity}", flush=True)


def run(args):
    if socket.gethostname().split(".")[0] != "kali-strix1":
        raise RuntimeError("expected kali-strix1")
    support_sha = subprocess.check_output(
        ["git", "-C", str(SUPPORT), "rev-parse", "HEAD"], text=True,
    ).strip()
    if support_sha != "45bb4dea6f0412a20bb04a6837e39f0d665d7e36" or subprocess.check_output(
        ["git", "-C", str(SUPPORT), "status", "--porcelain"], text=True,
    ):
        raise RuntimeError("reviewed fleet support checkout changed")
    support_path = str(SUPPORT / "tools")
    if support_path not in sys.path:
        sys.path.insert(0, support_path)
    from runner_fleet import backend, host_helper
    from runner_fleet.registered_probe import capacity
    from runner_fleet.original_retention import inspect_linked_disk
    from runner_fleet.single_probe import (
        IsolatedMetadata, STATE_DIRECTORY, manager_stopped,
        validate_state_directory, verify_guest_lifetime, verify_host_lifetime_policy,
    )
    from runner_fleet.state import State

    installed_helper = backend.helper
    host_helper.run = network_read

    def read_only_helper(command, *values):
        if command == "verify-network":
            return host_helper.verify_network()
        if command not in ("observe", "start", "stop"):
            raise RuntimeError("unsupported fleet helper operation")
        return installed_helper(command, *values)

    backend.helper = read_only_helper
    route = getattr(args, "route", None)
    if args.lane == "host":
        route = Route(HOST, REPOS[HOST], HOST_WORKFLOW, args.workflow_blob)
    repo = route.repository if route else CANARY

    def lookup():
        if repo == CANARY:
            return canary_job(args.run_id, args.job_id, args.name)
        _, jobs = queued_jobs(api, route, args.run_id, actor_ids={OWNER})
        found = [job for job in jobs if job["id"] == args.job_id]
        if len(found) != 1:
            raise RuntimeError("requested direct job is not queued")
        return found[0]

    manager_stopped()
    verify_host_lifetime_policy()
    validate_state_directory(STATE_DIRECTORY)
    state = State(STATE_DIRECTORY)
    runners_path = f"/repos/{repo}/actions/runners"
    try:
        with state.lock():
            manager_stopped()
            reconcile_stopped_direct(state, backend, installed_helper)
            if args.lane == "cleanup":
                return 0
            try:
                budget = capacity(STATE_DIRECTORY)
            except ValueError as error:
                raise CapacityUnavailable("one-VM capacity is temporarily unavailable") from error
            host_helper.verify_network()
            if inventory(api, runners_path, "runners"):
                raise RuntimeError("another runner is already registered")
            job = lookup()
            resource = state.reserve(job | {"repository": repo.removeprefix("newdarwindev/")}, details={
                "isolated_direct_pilot": True, "image_sha256": IMAGE_SHA, "run_id": args.run_id,
            })
            slot, identity, name = resource["slot"], resource["id"], resource["runner_name"]
            directory = STATE_DIRECTORY / "resources" / identity
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / "candidate.json").open("xb") as stream:
                stream.write((STATE_DIRECTORY / MANIFEST).read_bytes())
            machine = backend.Backend(IsolatedMetadata(state, directory), IMAGE)
            report = {"resource": identity, "slot": slot, "repository": repo, "capacity": budget}
            runner_id = None
            try:
                machine.create(resource, network_mode="verify")
                verify_guest_lifetime(slot)
                deadline = time.monotonic() + 220
                while not machine.ready(resource):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("disposable guest did not become ready")
                    time.sleep(2)
                machine.isolation(resource, check_tooling=False)
                print("Guest online and isolation verified", flush=True)

                def verify_guest():
                    active = installed_helper("observe", slot)
                    if active.get("resource_id") != identity or active.get("ActiveState") != "active":
                        raise RuntimeError("disposable guest ownership changed")
                    verify_guest_lifetime(slot)

                def record_runner(value):
                    nonlocal runner_id
                    runner_id = value
                    state.update(resource, "registered", runner_id=value)
                    print(f"GitHub runner {value} registered for job {args.job_id}", flush=True)

                def ssh(script):
                    return machine.ssh(resource, script, timeout=100, check=False)

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
                report["job"] = observe_result(
                    repo, args.run_id, job, runner_id, name, 210 if repo == CANARY else 960,
                )
                print(json.dumps(report["job"]), flush=True)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                report["error"] = str(error)[:400]
                print(report["error"], flush=True)
            finally:
                errors = []
                try:
                    current = installed_helper("observe", slot)
                    if current.get("resource_id") == identity:
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
                            )
                        elif any(machine.slot(resource).iterdir()):
                            raise RuntimeError("unowned files remain in slot")
                    except Exception as error:
                        errors.append(f"retention: {type(error).__name__}")
                    try:
                        remove_owned_runner(repo, name, job["labels"][0], runner_id)
                    except Exception as error:
                        errors.append(f"runner cleanup: {type(error).__name__}")
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


def next_queued_job(routes, cursor):
    """Poll one rotation, preserving FIFO order within each repository."""
    for offset in range(len(routes)):
        index = (cursor + offset) % len(routes)
        route = routes[index]
        path = f"/repos/{route.repository}/actions/workflows/{route.workflow_id}/runs"
        run_ids = set()
        for status in ("pending", "queued", "in_progress"):
            rows = inventory(api, path, "workflow_runs", {"event": "push", "status": status})
            run_ids.update(row["id"] for row in rows)
        for run_id in sorted(run_ids):
            try:
                _, jobs = queued_jobs(api, route, run_id, actor_ids={OWNER})
            except RoutingError:
                continue
            if jobs:
                return index, run_id, min(job["id"] for job in jobs)
    return None


def watch(routes_path):
    """Poll approved workflows and serve one direct job at a time."""
    routes = [Route(**row) for row in json.loads(routes_path.read_text())]
    if not routes or len({route.repository_id for route in routes}) != len(routes):
        raise ValueError("expected unique approved repository routes")
    cursor = 0
    while True:
        try:
            run(argparse.Namespace(lane="cleanup"))
            candidate = next_queued_job(routes, cursor)
            if candidate is None:
                time.sleep(60)
                continue
            index, run_id, job_id = candidate
            status = run(argparse.Namespace(
                lane="direct", route=routes[index], run_id=run_id, job_id=job_id, name=None,
            ))
        except (BlockingIOError, CapacityUnavailable, RoutingError,
                RunnerCleanupPending, GitHubUnavailable) as error:
            print(str(error), flush=True)
            time.sleep(60)
            continue
        cursor = (index + 1) % len(routes)
        if status == 2:
            time.sleep(60)


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
        raise InterruptedError("pilot interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    if args.lane == "watch":
        return watch(args.routes)
    return drain_host(args) if args.job_id is None else run(args)


if __name__ == "__main__":
    sys.exit(main())
