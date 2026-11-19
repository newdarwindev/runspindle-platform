"""Configure one repository runner before releasing its listener."""

from datetime import datetime, timedelta, timezone
import re
import shlex

from .private_relay import PUBLIC_REPOSITORIES, RelayError, inventory, positive

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
        raise RelayError("invalid repository runner scope")
    if (
        (repository in PUBLIC_REPOSITORIES) != label.startswith("runspindle-strix-direct-")
        or (repository == "newdarwindev/runspindle-strix-ci-canary")
        != label.startswith("runspindle-strix-canary-")
    ):
        raise RelayError("runner label does not match repository scope")
    return f"/repos/{repository}"


def _registration_token(api, path, now):
    response = api("POST", path + "/actions/runners/registration-token", 201)
    token = response.get("token", "")
    try:
        expires = datetime.fromisoformat(response.get("expires_at", ""))
    except (TypeError, ValueError):
        raise RelayError("invalid repository registration expiry") from None
    if (
        not isinstance(token, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token)
        or expires.utcoffset() is None
        or not timedelta(0) < expires - now <= timedelta(hours=1)
    ):
        raise RelayError("invalid short-lived repository registration credential")
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
        raise RelayError("registration repository identity changed")
    admit()
    now = clock() if clock else datetime.now(timezone.utc)
    token = _registration_token(api, path, now)
    try:
        admit()
        script = f"""set -eu
umask 077
ulimit -c 0
cd /home/runner/runner
for state in .env .runner .credentials .credentials_rsaparams _work; do
    test ! -e "$state" && test ! -L "$state"
done
set -a
. /etc/environment
set +a
ACTIONS_RUNNER_INPUT_TOKEN={shlex.quote(token)} ./bin/Runner.Listener configure \\
    --unattended --ephemeral --disableupdate --no-default-labels \\
    --url "https://github.com/{repository}" --name "{name}" --labels "{label}" --work _work \\
    >/dev/null 2>&1
"""
        outcome = ssh(script)
        if outcome.returncode:
            raise RelayError("guest runner configuration failed")
    finally:
        token = None
    matches = [
        row for row in inventory(api, path + "/actions/runners", "runners")
        if row.get("name") == name
    ]
    if (
        len(matches) != 1
        or matches[0].get("status") != "offline"
        or matches[0].get("ephemeral") is not True
        or matches[0].get("busy") is not False
        or [(item.get("name"), item.get("type")) for item in matches[0].get("labels", [])]
        != [(label, "custom")]
    ):
        raise RelayError("offline runner registration does not match its job")
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
set -C
exec 3> /home/runner/job.log
nohup env -u SSH_AUTH_SOCK ./bin/Runner.Listener run >&3 2>&1 < /dev/null &
""")
    if outcome.returncode:
        raise RelayError("guest listener startup failed")
