"""Admission checks for a repository's disposable Strix runner."""

from .github_jobs import LAB, NEXT, PARTITIONS, RoutingError, inventory, observe_push, positive
from .ephemeral_registration import configure, start_listener

JOB_NAMES = {
    f"validation / {name}": name for name in (*PARTITIONS, "strix-linux-x64")
}
CORE_SHARD_NAMES = {f"validation / auth_{index}": f"auth_{index}" for index in range(1, 6)}


def queued_jobs(api, route, run_id, *, actor_ids):
    """Return exact queued jobs with dedicated labels for an approved push."""
    if not actor_ids or any(not positive(actor_id) for actor_id in actor_ids):
        raise RoutingError("direct routing requires approved GitHub actor IDs")
    # The run may still report pending after GitHub releases individual jobs.
    # Only an exact, unassigned job that reports queued is eligible below.
    options = {"statuses": ("pending", "queued", "in_progress"), "actor_ids": actor_ids}
    attempt = observe_push(api, route, run_id, **options)
    jobs = inventory(
        api, f"{attempt.repository_path}/actions/runs/{run_id}/attempts/{attempt.attempt}/jobs", "jobs",
    )
    expected_names = JOB_NAMES
    if route.repository == "newdarwindev/runspindle-core":
        expected_names = JOB_NAMES | CORE_SHARD_NAMES
    elif route.repository in {LAB, NEXT}:
        expected_names = {"verify": "verify"}
    names = {job.get("name") for job in jobs}
    if len(names) != len(jobs) or not names.issubset(expected_names):
        raise RoutingError("unexpected direct job inventory")
    queued = []
    for job in jobs:
        suffix = expected_names[job["name"]]
        label = f"runspindle-strix-direct-{run_id}-{attempt.attempt}-{suffix}"
        if (
            job.get("run_id") != run_id
            or job.get("run_attempt") != attempt.attempt
            or job.get("head_sha") != attempt.sha
            or job.get("labels") != [label]
        ):
            raise RoutingError("unexpected direct job identity or label")
        if job.get("status") == "queued":
            if job.get("runner_id") not in (None, 0) or job.get("runner_name"):
                raise RoutingError("queued direct job already has a runner")
            queued.append(job)
    if observe_push(api, route, run_id, **options) != attempt:
        raise RoutingError("run attempt changed during job admission")
    return attempt, queued


def enroll_job(read_api, runner_api, ssh, route, run_id, job_id, runner_name, *,
               actor_ids, verify_guest, record_runner):
    """Enroll one isolated guest under the fleet lock and durable reservation."""
    attempt, jobs = queued_jobs(read_api, route, run_id, actor_ids=actor_ids)
    intended = next((job for job in jobs if job["id"] == job_id), None)
    if intended is None:
        raise RoutingError("requested job is no longer queued")
    label, = intended["labels"]

    def admit():
        verify_guest()
        current, jobs = queued_jobs(read_api, route, run_id, actor_ids=actor_ids)
        if current != attempt or not any(
            job["id"] == job_id and job["labels"] == [label] for job in jobs
        ):
            raise RoutingError("requested job changed before listener startup")

    runner_id = configure(
        runner_api, ssh, route.repository, route.repository_id, runner_name, label, admit=admit,
    )
    record_runner(runner_id)
    start_listener(ssh, admit=admit)
    return runner_id
