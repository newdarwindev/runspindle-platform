# Bounded document-only candidate

Status: source-only implementation and synthetic tests. No candidate image has
been built with this controller. No wheels have been downloaded or installed by
this change. CPython 3.12.3 renderer-byte reproduction, the host's CPython 3.14
compatibility, native cleanup and actual runtime bounds remain unqualified.

This implements the scoped-candidate and uncertain-effect requirements in
[desired intent](../specs/desired-intent.md). It is deliberately separate from
the full image builder and [ordinary queue campaign](RUNNER_FLEET.md). It does
not replace their evidence, change their pins, or make a failed GitHub check
green. Passing its unregistered guest is candidate evidence only. Exact-head CI,
image selection and merging require later, separate authorization.

## Narrow operation

1. Observe exact clean controller source, the existing qualified image and its
   unchanged manifest, the complete protected image/candidate inventory, stopped
   manager, drained idle state, idle slots, current isolation rules/TAPs/services,
   installed helper, KVM access, memory/disk headroom and all staged inputs.
2. Durably reserve one preparation guest. Boot a disposable overlay of the exact
   existing qualified image. Never overwrite that base. Require CPython 3.12.3,
   venv and bundled pip already present, and an absent document environment.
   Install only the three pinned document wheels, offline, with hash checking.
   Compare installed files and notices to their verified wheel bytes.
3. Acknowledge completed preparation, clean guest identity/SSH/cloud-init state,
   observe shutdown, and seal the result in a new operation-specific directory.
   Verify owned guest cleanup before reserving a second guest.
4. Boot one fresh, unregistered validation guest using the explicit overlay
   manifest. Reconstruct root, seven component and two H10 checkouts from
   pre-staged, hash/size/commit-pinned Git bundles. Git accepts only file
   transport. Check every component against the root commit's gitlink, run H10,
   verify the document interpreter, and run full root implemented checks normally
   and with `-O`. These include required renderer regeneration/byte tests; no skip
   or fallback is added. Missing/incomplete bundles fail instead of fetching.
5. Verify the second guest's owned cleanup, recheck protected bytes and existing
   network/manager state, and record the result. Leave the manager stopped and
   drained. Do not promote, restore, register runners, admit queue jobs, dispatch
   workflows, download inputs or repair missing prerequisites.

The new `document-overlay-v1` image manifest binds the unchanged base digest and
manifest digest, document closure, exact controller, operation, guest document
receipt and validation sources. It is not a full-recipe rebuilt image. Ordinary
backend/candidate binding and the existing service preflight/resume admission
reject its type, even if someone copies its manifest into an ordinary candidate
or qualified-manifest location. There is no automatic promotion command in this
repository. Only the narrow validation backend opts into the distinct type.

The copied preparation module/script hashes are recorded separately and checked
inside preparation before the verifier is imported or wheels are installed.

## Plan contract and read-only entry point

`python3 tools/document_candidate.py --plan /absolute/operator-plan.json`

This command parses the plan and prints its bounded intent. It does not construct
State, create files/directories/bytecode, probe services, or touch fleet state.
It is not a successful preflight. Execution additionally requires `--execute`
and separate approval of the exact plan and its effects. No executable example
plan with real host identities is committed.

The strict schema has these fields:

- `schema_version`: integer 1; `operation_id`: a fresh 32-hex identifier.
- `execution_source_sha` and `binding`: exact clean commit and the three hashes
  from `runner_fleet.identity.binding`. The controller hash covers the new
  entry point, supervisor and overlay recipe. The existing full-recipe hash is
  retained as a source binding only, never as a claim that this overlay rebuilt it.
- `state_directory`, `downloads`, `source_bundles`: explicit canonical absolute
  existing paths. The controller does not acquire or stage missing inputs.
- `base_image`, `base_manifest`: the existing `qualified.qcow2`/`qualified.json`;
  `base_sha256`, `base_manifest_sha256`: exact independently reviewed hashes.
- `document_closure_sha256`, `helper_sha256`, `host_python`: exact approved inputs.
  The host version must be explicitly selected from 3.12.14 or 3.14.8; this allowlist
  is not evidence that both were tested. Verification observes the actual version.
- `network_rules_sha256`: hash of a fresh successful
  `sudo -n nft --stateless list table inet runspindle_fleet` observation. Execution
  repeats that read, checks active proxy/canary units and dedicated TAP addresses,
  and fails closed on denial, missing data or drift. It never calls helper(network).
- `minimum_free_bytes`: at least 56 GiB on image, slot and state filesystems.
  The standalone base must have virtual size at most 24 GiB. This conservative
  admission threshold allows two 24 GiB guest/image objects plus headroom; it is
  not a host disk quota or a guarantee against unrelated concurrent disk usage.
  Existing 18 GiB memory availability and installed guest limits remain unchanged.
- `protected`: the complete absolute-path/hash inventory of existing image files
  plus shared `candidate.json`, if present. Symlinks, omissions and added files
  fail verification. Only the newly owned output is excluded from the final scan.
- `sources`: exactly root, core, host, browser, tether, refinery, console, platform,
  feedback-tether and feedback-refinery. Each contains commit, sha256 and bytes;
  files are `<name>.bundle` in source_bundles, each at most 1 GiB. They must already
  contain the required objects and history. Bundle creation is outside this tool.
  The Platform component here deliberately matches the selected root commit's
  existing gitlink, which can differ from the separately pinned execution
  controller. Both identities are recorded. Root validation does not claim to
  qualify the new controller's entire Platform tree or change a component pin.

Initial execution verification is a separately supervised read-only worker with
only a temporary private log. It precedes State construction and fleet writes.
SQLite queries use a private, promptly deleted DB-plus-WAL working copy, bounded
to 64 MiB, and compare the original bytes and presence before/after. SQLite never
opens the originals, and fresh WAL frames are not ignored. Its connection is
explicitly closed. Source status uses Git's no-optional-locks mode.
After it succeeds, the parent holds the existing fleet lock and rechecks the
manager/drained state. A later failed prerequisite never triggers a repair action.

## Supervision and uncertain cleanup

The foreground supervisor enforces an overall 4,200-second ceiling. Phase caps
are 300 seconds initial verification, 1,500 preparation, 600 sealing, 300 first
cleanup, 900 fresh validation including boot/transfers, 300 second cleanup and
300 final verification. They are ceilings, not completion estimates. The existing
guest 1,500-second systemd limit is unchanged and can terminate a guest earlier.

Each external phase is a separate process group. The parent waits independently
and budgets termination/reaping inside that phase's cap. An overall timer bounds
supervisor-side I/O too; nested timers cannot extend it. New work preserves 300
seconds for current owned cleanup and 300 for final observations. No deadline is
extended and no stage is retried. Cleanup ignores repeated soft cancellation but
retains its hard deadline.

The parent caps log pipes at 64 MiB without applying a file-size limit to image
conversion. A process-local Linux child subreaper and waitid observations detect
orphaned descendants that leave the worker's process group. Pre-existing children,
unavailable observations or a surviving detached descendant refuse success and
retain uncertainty; the controller never broadly kills unknown process groups.
systemd-created guests remain the explicit, separately observed ownership boundary.

Durable reservation precedes external mutation, with operation/stage/source
identity and `cleanup_verified=false`. Cleanup first establishes exact helper
ownership, stops that unit, positively observes termination and absence of
dedicated-account processes, then removes only expected transient slot files and
the operation's private SSH key. Unknown files, foreign/stale ownership, denied
observations, interrupted cleanup or an unreaped phase group retain occupied,
uncertain capacity and prevent a second guest. A partial create before helper
ownership exists intentionally requires later reconciliation rather than guessing
which files are safe to delete.

SIGKILL, host loss, uninterruptible kernel work and privileged descendants that
cannot be signaled can prevent automatic cleanup. They are not reported as clean
or successful. No broad reconcile, teardown, process kill, runner deletion or
service restoration is attempted. A partially converted candidate is retained in
its exclusively created operation directory as failed evidence, never retried or
promoted. Existing images and shared candidate metadata are never overwritten.

## Runtime approval boundary

Source preparation and synthetic tests authorize none of the following effects:
reading protected host prerequisites, staging the selected public wheels/bundles,
creating ephemeral guest credentials, booting the two guests, running their
approved source/tests, installing those wheels inside preparation, stopping owned
units, or sealing a new root-owned image. Approve a concrete plan only after fresh
read-only observations establish that its preconditions can succeed. A denied
probe is a blocker, not permission to substitute a boolean or stale receipt.

This narrow controller does not execute the custom Core builder archive or rebuild
Rust/Node/Temporal. Those capabilities, if used by approved root tests, are inherited
from the exactly pinned existing base, with no new qualification claim. Existing
gates for other repositories and native lanes remain separate.
