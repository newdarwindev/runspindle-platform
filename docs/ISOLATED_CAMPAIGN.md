# Bounded isolated campaign guard

Status: source implementation with synthetic fixtures only. Live execution is
disabled. This is not an operational campaign or evidence of successful CI.

The existing candidate queue command serves repository queues. A runner can
claim an older matching job, and a successful receipt is not eight-repository
coverage. This new module is separate from that command: it does not silently
change its behavior, install anything, or supply an alternative activation path.

## Implemented boundary

`tools/runner_fleet/isolated_campaign.py` validates a plan containing exactly
one target for each of the existing eight repositories. Each target binds a
branch ref, source SHA, workflow content SHA256, run ID, run attempt and job ID.
The workflow path, dispatch event, source repository, Strix job name and unique
campaign label set are checked against independent observations. Image/source,
recipe, closure and executor hashes remain separate exact pins. The module
validates their format; a future adapter must independently verify their bytes.

A private, separate SQLite ledger implements:

- A durable total attempt cap of at most eight and at most two occupied slots.
  Failed/uncertain attempts consume the cap. Completed attempts remain counted.
  Each target gets one attempt, with no replacement attempt or retry.
- Transactional admission before any hypothetical external effect, immutable
  plan comparison on reopen, and persisted admission/job deadlines. Backwards
  wall-clock movement halts the campaign. A caller's elapsed-time observation
  can narrow admission but cannot reset the persisted deadline.
- Exact requested and actual assignment comparisons. A mismatch halts the
  campaign and leaves the attempt uncertain. This is attribution, not a claim
  that the wrong assignment was prevented before execution.
- Storage charges before writes, retained charges on failure, and no refunds.
  The initial free-space check reserves all capped attempts plus the original
  30 GiB host reserve; subsequent observations must cover remaining obligations.
- Unique, exclusive, fsynced and hash-verified capture chunks. A failed write
  preserves its file and charge. Budget exhaustion refuses a new write and
  preserves prior files. Source streams and originals are never truncated,
  deleted, rotated or rewritten by this module.
- Cleanup uncertainty keeps capacity occupied and halts further admission.
  All eight expected targets must complete before guard coverage is complete.
  Every report still says live execution is disabled and is not CI qualification.

The ledger accepts observations supplied by its caller. Its cleanup booleans
are a policy interface, not an implemented host/runner/artifact verifier.
There are no imports or calls to GitHub, the fleet State constructor, guest
launchers, registration helpers or service/network controls. Both normal plan
inspection and `--execute` remain blocked; execution has no override flag.

## Retention envelope

Each attempted guest reserves two 24 GiB disk extents plus 64 MiB QCOW2 allowance
per extent, 16 MiB miscellaneous metadata, and a configured 16 MiB–1 GiB log
allowance (256 MiB by default). A campaign reserves another 64 MiB. Capture
charges include 8 KiB overhead per bounded chunk. Existing artifacts are excluded
from this new allocation and remain preserved; the calculation uses observed
free bytes, not total filesystem size or reclaimable space.

At defaults, eight attempts require **387.1875 GiB additional free allocation**,
plus the unchanged **30 GiB reserve**, or **417.1875 GiB free before admission**.
One capped attempt requires 78.453125 GiB free. These conservative allowances
are accounting policy, not a filesystem quota or an enforced limit on QEMU,
serial output, snapshots, journals, concurrent writers or GitHub logs.

There is no live writer adapter or independent supervisor in this change.
All external writers must be fenced/accounted before this envelope can become
an operational guarantee. Such an adapter must observe allocated bytes on the
actual filesystem, reserve conversion peak space, stop producers with enough
headroom for buffered output and cleanup, and retain complete existing evidence.
It must not rely on a periodic free-space check as a hard I/O limit. If a full
verified snapshot cannot fit, preserve the original and mark cleanup uncertain;
never release the reservation to make progress. Closing this gap requires
separate design, review and target qualification, not a larger numeric allowance.

## Assignment constraints and proposed adapter

A unique label added to all ordinary Strix labels does not isolate a runner:
older jobs can request the ordinary subset. Proposed campaign workflows must
request the unique campaign label, while campaign runners omit at least one
ordinary required custom label. The ordinary workflows and registrations must
remain unchanged. The current scheduler requires the exact configured lane
labels, so a separately reviewed campaign-specific route is also necessary.

Before a real adapter is enabled, verify an available server-enforced
runner/workflow restriction and its scope against GitHub's actual capabilities.
An allowlisted requested run, an operator boolean, a private label, an observed
claim or a post-assignment rejection does not establish pre-execution exact-run
binding. If that guarantee is unavailable, keep execution blocked and obtain an
explicitly different queue-scoped authorization/design; never call it isolation.

New workflow routing would need new reviewed commits and exact workflow/source
bindings. No workflow or label changes are included here. A future approved
adapter must resolve and record the new run/attempt/job identities after exactly
one dispatch per repository, then revalidate the entire binding before admission
and registration. Any unresolved or successor attempt must stop execution.

## Remaining work and approvals

This module deliberately provides no live dispatch, token, registration,
supervisor, disk writer, host observer, cleanup executor or promotion adapter.
Admission/job deadlines are persistent policy checks, not independent process
termination. Cleanup duration is bounded in the plan but not executed here.
The existing repository-queue command is unchanged and must not be substituted
for the disabled isolated path.

Required future work includes independent source review; synthetic adapter
failure tests; supported server assignment enforcement; complete writer/resource
fencing and target filesystem verification; exact changed executor/workflow
pins; sufficient preserved storage; and separately approved bounded live tests.
The plan still allows only two Strix slots and the original time ceilings.
Eight long jobs may exceed the bounded admission window; a blocked partial
report is preferable to extending deadlines or retrying automatically.

Operational approval must separately identify drain-state changes, credentials
use, workflow publication/dispatch, ephemeral registration, guest and dedicated
network/service actions, retention capacity and failure reconciliation. It does
not follow from this source PR. Keep selected and rollback images unchanged;
manager activation, promotion, campaigns and workflow/security changes require
their own authorized actions.

Synthetic checks run entirely in temporary directories, including separate
processes racing real SQLite transactions. They cover restart/cap/concurrency,
identity mismatch, deadline reset/backwards time, disk/log exhaustion, incomplete
writes, preservation, uncertain cleanup, incomplete coverage and the disabled
live entry point. They do not run the fleet or qualify real GitHub assignment.
