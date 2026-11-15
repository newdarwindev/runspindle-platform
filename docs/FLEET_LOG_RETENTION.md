# Fleet diagnostic retention candidate

This source candidate preserves captures and original stopped-guest bytes. It
is not an installed controller, qualified image or deployment grant. Existing
user data and logs remain untouched until the owner approves the exact plan.

Diagnostic, archive, build and redacted registration output use exclusive
mode-0600 captures with read-back hashes and fsync. Later polls never replace
earlier captures. Linked/nonregular serial sources and occupied startup log
paths are rejected; QEMU serial output also uses append mode. Failed attempts
retain their outputs. The root helper rejects linked slot inputs instead of
unlinking them. Preparation directories remain inside the retained guest image.

## Standalone guest retention

A text tail or runner archive cannot prove all persisted guest logs were read.
After guest termination the candidate therefore inspects the overlay and base
as qcow2, requires exactly the reserved standalone backing image and recorded
base digest, checks the overlay, then converts it to a new self-contained image.
It checks the copy has no backing reference and the expected virtual size, and
uses real qemu-img compare to verify logical disk bytes. Source identity,
timestamps and source/base hashes must remain stable across those operations.
The verified snapshot, computed hashes and complete command results are kept.

Only after verification does cleanup relocate the original overlay inode
into the same resource's evidence directory. The original serial inode is also
retained after verifying its full capture. These are same-filesystem renames;
there is no copy/delete fallback. Input, conversion, comparison, verification
or relocation uncertainty blocks cleanup and preserves all available evidence.
No guest disk or serial log is deleted. The standalone copy remains readable
without the former backing path; the original overlay is preserved as original
bytes, with its observed backing reference recorded. Read-only mode is not a
claim of immutable lifetime sealing or production durability.

## Capacity and approval

The owner must approve the exact controller/build plan, privileged helper scope
and relocation of original guest artifacts before those operations run. Source
tests and PR publication do not authorize deployment, manager resume or existing
data movement. Qualified/previous images, rollback data and logs are retained.

Before any reservation, the build requires the existing host reserve plus three
24 GiB disks (overlay, converted candidate, independent retained guest copy) and
8 GiB incremental evidence: currently110GiB available on the actual fleet
filesystem. Repeated historical retention also consumes capacity; no expiry,
pruning or automatic reclamation is included. The sealed pinned Ubuntu base must
already exist and match; the builder refuses to replace it. Existing candidate
metadata is captured before publishing a new real build manifest.

## Verification scope

python3 -m unittest tests.test_log_retention tests.test_build_capacity -v
exercises snapshots, redaction, original inode preservation, failed guest archive,
comparison/verification failure, foreign backing chains, links/FIFOs, capacity
refusal and cross-filesystem refusal. Qcow2-specific tests use actual local
qemu-img/qemu-io on tiny synthetic disks, recover guest changes after moving the
former backing path, and detect deliberate corruption through real comparison.
They explicitly skip if those tools are absent; skips are not passing evidence.
Other tests mock infrastructure orchestration. Run the complete suite, scaffold
and pinned static quality too. No test here boots a guest, installs a controller
or qualifies live cancellation, installed tools or a production system.
