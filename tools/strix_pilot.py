"""Run one exact GitHub job in a disposable Strix VM under the fleet lock."""

import argparse
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from tools.runner_fleet.direct_routing import enroll_job, queued_jobs
from tools.runner_fleet.ephemeral_registration import configure, start_listener
from tools.runner_fleet.private_relay import Route, inventory

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


def api(method, path, expected=200):
    args = ["gh", "api", "--hostname", "github.com", "--method", method, path]
    try:
        result = subprocess.run(args, capture_output=True, timeout=18, check=False, text=True)
        if result.returncode:
            raise RuntimeError(f"GitHub {method} failed")
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RuntimeError(f"GitHub {method} unavailable") from None


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
        row = api("GET", path)
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
    sys.path.insert(0, str(SUPPORT / "tools"))
    from runner_fleet import backend, host_helper
    from runner_fleet.registered_probe import capacity
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
    repo = CANARY if args.lane == "canary" else HOST
    route = Route(HOST, REPOS[HOST], HOST_WORKFLOW, args.workflow_blob) if repo == HOST else None

    def lookup():
        if repo == CANARY:
            return canary_job(args.run_id, args.job_id, args.name)
        _, jobs = queued_jobs(api, route, args.run_id, actor_ids={OWNER})
        found = [job for job in jobs if job["id"] == args.job_id]
        if len(found) != 1:
            raise RuntimeError("requested public job is not queued")
        return found[0]

    manager_stopped()
    verify_host_lifetime_policy()
    validate_state_directory(STATE_DIRECTORY)
    state = State(STATE_DIRECTORY)
    runners_path = f"/repos/{repo}/actions/runners"
    try:
        with state.lock():
            manager_stopped()
            if state.active():
                raise RuntimeError("fleet slot is occupied or unresolved")
            budget = capacity(STATE_DIRECTORY)
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
                        if lookup() != job:
                            raise RuntimeError("canary job changed during admission")

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
                report["error"] = str(error)[:180]
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
                try:
                    rows = inventory(api, runners_path, "runners")
                    for row in rows:
                        if row.get("name") == name and row.get("labels") == [
                            item for item in row.get("labels", []) if item.get("name") == job["labels"][0]
                        ] and len(row.get("labels", [])) == 1:
                            api("DELETE", f"{runners_path}/{row['id']}", 204)
                    if any(r.get("name") == name for r in inventory(api, runners_path, "runners")):
                        raise RuntimeError("owned runner remains registered")
                except Exception as error:
                    errors.append(f"runner cleanup: {type(error).__name__}")
                if not errors:
                    try:
                        if current.get("resource_id") == identity:
                            machine.destroy(resource)
                        elif any(machine.slot(resource).iterdir()):
                            raise RuntimeError("unowned files remain in slot")
                    except Exception as error:
                        errors.append(f"retention: {type(error).__name__}")
                report["cleanup"] = "verified" if not errors else "uncertain"
                report["cleanup_errors"] = errors
                state.update(resource, "destroyed" if not errors else "uncertain", pilot_result=report)
                print(json.dumps(report), flush=True)
            wanted = "failure" if repo == CANARY and args.name == "failure-proof" else "success"
            return 0 if report.get("job", {}).get("conclusion") == wanted and not errors else 1
    finally:
        state.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("canary", "host"))
    parser.add_argument("run_id", type=int)
    parser.add_argument("job_id", type=int)
    parser.add_argument("--name", choices=("proof", "failure-proof"), default="proof")
    parser.add_argument("--workflow-blob")
    args = parser.parse_args()
    def interrupted(*_):
        raise InterruptedError("pilot interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
