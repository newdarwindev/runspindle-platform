# Qualified automatic fleet: staged implementation

Status: source-only. No installed helper, manager, network, image, registration or
service has changed. The completed three-repository diagnostic campaign does not
authorize more attempts or automatic activation.

## First implemented slice: verify-only networking

`Backend.create(..., network_mode="verify")` uses the new fixed-scope helper
`verify-network`, rather than reapplying host settings. Legacy explicitly approved
provisioning remains the default for existing callers until their lifecycle is
migrated and independently reviewed. No automatic caller is enabled here.

The verifier reads the exact fleet nft table in stateless numeric rendering;
missing/additional/changed rules fail closed. It checks both TAPs' ownership,
addresses and up flags, no bridge master, the canary veth pair's reciprocal indices,
exact routes and four already-active proxy/canary services. It never starts a
service or changes a rule, address, interface, route or namespace. Formatting or
version drift rejects rather than falling back to provisioning.

This is not a complete host security attestation or an atomic promise against a
concurrent privileged administrator. Installed helper/proxy code, service units,
proxy allowlist and image receipts still require exact reviewed bindings. Existing
per-guest isolation probes remain mandatory before any registration. The read-only
preflight does not independently authorize a job's credentials or effects.

## Remaining implementation before activation

1. One shared Strix reservation at a time, not configured capacity two. Recheck
   available RAM (guest7GiBhost cap plus6GiB reserve), actual fleet/state filesystems,
   backing thin pool physical/metadata space and disk allowance inside the existing
   canonical manager lock. Unknown resources remain occupied.
2. Explicit per-repository qualified image/closure records. Reject stale or missing
   recipes, licenses, runtime artifacts and suite evidence, not silently select the
   old qualified pointer. Scope qualification to actual suites: scaffold success
   does not qualify mutation, Browser E2E or OTel dependencies.
3. Register with `--no-default-labels` and reviewed image/suite-specific labels;
   update only corresponding workflows. This prevents accidental generic backlog
   routing but is NOT a security boundary or exact-job guarantee. Audit every
   eligible workflow's token, secrets, environment and OIDC authority. No pre-job
   hook can make untrusted action preparation safe.
4. Preserve stopped original overlays without creating flattened copies. Use the
   existing canonical resource record to bind retained paths, identities, hashes
   and exact immutable backing image; no second database. Any missing/changed base
   blocks restore. Release slot only after confirmed termination, deregistration,
   recoverable atomic original-file relocation and durable receipt. Uncertain
   relocation leaves occupied capacity; no automatic deletion or base GC.
5. Read-only audit actual assigned run/job/head and outcome; preserve sanitized
   verification diagnostics before cleanup. Treat wrong/unknown assignment or
   missing evidence as unqualified. Bound job, watchdog, observation and cleanup.
6. Complete failure, cancellation, restart, low-pool, wrong-image and clean-up
   tests, independent review, then separately authorized bounded native checks.

## Actual diagnostic findings informing the plan

Browser's assigned acceptance placeholder failed closed; Core's actual mutation
partition failed after344seconds with precise cause still unavailable; Host's
scaffold passed. All three guests/registrations were removed and approved snapshots
retained. None establishes complete repository validation or the50%runtime target.
Rounded allocated sizes: Browser original overlay<1GiB plus14GiBflat snapshot;
Core original10GiB plus23GiBflat snapshot; Host flat snapshot14GiB. Future no-copy
retention could avoid approximately51GiB of duplicated flattened copies for this
workload; existing retained data remains untouched. This is disk savings, not a
claim of faster complete CI.

## Concrete deployment approvals, after exact candidate review

- Replace the fixed installed helper and manager with reviewed hashes, preserving
  owner/mode; install exact per-repo image/catalog and routing configuration.
- Explicitly approve any initial network provisioning or drift repair. Normal
  automatic launch must use verification only, with no repair fallback.
- Enable/start the bounded manager only for the named non-demo repositories and
  audited eligible-job authority, under a specified retention/disk budget and
  stop condition. No new egress, shared host Docker socket or sandbox bypass.
- No backup creation or deletion is part of no-copy retention. Existing snapshots
  remain until separately authorized handling. A retention budget stops admissions
  when exhausted rather than deleting or inventing free space.
- Demo-specific persistent jobs/services must be enabled by the owner personally;
  they cannot be included in assistant-enabled automatic service configuration.
- Guest package scripts that alter security settings need separate approval.
  In particular Ubuntu bubblewrap postinstall invokes a user-namespace sysctl:
  stage only reviewed binary/notices, omit scripts/config, and fail closed if
  existing namespace policy does not permit the qualified sandbox.

## Verifier rendering provenance

The exact numeric nft rendering was read from the already-provisioned Strix host
on2026-11-18 using only `nft -s -nn list table inet runspindle_fleet`. Its IPv6
nfproto is10 and established/related states render as0x2,0x4 in that installed
nft version. The namespace routes were read using
`ip -j -n runspindle-canary route show`: default via198.18.231.1 precedes the
connected198.18.231.0/30 route. No network mutation was used to collect them.
Version/ordering differences intentionally fail closed; these textual bindings
are conservative compatibility pins, not generic nft semantic normalization.

## Additional implemented source primitives (not manager integration)

`automatic_policy.ImageRoute` produces one exact repository/image/tool-closure/
validation-manifest/suite label. Registration must use `--no-default-labels` and
ONLY that label: adding generic labels would still admit old generic jobs by
GitHub subset matching. `capacity_one` admits at most one reservation against
fresh RAM, minimum actual filesystem, corresponding physical thin-pool/metadata
and remaining retention-budget observations. These functions cannot authenticate
qualification records; a reviewed trusted catalog loader remains required.

`original_retention` inspects real qcow2 backing metadata, binds original bytes
and exact immutable base identity, then records intent before moving original
inodes into an existing private same-filesystem directory. It makes no copied
image/log/key/seed, performs no deletion and releases no reservation. Failures
before, during or after moves preserve all original data; recovery must follow the
persisted intent and leave uncertain capacity occupied. Base lifetime/GC protection
must be integrated into the canonical resource state before deployment. The
primitive requires private owner directories; current helper slot ownership/mode
transition must be reviewed rather than silently chmodded or weakened. No current
manager path calls it. Tests use newly created tiny synthetic qcow files, not real
retained guests; they prove inode preservation and changed-base/intent/partial-move
failures, not full production cleanup or restore qualification.

## Catalog/admission integration candidate

A separate source-only `AutomaticAdmission` coordinator now connects trusted
catalog validation, atomic one-Strix-slot reservation and verify-only Backend
creation. Ready registration repeats catalog bindings and isolation, then uses
only the exact route label with `--no-default-labels`. Default drain remains on.
The existing manager quarantines resources owned by this new lifecycle rather
than registering them generically or taking flattened backups. No installed code,
catalog, workflow label or service has changed.

The catalog loader requires root-owned non-writable regular files and exact
manifest/evidence hashes. Its explicit suite schema rejects partial scaffold
qualification: validation requires all seven partitions plus aggregate; Browser
account E2E requires normal/optimized/sandbox/cleanup results. These are trusted
operator-reviewed evidence records, not cryptographic proof or self-authorization.
No actual repository entry has been marked qualified or installed. Backend still
rehashes the actual image and requires its existing recipe/controller bindings.

This coordinator deliberately has no run-loop/CLI activation: restart recovery,
registration absence verification, exact retention/base lifetime integration and
actual assignment observation still need independent implementation/qualification.
Do not use it operationally yet. Capacity input must come from the verified actual
mount/pool probe while holding the canonical lock, not a guessed free-space value.
Requested workflow/manifest digests must come from the exact requested GitHub
source. Routing cannot constrain another eligible workflow's token/secrets/OIDC
permissions; the complete eligible-workflow authority audit remains a prerequisite.

## Owned no-copy cleanup integration candidate

`AutomaticCleanup.finish` now uses the existing canonical resource record and
lock. Exact resource/repository/slot/runner identity is checked before effects.
Stop and deregistration are separately attempted under bounded cleanup even when
the first state write fails. Repeated cancellation is deferred through cleanup;
the existing host watchdog still handles uncatchable failures. No copied guest
archive, flattened image or deletion is used.

Only after termination and registration absence are verified may the original
inodes move. Intent and completion are stored in the existing resource details.
Any uncertain step keeps capacity occupied. Existing interrupted retention intent
is deliberately not retried blindly; explicit partial-move recovery remains a
blocker to unattended operation. A successful completion releases the slot but
retains the resource row and immutable-base references. The read-only
`assert_unreferenced_image` guard rejects deletion/replacement of referenced bases;
no garbage collector is enabled. Other image writers/promoters must be audited
and integrated with this guard before activation, so this is not a claim that all
external base writers are already constrained.

The helper source proposes tightening a stopped slot directory to0700 after
confirmed cgroup termination and ownership return. This is a security-sensitive
installed-helper change requiring separate deployment approval; it has NOT been
applied. The no-copy primitive currently rejects the old0770 slot mode rather
than silently loosening its private-directory check.

Tests use actual newly created tiny qcow images and actual temporary SQLite,
with all host stop/GitHub operations mocked. They cover preserved inode identity,
restart/base-reference retention, failure of stop/removal/DB updates, and refusal
to retry an existing partial intent. No real retained guest has been moved or
recovered with this new code, and no automatic run loop is enabled.
Catalog applicability is deliberately exact-source: requested head must equal the
qualified source revision, which is durably retained and included in the custom
runner label. Later commits with identical workflow/manifest are NOT admitted.
Automatically generalizing image qualification across code revisions requires a
separate dependency/lock/input applicability policy and review; this conservative
candidate does not yet satisfy unattended validation of every new commit.
