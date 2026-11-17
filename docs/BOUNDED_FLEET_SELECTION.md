# Bounded fleet selection: observation, not execution

`python3 -m tools.runner_fleet.bounded_selection` accepts an exact repository,
current-main source SHA, workflow path and queued job name. It resolves a single
existing run attempt/job, verifies workflow bytes against their Git blob, observes
existing host network configuration, then rechecks selection freshness. Ambiguous,
stale, assigned, untrusted-source, truncated and unexpected configuration inputs
fail closed. The command never registers runners, dispatches jobs, invokes network
setup, changes services or promotes images. `--execute` fails before any reads.

Each external read has a 15-second deadline, independent GNU timeout supervisor
with five-second kill grace, and 2 MiB combined output bound. The inspection has
an 18-read and cooperative 120-second aggregate budget. GitHub reads explicitly
use github.com and GET. Credentials and arbitrary stderr are not printed.

Existing nftables rules, TAP ownership/addressing and four service states must
match the expected profile. Mutable nftables handles and packet counters are
ignored, not enforcement expressions. Passing proves configuration observation
only: it does not prove service executable bytes, traffic isolation or absence
of changes immediately afterward. No receipt is an authorization or capability.

## Qualification gaps

A selected job is not server-enforced assignment. Personal-repository runner
labels and ephemeral registration cannot guarantee an exact run will be assigned.
This module therefore always emits `execution_allowed: false`. The proposed
one-attempt limit is descriptive, not an implemented live executor.

A future live adapter needs independently bounded guest lifetime and all output
writers, retained evidence, verified runtime bytes, fresh traffic isolation tests,
and an enforceable admission mechanism. It must not call the existing mutating
network setup merely because a read-only snapshot matched. Security changes need
their own action-time authorization. No broad queue activation is permitted.

GitHub's job-start hook is a possible **pre-step** guard, not server-side assignment:
the pinned runner's JobExtension prepares/downloads actions before adding this
hook, then adds container setup and job steps. Its complete failure semantics,
workflow conditions and trusted identity inputs still need verification. A wrong
assignment may consume an ephemeral runner and fail a different queued job. Do not
present a hook as an already verified isolation boundary.

References:
- [Runner groups and access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
- [Job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts)
- [Pinned runner initialization](https://github.com/actions/runner/blob/v2.338.2/src/Runner.Worker/JobExtension.cs)
- [Offline image probe](OFFLINE_IMAGE_PROBE.md)

Tests use synthetic API, nftables and TAP fixtures plus bounded subprocess checks.
They do not dispatch CI or establish production qualification.
