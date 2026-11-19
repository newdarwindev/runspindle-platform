"""Configure one repository runner before releasing its listener."""

from datetime import datetime, timedelta, timezone
import re
import shlex
import time

from .github_jobs import PUBLIC_REPOSITORIES, RoutingError, inventory, positive

PRIVATE_REPOSITORIES = {
    "newdarwindev/runspindle-strix-ci-canary",
    "newdarwindev/runspindle-strix-exec",
}
LABEL = re.compile(
    r"runspindle-strix-(?:(?:canary-(?:proof|failure)-[1-9][0-9]*-[1-9][0-9]*)"
    r"|(?:(?:direct-)?[1-9][0-9]*-[1-9][0-9]*-[a-z0-9_-]+))"
)
RUNNER_NAME = re.compile(r"runspindle-fleet-[0-9a-f]{32}")


def _scope(repository, repository_id, name, label):
    if (
        repository not in PUBLIC_REPOSITORIES | PRIVATE_REPOSITORIES
        or not positive(repository_id)
        or not RUNNER_NAME.fullmatch(name)
        or not LABEL.fullmatch(label)
    ):
        raise RoutingError("invalid repository runner scope")
    if (
        (repository in PUBLIC_REPOSITORIES) != label.startswith("runspindle-strix-direct-")
        or (repository == "newdarwindev/runspindle-strix-ci-canary")
        != label.startswith("runspindle-strix-canary-")
    ):
        raise RoutingError("runner label does not match repository scope")
    return f"/repos/{repository}"


def _registration_token(api, path, clock):
    response = api("POST", path + "/actions/runners/registration-token", 201)
    now = clock()
    token = response.get("token", "")
    try:
        expires = datetime.fromisoformat(response.get("expires_at", ""))
    except (TypeError, ValueError):
        raise RoutingError("invalid repository registration expiry") from None
    if (
        not isinstance(token, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token)
        or expires.utcoffset() is None
        or not timedelta(0) < expires - now <= timedelta(hours=1, seconds=2)
    ):
        raise RoutingError("invalid short-lived repository registration credential")
    return token


def configure(api, ssh, repository, repository_id, name, label, *, admit, clock=None):
    """Configure offline. The SSH adapter must send its script only on stdin."""
    path = _scope(repository, repository_id, name, label)
    record = api("GET", path)
    if (
        record.get("id") != repository_id
        or record.get("full_name") != repository
        or record.get("private") is not (repository in PRIVATE_REPOSITORIES)
    ):
        raise RoutingError("registration repository identity changed")
    admit()
    token = _registration_token(api, path, clock or (lambda: datetime.now(timezone.utc)))
    try:
        admit()
        script = f"""set -eu
umask 077
ulimit -c 0
cd /home/runner/runner
for state in .env .runner .credentials .credentials_rsaparams _work; do
    if test -e "$state" || test -L "$state"; then exit 3; fi
done
set -a
. /etc/environment
set +a
export PATH=/opt/runspindle-quality/bin:$PATH
ACTIONS_RUNNER_INPUT_TOKEN={shlex.quote(token)} ./bin/Runner.Listener configure \\
    --unattended --ephemeral --disableupdate --no-default-labels \\
    --url "https://github.com/{repository}" --name "{name}" --labels "{label}" --work _work \\
    >/dev/null 2>&1
python3 - "{name}" "https://github.com/{repository}" <<'PY'
import json
import sys
with open(".runner", encoding="utf-8-sig") as stream:
    config = json.load(stream)
agent = config.get("agentId")
if type(agent) is not int or agent <= 0:
    raise SystemExit(4)
if config.get("ephemeral") is not True or config.get("disableUpdate") is not True:
    raise SystemExit(5)
if config.get("agentName") != sys.argv[1] or config.get("gitHubUrl", "").rstrip("/") != sys.argv[2]:
    raise SystemExit(6)
print(agent)
PY
"""
        outcome = ssh(script)
        if outcome.returncode or not re.fullmatch(r"[1-9][0-9]{0,19}", outcome.stdout.strip()):
            raise RoutingError(f"guest runner configuration failed (exit {outcome.returncode})")
        configured_id = int(outcome.stdout.strip())
    finally:
        token = None
    deadline = time.monotonic() + 20
    while True:
        matches = [
            row for row in inventory(api, path + "/actions/runners", "runners")
            if row.get("name") == name
        ]
        if matches or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if (
        len(matches) != 1
        or matches[0]["id"] != configured_id
        or matches[0].get("status") != "offline"
        or ("ephemeral" in matches[0] and matches[0]["ephemeral"] is not True)
        or matches[0].get("busy") is not False
        or [(item.get("name"), item.get("type")) for item in matches[0].get("labels", [])]
        != [(label, "custom")]
    ):
        diagnostic = [
            {
                "status_known": row.get("status") in ("online", "offline"),
                "offline": row.get("status") == "offline",
                "ephemeral": row.get("ephemeral") is True,
                "ephemeral_reported": "ephemeral" in row,
                "busy_false": row.get("busy") is False,
                "custom_label_count": sum(item.get("type") == "custom" for item in row.get("labels", [])),
                "label_matches": [item.get("name") for item in row.get("labels", [])] == [label],
            }
            for row in matches
        ]
        raise RoutingError(f"offline runner registration does not match its job: {diagnostic}")
    return matches[0]["id"]


def start_listener(ssh, *, admit):
    """Release the listener after the supervisor records the exact runner ID."""
    admit()
    outcome = ssh("""set -eu
umask 077
ulimit -c 0
cd /home/runner/runner
test ! -e .env && test ! -L .env
set -a
. /etc/environment
set +a
export PATH=/opt/runspindle-quality/bin:$PATH
set -C
exec 3> /home/runner/job.log
nohup env -u SSH_AUTH_SOCK ./bin/Runner.Listener run >&3 2>&1 < /dev/null &
""")
    if outcome.returncode:
        raise RoutingError("guest listener startup failed")
