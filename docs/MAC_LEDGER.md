# Mac disposable fleet ledger candidate

Status: synthetic lifecycle tests and read-only local inventory observations.
Guest boot, launchd management, runner registration, GitHub jobs and target
isolation qualification remain untested.

`tools/runner_fleet/mac_lifecycle.py` prepares the one-slot Mac ownership
boundary. It validates the existing repository, workflow, ref, exact labels,
job name and source revision before reservation. The shared SQLite state
allocates a monotonic Mac lease epoch in the same transaction as the resource
reservation. The candidate image digest, lane, backend, source revision and
lease are retained in the private ledger. Full GitHub job payloads are not
copied into the Mac reservation event.

An owner marker and independent Tart/Lima inventory are required for cleanup.
Only the exact fleet-named guest on the recorded backend may be stopped and
deleted. A changed marker, lost stop reply, still-visible guest or untracked
fleet-named guest leaves the slot occupied or blocks a new reservation. The
code does not delete a pre-existing personal Tart/Lima instance. A crash
between the SQLite reservation and marker write intentionally leaves the
slot occupied for manual reconciliation; it is not treated as a clean guest.
The candidate manager now reads authoritative GitHub workflow and job pages
for the eight allowlisted repositories. It admits only the exact manually
dispatched fleet qualification path, trusted source repository/ref, revision,
job name and labels. It watches queued/in-progress runs through completion,
persists queue expiry and terminal job identities, and re-fetches the exact
run/job under the single-process lock before reservation. API errors,
duplicate or contradictory identities, completed jobs and expired queue time
block reservation. A GitHub conclusion is recorded as an observation, never
as fleet qualification or proof of isolation. No GitHub dispatch, runner
registration or cancellation is implemented; the future operational manager
must still reconcile external effects before retrying any uncertain action.

The backend's `create` method always fails closed. Default Tart/Lima networks
have not passed the project's host/LAN/peer egress and live-canary contract,
and there is no qualified ARM64 runner image or real launchd credential
context. The image digest accepted by this preparation API is **candidate
metadata**, not independent qualification or authority to boot. No CLI
command exposes this manager as an installed service or activatable runner.

Synthetic tests cover wrong repository/workflow/labels/image bindings,
single-slot capacity, monotonic leases, boot refusal, owned stop/delete and
independent absence, changed ownership markers, lost stop replies, stale
leases, untracked guests and a same-name guest on the opposite backend.
Mocked API tests cover pagination, conflicting run/job identity, fork and
untrusted event denial, stale/duplicate jobs, API failure, queue timeout,
terminal observation and same-lock reservation re-fetch.
Use a local Python 3.10+ interpreter, for example
`/opt/homebrew/bin/python3.13 -m unittest tests.test_mac_lifecycle -v`, and
run `python3 tools/check_scaffold.py`. These checks do not satisfy any live fleet
scenario. The remaining activation plan and original resource thresholds are
in [RUNNER_FLEET.md](RUNNER_FLEET.md).

## Local command inventory checks

The Mac adapter reads one JSON object per Lima instance and recognizes explicit
Running and Stopped states. A successful command can still report skipped or
broken instances. The adapter blocks on those diagnostics. It accepts the
documented empty result with Lima's single no-instance warning.
[Lima 2.3.0 inventory source](https://github.com/lima-vm/lima/blob/v2.3.0/cmd/limactl/list.go)

Tart inventory is restricted to local guests. Each row must have a unique
name and consistent boolean running state and lifecycle state, including the
suspended state. Unknown, duplicate or contradictory rows block allocation
and cleanup. The SQLite regression reopens the manager after reservation and
verifies that an incomplete observation keeps the slot occupied without
issuing a stop or delete. Deliberately ignoring an empty-output warning makes
that test fail.
[Tart 2.41.1 inventory source](https://github.com/openai/tart/blob/2.41.1/Sources/tart/Commands/List.swift)

Run the bounded synthetic checks from the repository root:

```sh
python3 -B -m unittest tests.test_mac_lifecycle tests.test_mac_policy_boundary tests.test_mac_polling tests.test_fleet_lifecycle
```

All 33 tests passed in both normal and optimized CPython 3.13.18, with
SQLite resource warnings. In macOS arm64 build `26B123`, read-only commands
against disposable `LIMA_HOME` and `TART_HOME` directories verified both empty
inventories, two Lima fixture directories reported as Stopped, and rejection
of a Lima fixture with incomplete configuration. Neither a guest boot nor
mutating backend commands were needed for this observation. Nonempty Tart
rows and running or suspended guests were exercised with synthetic output.

| Command | Version | Executable SHA-256 |
| --- | --- | --- |
| `limactl` | 2.3.0 | `f0b05d09511621106bae7e447ff7aed4705f40f8d71784a844a1258d3c14426c` |
| `tart` | 2.41.1 | `a5e51a515d1b96c14e369e10352532f9aa5230c2233e8f39b611122bf6852276` |

## Offline proposal reconciliation

The Mac source candidate at `62a1aad9ba7e8da573afed8973e5a6edcf848822`
is reconciled with Platform PR #27 at
`5a91f3b4acee7a47e5f73992d13cd7bbb7d160c4`. These are source inputs,
not installed controller or image qualification. The PR's paired demo
policy/configuration proposal remains inactive and its validation does not
grant scheduling or polling authority.

The active configuration still requires exactly the original eight repositories.
Scheduling and Mac admission reject a candidate repository tuple. The Mac
manager checks the active policy before opening its state and before polling
or reserving, and snapshots caller configuration so an offline proposal cannot
mutate its active allowlist. No demo enrollment, dispatch, runner registration,
service installation, or activation entrypoint is added.

Reconciliation tests cover candidate rejection before state or external calls,
unchanged exact ARM64 routing, isolated configuration snapshots, changed manager
policy rejection and the original 10 GiB memory gate. The existing lease,
cleanup and read-only API tests remain synthetic. No private host inventory,
preflight artifacts, credentials or operational endpoints are included.
The Strix dispatcher retains its input-less command contract for repositories
whose workflows do not yet declare a lane input; the new Platform workflow
defaults to the Strix lane. A mocked regression checks this compatibility.

Local Python 3.13 discovery collected 234 tests: 221 passed and 13 were skipped.
The skips remain explicit: five opt-in live Temporal tests and eight disposable
Linux ARM64 installation tests. The focused proposal/Mac/scheduling set passed
47 tests. Scaffold structure, Python syntax and whitespace checks passed.
Existing tests emitted unclosed SQLite connection ResourceWarnings.
The Mac environment lacks the pinned static checker packages; no dependencies
were installed. Pinned static checks run separately against a verified source-only
cloud mirror. Cloud static evidence and Mac unit evidence remain distinct from
Mac Actions, live guest, isolation, installed-byte or upstream integration
qualification. Guest boot stays blocked.
