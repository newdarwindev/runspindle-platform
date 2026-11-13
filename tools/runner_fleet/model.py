"""Pure scheduling and evidence rules, independent of host virtualization."""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = (
    "runspindle",
    "runspindle-core",
    "runspindle-host",
    "runspindle-browser",
    "runspindle-tether",
    "runspindle-refinery",
    "runspindle-console",
    "runspindle-platform",
)
SCENARIOS = (
    "repository_coverage",
    "native_mac_coverage",
    "real_workload",
    "isolation",
    "freshness",
    "capacity_fairness",
    "recovery",
    "host_availability",
    "truthful_failures",
)


def config():
    value = json.loads((ROOT / "fleet/config.json").read_text())
    if value["owner"] != "newdarwindev" or tuple(value["repositories"]) != REPOSITORIES:
        raise ValueError("Fleet repository allowlist changed")
    return value


def choose_jobs(cfg, host, queued, active, cursor=0, drained=False):
    """One reservation per queued job, round-robin, capacity shared by lanes.

    Reservations include booting/unknown resources; callers must reconcile them
    before removing them. Existing persistent runners are never counted as ours.
    """
    if drained:
        return [], cursor
    slots = max(0, cfg["hosts"][host]["capacity"] - len(active))
    # GitHub may still report a booting reservation as queued. Job IDs are
    # repository scoped; unidentifiable entries cannot reserve infrastructure.
    reserved = {(j.get("repository"), j.get("id")) for j in active if "id" in j}
    remaining = []
    seen = set(reserved)
    for job in queued:
        identity = (job.get("repository"), job.get("id"))
        if type(identity[1]) is not int or identity[1] <= 0 or identity in seen:
            continue
        seen.add(identity)
        remaining.append(job)
    selected = []
    repos = cfg["repositories"]
    while slots:
        for offset in range(len(repos)):
            index = (cursor + offset) % len(repos)
            repo = repos[index]
            match = next(
                (
                    j
                    for j in remaining
                    if j["repository"] == repo
                    and matching_lane(cfg, host, j["labels"]) is not None
                ),
                None,
            )
            if match is not None:
                selected.append(match)
                remaining.remove(match)
                cursor = (index + 1) % len(repos)
                slots -= 1
                break
        else:
            break
    return selected, cursor


def matching_lane(cfg, host, labels):
    requested = {label.lower() for label in labels}
    # Require explicit host and isolation labels. Generic self-hosted jobs do
    # not grant permission to provision disposable infrastructure.
    if not {"self-hosted", "runspindle-isolated", host} <= requested:
        return None
    for name, lane in cfg["lanes"].items():
        if lane["host"] == host and requested <= {x.lower() for x in lane["labels"]}:
            return name
    return None


def classify_job(job, sha, lane, runner_prefix="runspindle-fleet-"):
    """A green GitHub job alone does not qualify the requested lane or SHA."""
    if job.get("head_sha") != sha:
        return "blocked", "revision mismatch"
    if job.get("status") != "completed":
        return "blocked", "job not terminal"
    if not job.get("runner_name", "").startswith(runner_prefix):
        return "blocked", "runner is not fleet-owned"
    if not job.get("runner_id"):
        return "blocked", "runner identity missing"
    if not {x.lower() for x in lane["labels"]} <= {
        x.lower() for x in job.get("labels", [])
    }:
        return "blocked", "lane labels missing"
    if job.get("conclusion") != "success":
        return "failed", job.get("conclusion") or "missing conclusion"
    steps = job.get("steps", [])
    if not any(
        s.get("name") == "Run implemented validation"
        and s.get("conclusion") == "success"
        for s in steps
    ):
        return "blocked", "implemented workload evidence missing"
    return "passed", None


def empty_report(cfg, revisions):
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "evidence_class": "runner_qualification",
        "status": "blocked",
        "coverage": [
            {
                "repository": f'{cfg["owner"]}/{repo}',
                "lane": lane,
                "requested_sha": revisions.get(repo),
                "status": "blocked",
                "reason": "not executed",
                "jobs": [],
            }
            for repo in cfg["repositories"]
            for lane in cfg["lanes"]
        ],
        "scenarios": {
            name: {"status": "blocked", "reason": "not exercised"} for name in SCENARIOS
        },
        "product_acceptance": "not_run",
    }
