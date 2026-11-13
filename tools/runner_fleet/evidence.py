"""Collect GitHub job receipts without confusing workload and fleet qualification."""

import json
import os
import tempfile
import subprocess
from .model import classify_job, empty_report


def gh_json(arguments):
    result = subprocess.run(
        ["gh", *arguments], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def collect(cfg, revisions, receipts, log_dir):
    """Receipts map repo/lane to run ID. Fetch authoritative job data and logs.

    Does not trust caller-supplied conclusions. Does not certify isolation,
    capacity or recovery from a passing workload. Raw logs stay private.
    """
    report = empty_report(cfg, revisions)
    for row in report["coverage"]:
        repo = row["repository"]
        key = repo + "/" + row["lane"]
        run_id = receipts.get(key)
        if not run_id:
            continue
        if type(run_id) is not int or run_id <= 0:
            row["reason"] = "invalid run ID"
            continue
        try:
            run = gh_json(["api", f"repos/{repo}/actions/runs/{run_id}"])
            if run.get("head_sha") != row["requested_sha"]:
                row["reason"] = "workflow revision mismatch"
                continue
            if run.get("path") != ".github/workflows/fleet-qualification.yml":
                row["reason"] = "unexpected workflow"
                continue
            if run.get("event") not in ("workflow_dispatch", "push"):
                row["reason"] = "untrusted workflow event"
                continue
            pages = gh_json(
                [
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
                ]
            )
            jobs = [
                job
                for page in pages
                for job in page["jobs"]
                if job["name"] == row["lane"]
            ]
            if len(jobs) != 1:
                row["reason"] = "expected exactly one lane job"
                continue
            job = jobs[0]
            status, reason = classify_job(
                job, row["requested_sha"], cfg["lanes"][row["lane"]]
            )
            row.update(status=status, reason=reason)
            row["jobs"] = [
                {
                    k: job.get(k)
                    for k in (
                        "id",
                        "html_url",
                        "head_sha",
                        "runner_id",
                        "runner_name",
                        "labels",
                        "status",
                        "conclusion",
                        "started_at",
                        "completed_at",
                    )
                }
            ]
            if job["status"] == "completed":
                logs = subprocess.run(
                    ["gh", "api", f'repos/{repo}/actions/jobs/{job["id"]}/logs'],
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
                path = log_dir / f'{repo.split("/")[1]}-{row["lane"]}-{job["id"]}.log'
                fd, temporary = tempfile.mkstemp(dir=log_dir, prefix=".fleet-log-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(logs.stdout)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                row["log_path"] = str(path)
            else:
                row.update(
                    status="blocked",
                    reason="job queued or running; never counted as passed",
                )
        except (subprocess.SubprocessError, ValueError, KeyError, OSError) as error:
            # Do not copy gh stderr (which can include private data) into receipts.
            row.update(
                status="blocked",
                reason=f"GitHub evidence unavailable: {type(error).__name__}",
            )
    # Nine operational scenarios require live observations; workload receipts
    # alone deliberately cannot turn overall qualification green.
    return report
