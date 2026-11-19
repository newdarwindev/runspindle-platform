"""Admission checks for a public repository's disposable Strix runner."""

from .private_relay import PARTITIONS, RelayError, inventory, observe_public, positive

JOB_NAMES = {
    f"validation / {name}": name for name in (*PARTITIONS, "strix-linux-x64")
}


def queued_jobs(api, route, run_id, *, actor_ids):
    """Return exact queued jobs with dedicated labels for an approved push."""
    if not actor_ids or any(not positive(actor_id) for actor_id in actor_ids):
        raise RelayError("direct routing requires approved GitHub actor IDs")
    options = {"statuses": ("queued", "in_progress"), "actor_ids": actor_ids}
    attempt = observe_public(api, route, run_id, **options)
    jobs = inventory(
        api, f"{attempt.public_path}/actions/runs/{run_id}/attempts/{attempt.attempt}/jobs", "jobs",
    )
    names = {job.get("name") for job in jobs}
    if len(names) != len(jobs) or not names.issubset(JOB_NAMES):
        raise RelayError("unexpected direct job inventory")
    queued = []
    for job in jobs:
        suffix = JOB_NAMES[job["name"]]
        label = f"runspindle-strix-direct-{run_id}-{attempt.attempt}-{suffix}"
        if (
            job.get("run_id") != run_id
            or job.get("run_attempt") != attempt.attempt
            or job.get("head_sha") != attempt.sha
            or job.get("labels") != [label]
        ):
            raise RelayError("unexpected direct job identity or label")
        if job.get("status") == "queued":
            if job.get("runner_id") not in (None, 0) or job.get("runner_name"):
                raise RelayError("queued direct job already has a runner")
            queued.append(job)
    if observe_public(api, route, run_id, **options) != attempt:
        raise RelayError("public run attempt changed during job admission")
    return attempt, queued
