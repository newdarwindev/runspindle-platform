"""GitHub transport and durable handoff for the private Strix validation lane."""

from dataclasses import dataclass
import http.client
import json
import re
from urllib.parse import urlencode


CHECK_NAME = "Runspindle Strix execution"
PARTITIONS = ("quality", "component", "rust", "mutation_1", "mutation_2", "mutation_3", "mutation_4")
JOBS = (*PARTITIONS, "aggregate")
SHA = re.compile(r"[0-9a-f]{40}")
PUBLIC_REPOSITORIES = frozenset(
    f"newdarwindev/runspindle{suffix}" for suffix in
    ("", "-platform", "-core", "-host", "-browser", "-tether", "-refinery", "-console")
)
SOURCE_PATH = ".github/workflows/validation.yml"
PRIVATE_PATH = ".github/workflows/strix-validation.yml"
API_VERSION = "2026-03-10"


class RelayError(ValueError):
    """A fixed diagnostic suitable for the fleet journal."""


def positive(value):
    return type(value) is int and value > 0


class GitHub:
    """The caller supplies a fresh role-scoped credential without logging it."""

    def __init__(self, token):
        self._token = token

    def __call__(self, method, path, expected=200, data=None):
        if not path.startswith("/repos/") or any(c in path for c in "\r\n"):
            raise RelayError("unexpected GitHub API route")
        connection = http.client.HTTPSConnection("api.github.com", timeout=10)
        try:
            connection.request(method, path, None if data is None else json.dumps(data), {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self._token(),
                "Content-Type": "application/json",
                "User-Agent": "runspindle-strix-relay",
                "X-GitHub-Api-Version": API_VERSION,
            })
            response = connection.getresponse()
            if response.status != expected:
                raise RelayError(f"unexpected GitHub API status {response.status}")
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise RelayError("GitHub API response exceeded limit")
            return json.loads(raw)
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise RelayError("GitHub API response unavailable") from None
        finally:
            connection.close()


def inventory(api, path, field, filters=None):
    """Read a complete bounded inventory or leave its result unknown."""
    rows = []
    for page in range(1, 11):
        query = urlencode((filters or {}) | {"per_page": 100, "page": page})
        value = api("GET", f"{path}?{query}")
        batch = value.get(field)
        count = value.get("total_count")
        if type(batch) is not list or type(count) is not int or not 0 <= count <= 1000:
            raise RelayError("GitHub inventory is unavailable or oversized")
        rows.extend(batch)
        if (
            any(not positive(row.get("id")) for row in rows)
            or len({row["id"] for row in rows}) != len(rows)
            or len(rows) > count
        ):
            raise RelayError("GitHub inventory changed while paging")
        if len(rows) == count:
            return rows
        if len(batch) != 100:
            break
    raise RelayError("GitHub inventory pagination was incomplete")


@dataclass(frozen=True)
class Route:
    repository: str
    repository_id: int
    workflow_id: int
    workflow_blob: str

    def __post_init__(self):
        if (
            self.repository not in PUBLIC_REPOSITORIES
            or not positive(self.repository_id)
            or not positive(self.workflow_id)
            or not SHA.fullmatch(self.workflow_blob)
        ):
            raise RelayError("invalid approved public route")


@dataclass(frozen=True)
class Attempt:
    route: Route
    run_id: int
    attempt: int
    sha: str

    @property
    def key(self):
        return f"strix-v1:{self.route.repository_id}:{self.run_id}:{self.attempt}:{self.sha}"

    @property
    def public_path(self):
        return f"/repos/{self.route.repository}"


def observe_public(api, route, run_id, *, statuses=("in_progress",), actor_ids=None):
    """Read the normal event and pin the reporter bytes at its exact commit."""
    if not positive(run_id):
        raise RelayError("invalid public run ID")
    path = f"/repos/{route.repository}"
    run = api("GET", f"{path}/actions/runs/{run_id}")
    head = run.get("head_sha", "")
    branch = run.get("head_branch", "")
    if (
        run.get("id") != run_id
        or run.get("workflow_id") != route.workflow_id
        or run.get("path") != SOURCE_PATH
        or run.get("event") != "push"
        or run.get("status") not in statuses
        or not (branch == "main" or (isinstance(branch, str) and re.fullmatch(r"ci/.+", branch)))
        or not isinstance(head, str)
        or not SHA.fullmatch(head)
        or not positive(run.get("run_attempt"))
    ):
        raise RelayError("public run is outside the approved route")
    if actor_ids is not None and any(
        (run.get(field) or {}).get("id") not in actor_ids
        for field in ("actor", "triggering_actor")
    ):
        raise RelayError("public run actor is outside the approved route")
    for field in ("repository", "head_repository"):
        repository = run.get(field) or {}
        if (
            repository.get("id") != route.repository_id
            or repository.get("full_name") != route.repository
            or repository.get("private") is not False
        ):
            raise RelayError("public source repository identity did not match")
    reporter = api("GET", f"{path}/contents/{SOURCE_PATH}?ref={head}")
    if reporter.get("type") != "file" or reporter.get("sha") != route.workflow_blob:
        raise RelayError("public reporter workflow is outside the approved revision")
    return Attempt(route, run_id, run["run_attempt"], head)


def discover_public(api, routes):
    """Poll only configured validation workflows. Recheck each before dispatch."""
    for route in routes:
        runs = inventory(
            api, f"/repos/{route.repository}/actions/workflows/{route.workflow_id}/runs",
            "workflow_runs", {"event": "push", "status": "in_progress"},
        )
        # GitHub lists newest first. Visit the oldest candidate on this route first.
        for run in sorted(runs, key=lambda row: row["id"]):
            branch = run.get("head_branch") or ""
            if branch == "main" or branch.startswith("ci/"):
                yield observe_public(api, route, run["id"])


@dataclass(frozen=True)
class PrivateWorkflow:
    repository: str
    repository_id: int
    workflow_id: int
    ref: str
    sha: str
    actor_id: int

    def __post_init__(self):
        if (
            self.repository != "newdarwindev/runspindle-strix-exec"
            or not positive(self.repository_id)
            or not positive(self.workflow_id)
            or not positive(self.actor_id)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", self.ref)
            or not SHA.fullmatch(self.sha)
        ):
            raise RelayError("invalid approved private workflow")

    @property
    def path(self):
        return f"/repos/{self.repository}"

    def check_ref(self, api):
        repository = api("GET", self.path)
        if (
            repository.get("id") != self.repository_id
            or repository.get("full_name") != self.repository
            or repository.get("private") is not True
        ):
            raise RelayError("execution repository must be the approved private repository")
        ref = api("GET", f"{self.path}/git/ref/heads/{self.ref}")
        if (ref.get("object") or {}).get("sha") != self.sha:
            raise RelayError("private workflow branch advanced")

    def matches(self, run, attempt):
        return (
            positive(run.get("id"))
            and run.get("workflow_id") == self.workflow_id
            and run.get("path") == PRIVATE_PATH
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == self.ref
            and run.get("head_sha") == self.sha
            and run.get("run_attempt") == 1
            and run.get("display_title") == attempt.key
            and all(
                (run.get(field) or {}).get("id") == self.actor_id
                for field in ("actor", "triggering_actor")
            )
            and all(
                (run.get(field) or {}).get("id") == self.repository_id
                for field in ("repository", "head_repository")
            )
        )


class Relay:
    """Call under the canonical fleet lock. Keep ambiguous writes pending."""

    def __init__(self, state, public_read, private_api, checks_api, private, app_id):
        if not positive(app_id):
            raise RelayError("invalid reporter App ID")
        self.state = state
        self.public_read = public_read
        self.private_api = private_api
        self.checks_api = checks_api
        self.private = private
        self.app_id = app_id

    def dispatch(self, attempt):
        record = self.state.get(attempt.key)
        if record:
            if "private_run_id" in record:
                return record["private_run_id"]
            runs = inventory(
                self.private_api, f"{self.private.path}/actions/workflows/{self.private.workflow_id}/runs",
                "workflow_runs", {"event": "workflow_dispatch", "branch": self.private.ref},
            )
            matches = [run for run in runs if run.get("display_title") == attempt.key]
            if len(matches) != 1 or not self.private.matches(matches[0], attempt):
                raise RelayError("private dispatch is unresolved; reconcile before retrying")
            private_id = matches[0]["id"]
        else:
            self.private.check_ref(self.private_api)
            if observe_public(self.public_read, attempt.route, attempt.run_id) != attempt:
                raise RelayError("public attempt changed before dispatch")
            self.state.set(attempt.key, {"dispatch_requested": True})
            response = self.private_api(
                "POST", f"{self.private.path}/actions/workflows/{self.private.workflow_id}/dispatches",
                200, {"ref": self.private.ref, "inputs": {
                    "request": attempt.key, "repository": attempt.route.repository, "sha": attempt.sha,
                }},
            )
            private_id = response.get("workflow_run_id")
            if not positive(private_id):
                raise RelayError("private dispatch did not return a valid run ID")
        self.state.set(attempt.key, {"dispatch_requested": True, "private_run_id": private_id})
        return private_id

    def private_run(self, attempt):
        record = self.state.get(attempt.key, {})
        private_id = record.get("private_run_id")
        if not positive(private_id):
            raise RelayError("private dispatch is unresolved")
        run = self.private_api("GET", f"{self.private.path}/actions/runs/{private_id}")
        if run.get("id") != private_id or not self.private.matches(run, attempt):
            raise RelayError("private workflow identity changed")
        return run

    def queued_jobs(self, attempt):
        run = self.private_run(attempt)
        if run.get("status") == "completed":
            return []
        self.private.check_ref(self.private_api)
        if observe_public(self.public_read, attempt.route, attempt.run_id) != attempt:
            raise RelayError("public attempt changed before runner admission")
        run_id = run["id"]
        jobs = inventory(
            self.private_api, f"{self.private.path}/actions/runs/{run_id}/attempts/1/jobs", "jobs",
        )
        names = {job.get("name") for job in jobs}
        if len(names) != len(jobs) or not names.issubset(JOBS):
            raise RelayError("unexpected private job inventory")
        for job in jobs:
            if (
                job.get("run_id") != run_id
                or job.get("run_attempt") != 1
                or job.get("head_sha") != self.private.sha
                or job.get("labels") != [f"runspindle-strix-{run_id}-1-{job['name']}"]
            ):
                raise RelayError("unexpected private job identity")
        queued = [job for job in jobs if job.get("status") == "queued"]
        if any(job.get("runner_id") not in (None, 0) or job.get("runner_name") for job in queued):
            raise RelayError("queued private job already has a runner")
        return queued

    def publish(self, attempt, reconciled):
        """Publish after the fleet verifies each recorded assignment and cleanup.

        reconciled maps job IDs to runner IDs from the canonical fleet ledger.
        """
        run = self.private_run(attempt)
        if run.get("status") != "completed":
            raise RelayError("private execution has not finished")
        private_id = run["id"]
        jobs = inventory(
            self.private_api, f"{self.private.path}/actions/runs/{private_id}/attempts/1/jobs", "jobs",
        )
        if any(
            job.get("status") != "completed"
            or job.get("run_id") != private_id
            or job.get("run_attempt") != 1
            or job.get("head_sha") != self.private.sha
            for job in jobs
        ):
            raise RelayError("unexpected private job identity or state")
        assigned = {job["id"]: job["runner_id"] for job in jobs if positive(job.get("runner_id"))}
        if assigned != reconciled or len(set(assigned.values())) != len(assigned):
            raise RelayError("exact private runner assignment and cleanup are unverified")
        success = (
            run.get("conclusion") == "success"
            and len(jobs) == len(JOBS)
            and {job.get("name") for job in jobs} == set(JOBS)
            and len(assigned) == len(JOBS)
            and all(
                job.get("conclusion") == "success"
                and job.get("labels") == [f"runspindle-strix-{private_id}-1-{job['name']}"]
                for job in jobs
            )
        )
        conclusion = "success" if success else "failure"
        return self._publish_check(attempt, private_id, conclusion)

    def _publish_check(self, attempt, private_id, conclusion):
        if observe_public(self.public_read, attempt.route, attempt.run_id) != attempt:
            raise RelayError("public attempt changed before reporting")
        record = self.state.get(attempt.key)
        checks = inventory(
            self.checks_api, f"{attempt.public_path}/commits/{attempt.sha}/check-runs", "check_runs",
            {"app_id": self.app_id, "check_name": CHECK_NAME, "filter": "all"},
        )
        matches = [check for check in checks if check.get("external_id") == attempt.key]
        if not matches:
            if record.get("publication_requested"):
                raise RelayError("check publication is unresolved; reconcile before retrying")
            self.state.set(attempt.key, record | {"publication_requested": True})
            matches = [self.checks_api("POST", f"{attempt.public_path}/check-runs", 201, {
                "name": CHECK_NAME, "head_sha": attempt.sha, "external_id": attempt.key,
                "status": "completed", "conclusion": conclusion,
                "output": {"title": CHECK_NAME, "summary": f"Private Strix run {private_id}."},
            })]
        if len(matches) != 1 or any(
            check.get("head_sha") != attempt.sha
            or check.get("name") != CHECK_NAME
            or check.get("external_id") != attempt.key
            or check.get("status") != "completed"
            or check.get("conclusion") != conclusion
            or (check.get("app") or {}).get("id") != self.app_id
            or not positive(check.get("id"))
            for check in matches
        ):
            raise RelayError("published check identity or conclusion did not match")
        check_id = matches[0]["id"]
        self.state.set(attempt.key, record | {"publication_requested": True, "check_id": check_id})
        return check_id
