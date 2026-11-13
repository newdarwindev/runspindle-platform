# Disposable Temporal development lane

The original Python lane runs the unmodified official Temporal CLI **1.10.0**
(embedded server **1.32.2**) in a scratch container. This qualifies only the
bounded Linux arm64 development behavior recorded in
[the evidence](temporal-lane-evidence.json). The full platform issue remains
open for other dependencies, telemetry privacy/readback, packaging and runner
qualification. Production and live acceptance remain not run.

## Preparation and execution

Prerequisites: Python 3.9+, curl, and an available Linux Docker engine supporting
internal networks, volumes, read-only roots, resource limits and arm64 execution.
No alternate service or floating version is selected when these are unavailable.
The amd64 official archive is pinned but has not been exercised here.

Preparation explicitly allows dependency acquisition. It verifies the official
archive and MIT license bytes before building a scratch image with networking
turned off. No upstream base image is downloaded. Immutable source revisions,
archive hashes, server version and licenses are in
[dependencies.json](../temporal/dependencies.json); extracted executable hash and
local immutable image ID are recorded in ignored `prepared.json`.

```sh
python3 tools/temporal_lane.py prepare --architecture arm64 --directory local/temporal-prepared
```

After preparation, the commands below run without downloads. Missing images or
changed prepared bytes/manifests block. Before any CLI image execution, the lane
rechecks archive/license pins, compares executable bytes to the verified archive,
and rebuilds the fixed scratch recipe offline to require the prepared image ID
to match. Mutable metadata cannot substitute an unrelated local image; Docker runs always use `--pull never`.

```sh
RUNSPINDLE_TEMPORAL_PREPARED="$PWD/local/temporal-prepared" python3 -m unittest discover -s tests -v
python3 tools/temporal_lane.py smoke --directory local/temporal-prepared
python3 tools/check_temporal_mutations.py
python3 tools/check_scaffold.py
```

Without `RUNSPINDLE_TEMPORAL_PREPARED`, the Python suite runs fast boundary and
fixture tests and explicitly skips five real-lane cases. Skips are not integration
evidence. The operational `tools/acceptance_gate.py` still fails closed.

## Runtime and component API

Import `tools/temporal_lane.py` and use `with Lane(prepared_directory) as lane:`.
`lane.address` is `temporal:7233` only inside `lane.network`. The network is
internal-only with isolated bridge gateway mode (no host gateway address), is attached to no other network, and publishes no host ports.
The server has a read-only root, dropped capabilities, no new privileges, a
1 GiB memory bound, two CPU limit, PID bound and temporary `/tmp`. The only
persistent run data is a unique disposable Docker volume mounted at `/data`.
The UI is disabled. No native host, account, provider or sibling runtime is used.

`lane.restart(abrupt=True)` kills and recreates the server against that exact
volume. `lane.cli(...)` executes the pinned CLI internally. Component clients
must be separately prepared local image IDs (`sha256:...`):

- `spawn_client(image_id, args, mounts=())` starts a tracked background client.
- `run_client(image_id, args, mounts=(), deadline=60)` waits with a deadline and
  removes the client even after timeout or failure.
- `exec_client(name, args, deadline=60)` acts only on an owned tracked client.

A mount entry is `{'source': '/absolute/synthetic/path', 'target': '/effects',
'read_only': False}`; read-only is the default. Only synthetic test data belongs
in these mounts. A caller using raw Docker calls must add its unique container
name to `lane.clients` **before** creation so lost replies remain cleanable.

Readiness has a configurable deadline up to 180 seconds and a real gRPC health
check. Every Docker call is bounded. Context cleanup removes tracked clients,
server, volume and network on success, exceptions, readiness failure, SIGINT
or SIGTERM; cleanup errors are failures. The real tests independently inspect
absence after normal completion, failure and fresh-process SIGTERM. Forced
SIGKILL of the controller, Docker daemon loss or host power loss cannot execute
Python cleanup: those events are unqualified and may require manual reconciliation
of `runspindle.synthetic=true` resources. No standing cleanup agent is installed.

The smoke test creates an unpolled synthetic workflow, independently reads its
history, kills the real server, restarts against the same SQLite volume, and
asserts identical history. It does not claim worker replay, completed activities,
application recovery, browser fencing or production-store durability. Core owns
those separate workflow tests. No service survives the verified bounded runs;
the prepared image/download cache remains local for offline reuse.
