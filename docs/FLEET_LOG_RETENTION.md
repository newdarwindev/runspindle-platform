# Fleet diagnostic retention candidate

This source candidate preserves captured logs and original stopped-guest bytes.
It is not an installed controller, a qualified image or deployment authority.
No existing evidence or original logs are migrated by publishing this source.

The previous diagnostics path could replace successful text/archive captures on
later polls. Cleanup could delete the guest disk even if SSH never returned a
complete archive. The candidate instead creates exclusive, mode-0600 captures
with read-back hashes; prior captures are retained. Registration output remains
redacted and earlier captures survive. Linked/nonregular serial sources are
rejected. The root helper retains linked slot inputs for reconciliation.

After guest termination, cleanup retains the entire original disk overlay,
records its SHA256 and the observed backing image path/SHA256, and retains the
original serial inode after verifying a complete capture. Relocation is a
same-filesystem rename, with no copy/delete fallback. Verification or relocation
failure blocks cleanup and preserves evidence. A failed SSH capture therefore
does not authorize deletion of guest logs. Successful slot/process cleanup does
not mean historical evidence was deleted or that a full guest archive was read.

## Deployment prerequisites

The owner must approve the exact new controller/image plan before installation.
That plan must explicitly authorize relocation of original stopped-guest disk
and serial files into their per-resource evidence directory. These operations
are not authorized merely by a source test or PR merge. Existing logs stay in
place until that approval; no prune or deletion is included.

Retained overlays depend on their exact backing image bytes. They are **not
standalone images**. The owner must retain all referenced backing image versions,
with their recorded digests and recovery mappings, for as long as logs are kept.
A controller/image rollout must not overwrite or remove the last retained copy
of a referenced backing version. This candidate does not implement a universal
backing-image garbage collector or authorize image promotion. Do not deploy it
until the concrete rollout/rollback plan proves backing preservation; otherwise
keep the current deployment blocked. Root-owned/read-only image files alone do
not establish immutable lifetime retention.

Capacity planning must include all historical guest overlays and snapshots plus
the new build's scratch/output budget and existing admission reserve. Retention
has no expiry or automatic reclamation. Moving evidence across filesystems is
blocked and requires a separately reviewed migration plan. Current guest log
archives and serial snapshots are partial observations; retained disk plus
backing bytes are required for complete persisted guest data reconstruction.

## Component checks

`python3 -m unittest tests.test_log_retention -v` exercises repeated captures,
redaction, original inode/content retention, archive failure, verification
failure, linked/FIFO sources and cross-filesystem refusal using synthetic files
and mocked infrastructure. Run existing fleet tests, the complete Python suite,
`python3 tools/check_scaffold.py` and pinned static quality as well. None of these
tests boot a guest or prove deployed retention, cancellation or image binding.
