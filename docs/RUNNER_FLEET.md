# Disposable runner fleet

Strix has a QEMU/KVM execution backend and a transactional manager. Live push,
cleanup, recovery and unattended qualification must pass before startup is
enabled or branch protections require `validation / strix-linux-x64`. Source
implementation and synthetic tests alone do not establish deployment evidence.
Both Mac lanes remain pending with their original memory reserves and gates.

The Mac ARM64 qualification workflow now names the two labels already present
in `fleet/config.json`. Manual dispatch chooses exactly one lane, defaulting
to Strix for the existing qualification collector. The partitioned validation
workflow retains its existing Strix route; Mac validation remains pending.
Admission requires the complete configured label set, including OS and
architecture. The [Mac ownership ledger](MAC_LEDGER.md) reserves a single
slot and reconciles owned guest identity, but boot is blocked. This workflow
definition and ledger do not provide a qualified Mac guest image, operational
manager or successful Actions execution. Do not dispatch them until the
Mac prerequisites below are qualified; no runner is presently registered by
the Mac preparation commands.

For the one shared Mac slot, the existing preflight requires 6 GiB host reserve
plus the larger 4 GiB Tart lane, or 10 GiB conservatively available. The 3 GiB
Lima lane would separately require 9 GiB. Preflight counts only free and
inactive pages on macOS; a memory-pressure percentage does not replace that
gate. Preserve the reserve and run one guest at a time. If the threshold is
unmet, close optional foreground workloads and remeasure or use a machine
with enough RAM. Do not reclaim memory by deleting user data or changing
global services. Keep raw host observations in private state, outside Git.

Mac activation additionally requires a digest-pinned ARM64 Linux image and
runner/toolchain closure, a separately pinned macOS Tart image and toolchain,
recorded notices, host-enforced guest egress denial with live host/LAN
canaries, a single-slot durable manager with restart reconciliation and
repository-scoped ephemeral registration, and real launchd-context credential
checks. The existing stopped Tart pilot and an interactive `gh` session do
not satisfy these gates. A Linux ARM64 DSPy test must use its locked Linux
Python/wheel closure inside a disposable Linux guest; macOS host packages
cannot be substituted. After code and independent negative tests, run the
24-cell qualification and operational fault matrix before any owner-operated
activation or requirement change.

## Execution boundary

Two fixed slots each reserve 4 vCPU and 6 GiB. Starting, cleaning and uncertain
resources occupy capacity. SQLite WAL transactions persist reservations before
external effects; an exclusive process lock prevents competing managers.
Dedicated unprivileged VM accounts run QEMU through bounded transient systemd
units. Guests receive an ephemeral SSH public key and a repository registration
token over SSH stdin. Account credentials remain in host-side `gh`.

`host_helper.py` is installed root-owned under `/opt/runspindle-fleet`. Its
accepted operations and slot paths are fixed. Fleet TAPs never bridge existing
networks. The fleet-owned nftables table is installed atomically before boot;
it denies host, LAN, peer and Internet traffic except each slot's own HTTPS
CONNECT proxy and replies to host administration. IPv6 is denied. No existing
firewall tables are flushed. `fleet/egress.json` records domain permissions.
The proxy resolves every connection, rejects any private/reserved/multicast
answer and dials a validated numeric address without another DNS lookup.

Live synthetic host and LAN canaries must first be reachable from the host,
then unreachable from the guest before runner registration. Pre-registration
probes also verify absent personal mounts, management keys, Docker sockets and
previous workspaces. Failures retain diagnostics and occupy capacity until
cleanup is established. No guest is a personal/persistent runner.

## Image preparation

`fleet/image-inputs.json` pins Ubuntu, runner, Rust and Node checksums and
licenses. A changed download fails verification. The guest package/license
manifest records package versions and copyright hashes. The image recipe pins
the current core and host validation commits, fetches their committed
`Cargo.lock` closures (including core's pinned host Git dependency), and runs
their actual test and Clippy commands with Cargo offline. The manifest records
checkout and lock hashes, registry archive checksums and crate license fields.
The build writes `candidate.qcow2` and `candidate.json` to the private state
directory. Promote those bytes to `qualified.qcow2` only after fresh-guest
workload, integrity, isolation and cleanup checks pass. No image binaries
belong in Git.

On Strix, after reviewing source and prerequisites:

```sh
sudo python3 tools/runner_fleet/bootstrap_host.py
python3 tools/runner_fleet/build_image.py
python3 tools/fleet.py preflight --host strix
python3 tools/fleet.py install --host strix
python3 tools/fleet.py status --host strix
python3 tools/fleet.py resume --host strix
```

The bootstrap installs only fleet accounts, root-owned code, TAPs, rules,
proxies and canaries. Image preparation shares the manager's capacity ledger.
The base image is sealed root-owned/read-only, and its digest is checked before
preflight and resume. Installation runs a fresh disposable guest probe, checks
resources and all repository permissions, and probes existing `gh` credentials
in the actual systemd User/DBus context. Missing background credentials block
installation; credentials are never replaced. The manager starts drained and
startup remains disabled. `resume` requires a recent successful preflight.

## Operation and reconciliation

[Build retention and stopped-slot recovery](RUNNER_BUILD_RECOVERY.md) describes
source fixes, preserved original/capture locations, and the separate approval
needed to reconcile an already uncertain build reservation.

The manager polls every 30 seconds, selecting explicitly labelled trusted
`main`/`ci/*` push/manual jobs round-robin across the eight repositories. Labels
cannot bind a runner to one queued job: the actual claimed job, SHA and runner
ID are persisted independently from the triggering queue reservation. Runners
register repository-scoped, ephemeral, with updates disabled against the pinned
image. Every runner executes at most one job.

Job claims must match the resource's repository, runner name and retained numeric
registration ID. A same-name claim from another repository is unrelated. A
contradictory or malformed runner ID keeps the slot uncertain. Before handling
completion after restart, the manager checks the retained claim against the same
binding before collecting logs or cleaning up the finished job.

Boot is bounded to 240 seconds, registration to 90 seconds, waiting for a claim
to 180 seconds, job execution to 900 seconds and guest lifetime to 1500 seconds.
Cleanup operations have bounded subprocess deadlines. Timeouts record failed
or blocked reasons. Diagnostics, GitHub logs and the ownership ledger remain
outside guests under the private state directory. Cleanup must confirm stopped
VMs, absent disks/seeds and absent runner registrations before releasing a slot.
A lost reply preserves uncertain ownership. On restart, interrupted boot or
cleanup is reconciled before replacements; live running guests can continue.
Untracked fleet services are stopped and their disks archived before allocation.

```sh
python3 tools/fleet.py drain --host strix
python3 tools/fleet.py status --host strix
python3 tools/fleet.py resume --host strix
python3 tools/fleet.py qualify --host strix --ref ci/strix-push-validation --revisions /private/revisions.json
python3 tools/fleet.py uninstall --host strix
```

Drain blocks allocations while current jobs finish. Status observes the service,
heartbeat, guests, durable resources, queue, errors and reconciliation timestamp.
Uninstall stops fleet work and removes the manager service, preserving logs,
images and protective network rules. Qualification dispatches real workloads,
follows terminal states and retains logs. Its queue limit is 1800 seconds;
expired or ambiguous execution remains blocked. Mac coverage and the operational
scenario matrix are never inferred from green workload jobs. Existing receipt
collection remains available through `--receipts` with all exact revisions.

## Workloads and rollout

`tools/validate.py` is the shared push/qualification entrypoint. It executes
structural, artifact, reference, mutation, fixture, Python, Rust and JavaScript
checks where present. Rust tests and Clippy run offline. It prints the checkout
SHA, image/tool versions, command exit codes, elapsed times, artifact checksums
and remaining implementation gaps. Unimplemented builds are reported explicitly.
The fleet image includes the prepared x64 Temporal dependency and a guest-local Docker daemon, so its real integration suite runs in canonical fleet validation. Canonical validation now explicitly requires that prepared path and fails when its five required Temporal tests do not execute successfully. For local Temporal boundary-only evidence use `python3 tools/python_tests.py --temporal boundary`; this ignores ambient Temporal opt-in and reports integration not run, and cannot qualify the canonical aggregate. Other installed tests retain their platform gates. No host Docker socket is mounted.
Acceptance workflows keep their failing gates separately.

First qualify a platform `ci/*` push at its exact SHA, then all eight repositories.
Prove deliberate failure/fix, two-slot capacity and fairness, guest freshness,
registration cleanup, cancellation, guest loss, manager restart during work and
cleanup, drain/resume and incompatible architecture exclusion. Retain private
SHAs, job URLs, runner IDs, conclusions, logs, image IDs and peak usage. Merge
through existing rules, verify all eight actual `main` pushes, and update the
composition platform pin plus lockfile together after the platform merge. Only
complete observed Strix qualification authorizes startup and required checks.

## Local checks

`python3 tools/fleet.py image-status --host strix --image /private/candidate.qcow2
--image-manifest /private/candidate.json` reads candidate bytes and the manifest
without creating state, booting a guest or contacting GitHub. It reports every
configured lane, the actual image digest and the recipe digest. A matching Strix
image/recipe passes identity only; installed tooling, lifecycle faults and Mac
lanes remain unqualified. The command exits 2 even when byte identity matches.

Status now reports separately observed guest ownership and durable reservations.
An unavailable/stale/future heartbeat or inventory poll, missing guest observation,
guest loss, ownership mismatch or inventory error advertises zero available slots.
Draining also advertises zero availability. Uncertain/cleaning reservations remain
occupied after a fresh process opens the SQLite store. This readout does not change
the scheduler or reconcile resources. Reservations retain the requested commit/run
separately from any actual claimed job; old reservations lacking that metadata
remain explicitly absent rather than acquiring a fabricated source identity.

The bounded synthetic fault suite and deliberate source mutations run through
`python3 tools/validate.py`; `tools/check_fleet_lifecycle_mutations.py` independently
removes freshness, recipe and uncertain-capacity checks and requires assertion
failures. Real QEMU/systemd/GitHub operations are mocked in the manager unit tests;
the fresh-process SQLite tests exercise an actual local database. These checks
cannot qualify actual guest loss, cancellation, cleanup, isolation or fairness on
any target. Actual Strix and Mac observations remain required for issue #15.

```sh
python3 tools/validate.py
```

These local/synthetic checks do not certify live qualification or product
acceptance. Both Mac lanes and product release gates remain separate.

## Source-only isolated campaign guard

The [isolated campaign guard](ISOLATED_CAMPAIGN.md) implements durable capped
admission, retention accounting and exact observation checks in a separate
offline ledger. Its live entry point remains disabled: server assignment
enforcement and complete external-writer fencing are unavailable. Synthetic
checks do not qualify GitHub assignment or enable campaign execution. Ordinary
repository-queue behavior, workflows and operational state are unchanged.
