"""Read and validate repository-scoped GitHub workflow jobs."""

from dataclasses import dataclass
import re
from urllib.parse import urlencode

PUBLIC_REPOSITORIES = frozenset(
    f"newdarwindev/runspindle{suffix}" for suffix in
    ("", "-platform", "-core", "-host", "-browser", "-tether", "-refinery", "-console", "-lab")
)
LAB = "newdarwindev/runspindle-lab"
NEXT = "newdarwindev/runspindle-next-experiment"
DIRECT_REPOSITORIES = PUBLIC_REPOSITORIES | {NEXT}
PARTITIONS = ("quality", "component", "rust", "mutation_1", "mutation_2", "mutation_3", "mutation_4")
SOURCE_PATH = ".github/workflows/validation.yml"
SHA = re.compile(r"[0-9a-f]{40}")


class RoutingError(ValueError):
    """A rejected or uncertain GitHub job observation."""


def positive(value):
    return type(value) is int and value > 0


def inventory(api, path, field, filters=None):
    """Read a complete bounded inventory or leave its result unknown."""
    rows = []
    for page in range(1, 11):
        query = urlencode((filters or {}) | {"per_page": 100, "page": page})
        value = api("GET", f"{path}?{query}")
        batch = value.get(field)
        count = value.get("total_count")
        if type(batch) is not list or type(count) is not int or not 0 <= count <= 1000:
            raise RoutingError("GitHub inventory is unavailable or oversized")
        rows.extend(batch)
        if (
            any(not positive(row.get("id")) for row in rows)
            or len({row["id"] for row in rows}) != len(rows)
            or len(rows) > count
        ):
            raise RoutingError("GitHub inventory changed while paging")
        if len(rows) == count:
            return rows
        if len(batch) != 100:
            break
    raise RoutingError("GitHub inventory pagination was incomplete")


@dataclass(frozen=True)
class Route:
    repository: str
    repository_id: int
    workflow_id: int
    workflow_blob: str
    additional_workflow_blobs: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            self.repository not in DIRECT_REPOSITORIES
            or not positive(self.repository_id)
            or not positive(self.workflow_id)
            or not SHA.fullmatch(self.workflow_blob)
            or not isinstance(self.additional_workflow_blobs, (tuple, list))
            or any(
                not isinstance(blob, str) or not SHA.fullmatch(blob)
                for blob in self.additional_workflow_blobs
            )
        ):
            raise RoutingError("invalid approved direct route")
        object.__setattr__(self, "additional_workflow_blobs", tuple(self.additional_workflow_blobs))

    @property
    def workflow_path(self):
        return ".github/workflows/local-ci.yml" if self.repository == LAB else SOURCE_PATH

    @property
    def private(self):
        return self.repository == NEXT


@dataclass(frozen=True)
class Attempt:
    route: Route
    run_id: int
    attempt: int
    sha: str
    workflow_blob: str

    @property
    def key(self):
        return f"strix-v1:{self.route.repository_id}:{self.run_id}:{self.attempt}:{self.sha}"

    @property
    def repository_path(self):
        return f"/repos/{self.route.repository}"


def observe_push(api, route, run_id, *, statuses=("in_progress",), actor_ids=None):
    """Read the push event and pin the workflow bytes at its exact commit."""
    if not positive(run_id):
        raise RoutingError("invalid workflow run ID")
    path = f"/repos/{route.repository}"
    run = api("GET", f"{path}/actions/runs/{run_id}")
    head = run.get("head_sha", "")
    branch = run.get("head_branch", "")
    if (
        run.get("id") != run_id
        or run.get("workflow_id") != route.workflow_id
        or run.get("path") != route.workflow_path
        or run.get("event") != "push"
        or run.get("status") not in statuses
        or not (branch == "main" or (isinstance(branch, str) and re.fullmatch(r"ci/.+", branch)))
        or not isinstance(head, str)
        or not SHA.fullmatch(head)
        or not positive(run.get("run_attempt"))
    ):
        raise RoutingError("workflow run is outside the approved route")
    if actor_ids is not None and any(
        (run.get(field) or {}).get("id") not in actor_ids
        for field in ("actor", "triggering_actor")
    ):
        raise RoutingError("workflow run actor is outside the approved route")
    for field in ("repository", "head_repository"):
        repository = run.get(field) or {}
        if (
            repository.get("id") != route.repository_id
            or repository.get("full_name") != route.repository
            or repository.get("private") is not route.private
        ):
            raise RoutingError("source repository identity did not match")
    workflow = api("GET", f"{path}/contents/{route.workflow_path}?ref={head}")
    workflow_blob = workflow.get("sha")
    if workflow.get("type") != "file" or workflow_blob not in (
        route.workflow_blob, *route.additional_workflow_blobs
    ):
        raise RoutingError("workflow is outside the approved revision")
    return Attempt(route, run_id, run["run_attempt"], head, workflow_blob)
