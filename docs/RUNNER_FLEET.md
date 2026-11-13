# Isolated runner fleet preparation

This is a partial implementation of the approved fleet deployment plan.
No fleet manager, service or runner has been installed. No GitHub job has
qualified a lane. Existing acceptance gates and repository protections are
unchanged. Production deployment remains unimplemented.

## Implemented and tested locally

- `fleet/config.json` defines the eight repository allowlist, three lane labels,
  resource budgets, shared host capacities and queue/job timeout policy.
- `tools/runner_fleet/model.py` selects explicitly labelled queued jobs fairly
  across repositories. Unknown and booting resources consume capacity. Both Mac
  lanes share one slot. This pure scheduler is not yet wired to a manager.
- `tools/fleet.py preflight --host strix|macbook` checks local host identity,
  installed executables, usable KVM, disk and conservative memory headroom, and
  administration access to all eight repositories using the existing `gh` login.
  It must run on the named physical host, under the intended service account.
- `qualify` creates a 24-cell report (eight repositories, three lanes) with all
  nine operational scenarios explicitly blocked until exercised. With receipts,
  it fetches authoritative GitHub workflow and job data, verifies the requested
  SHA, event, workflow path, lane labels, fleet runner identity and workload step,
  and preserves completed job logs privately. A passing workload cell cannot
  certify disposable isolation, recovery, fairness or the overall fleet.
- Reports and logs use atomic replacement and mode 0600. Raw evidence belongs
  outside Git; public code and tests use synthetic identifiers only.

The existing platform CI now runs all implemented Python tests. Its configured
workflow has not been run on a qualified fleet runner.

## Commands

Run from the component checkout. Default state is
`~/.local/state/runspindle-fleet`; use `--state-dir` on the intended volume.
Exit 2 means blocked. Reports always distinguish preparation from deployment.

```sh
python3 tools/fleet.py preflight --host strix
python3 tools/fleet.py status --host strix
python3 tools/fleet.py qualify --host strix --revisions /private/revisions.json
python3 tools/fleet.py qualify --host strix --revisions /private/revisions.json --receipts /private/receipts.json
```

`revisions.json` maps each short repository name to its exact lowercase
40-character commit SHA. Receipt keys are `newdarwindev/REPOSITORY/LANE` and
values are positive numeric workflow run IDs. Collection requires revisions for
all eight repositories. Only jobs named exactly after the configured lane from
`.github/workflows/fleet-qualification.yml` are eligible. That workflow is not
yet installed in any repository; existing unrelated workflows cannot qualify.
Raw logs may contain private build data and must remain private.

`drain` and `resume` currently write **desired state only**; they cannot control
resources without a manager and report observed state as `not_installed`.
`install` always refuses while backend and isolation prerequisites remain
unimplemented/unqualified. `uninstall` reports blocked and touches no external
resources, since this tool has installed none. Logs are retained.

## Private readiness evidence

Run the probes on both hosts and retain the JSON reports outside Git. Do not
publish operational inventory or raw job logs in public commits. Interactive
GitHub access is not evidence of credential access from systemd/launchd. A
pre-existing pilot image is not a qualified fleet image.

## Work still required before deployment

1. Implement QEMU/KVM, Lima and Tart lifecycle drivers, durable reservations,
   registration/removal, diagnostics, cancellation and restart reconciliation.
2. Prepare architecture-specific, digest-pinned images, runner archives and
   toolchains, including Rust offline dependencies; record image/tool licensing.
3. Enforce host-side network allowlists and deny host/LAN canaries. Disable
   personal mounts, host sockets, forwarded credentials and management access.
   A default Lima/Tart/QEMU configuration is not isolation evidence.
4. Implement systemd/launchd managers and check credential access in their real
   background context. Startup remains disabled until complete qualification.
5. Add portable validation in isolated branches for all eight repositories,
   native Mac host validation and release candidate gates. Preserve all blocked
   acceptance conclusions, trusted triggers and existing unrelated protections.
6. Implement dispatch, terminal-state following, queue timeout, workload/image
   identity attestation and all nine live scenario exercises. The collector is
   not yet a dispatch or fault-injection harness. Configured 900/1800-second
   timeout values are policy, not implemented live timeout enforcement.
7. Qualify Strix first, then Mac Linux, then Mac native. Record 16 Linux and eight
   native Mac executions, exact SHAs, job URLs, runner identities, resource use,
   freshness/canaries, fairness and recovery. A named successful step alone does
   not prove its contents; qualify the pinned workflow/image implementation too.
8. Only after success, enable startup and the agreed passing Strix requirements,
   merge normally and update the composition submodule pin and lockfile together.

The partial implementation deliberately does not expose an option to assert that
missing isolation/background/image checks passed. No account credentials enter
guests; no guests are created by these preparation commands.

## Validation

```sh
python3 tools/check_scaffold.py
python3 tools/check_shared_fixtures.py
python3 -m unittest discover -s tests -v
```

These are scaffold, fixture and synthetic unit tests. They do not establish
integration, live runner qualification or product acceptance.
