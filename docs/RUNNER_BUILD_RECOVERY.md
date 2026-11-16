# Runner build retention and stopped-slot recovery

Status: source fixes and temporary-fixture tests only. No existing guest, slot,
reservation, selected image, installed helper, service or network policy is
recovered or changed by this document. No qualified-image or live-CI claim.
This implements the preservation and uncertain-effect requirements in
[desired intent](../specs/desired-intent.md) and [fleet operation](RUNNER_FLEET.md).

## Failure prevention

Docker documents unlimited `json-file` logging as the default when `max-size`
is omitted; the explicit string `max-size=-1` is rejected by the pinned daemon.
See [Docker's logging options](https://docs.docker.com/engine/logging/drivers/json-file/#options).
Retained probes, servers and clients now use create/inspect/start, with
`json-file` and `mode=blocking`. The effective inspect result must contain no
`max-size` option. An inherited rotation limit or nonblocking mode blocks start;
we do not edit daemon configuration, cap logs, delete containers, or substitute
a permissive retention result. Created containers and command diagnostics remain
available when inspection fails.

Two bind mounts of the same filesystem can report equal `st_dev` values while
`rename` between their mount points still returns `EXDEV`. Comparing device IDs
cannot establish that original files can be relocated. Stopped-slot originals
now stay under `/var/lib/runspindle-fleet/retained/<resource-id>/`, beside the
slots on the fleet mount. The root helper creates only that fixed-scope private
archive after verifying resource ownership and stopped guest termination. Its
parent is root-owned mode 0711; each archive is operator-owned mode 0700, with
symlinks, foreign ownership, unexpected permissions and ACLs rejected.

The standalone QCOW2 and logical/byte verification receipts remain in private
state storage. Original disk, serial log and seed preserve their inodes through
same-mount rename. The ephemeral key is retained privately in its original state
directory. Capture and original paths are recorded separately. There is no
copy-and-delete fallback: a remaining rename, integrity, permission, or unknown
slot-file failure keeps the reservation occupied. Earlier captures and failed
attempt artifacts are never replaced. The guest image's backing base remains
retained separately; the independently checked standalone copy has no backing
file dependency.

## Recovery requires a separate bounded approval

Source publication does not authorize operating on a stopped slot. Before
recovery, obtain approval for these exact effects:

1. Install the reviewed root-owned `host_helper.py` from an exact source revision,
   preserving and verifying the prior installed file. This is an action-time
   change to the privileged helper, including creation of the fixed private
   retention directories. No bootstrap, access enrollment, firewall/TAP changes,
   service enabling or service start is needed.
2. Reconcile one named, already stopped **image-build** reservation: retain its
   disk/log/seed/key originals and verified captures, then release that reservation
   only after cleanup proves the slot empty and ownership unchanged. Existing
   images, prior captures and unrelated reservations remain untouched. Account
   for a fresh standalone copy plus the configured host storage reserve.

The operator must review the exact source diff and helper digest, verify the
fleet manager remains inactive, obtain the shared manager lock, and check the
named reservation's recorded source image, digest, slot, build request (`-2`)
and owner marker. Observe `MainPID=0` and an inactive/failed guest, verify no
dedicated-account guest process remains, and hash the original disk, backing
base and previously retained standalone copy against their recorded receipts.
If anything changed, stop and retain the uncertain reservation. An image-build
reservation never registered an Actions runner; do not apply this abbreviated
procedure to a job reservation with possible external registrations.

Use the reviewed checkout's `State` and `Backend`, with `Backend.image` explicitly
set to the recorded sealed preparation base (not the default qualified image).
Under `State.lock()`, set only the named reservation to `cleaning` with its
recovery reason; call `Backend.destroy(resource)`. This invokes the installed
fixed-scope stop helper, verifies termination again, retains/compares the guest
bytes, moves originals on the fleet mount, and verifies the slot is empty.
On any exception, record `uncertain` plus the error and stop without retries or
manual SQL release. On success, independently re-read helper observation,
original/capture receipts, slot contents and the selected/prior image digests.
Only then set this reservation to `destroyed` with `cleanup_verified=True` and
`operational_status=blocked`; the failed build remains failed. No image promotion,
manager resume, queue campaign, new build, or network reapplication is part of
that recovery approval.

A subsequent image build needs its own bounded approval and an exact source /
recipe / closure identity. Fresh sealed-image workload, integrity, isolation and
cleanup gates still apply before selection; synthetic tests cannot replace them.

## Source checks

The regression suite uses temporary synthetic fixtures and tiny real QCOW2
files; it starts no VM, Docker daemon/container, host service or CI job. The
bind-mount boundary is fault-injected without creating mounts. Tests verify
nonrotating blocking logging before start, rejection of inherited rotation,
private-archive boundaries, preserved original bytes/inodes, verified standalone
logical equality, and fail-closed rename/capture faults. Real Temporal integration
checks remain opt-in and are not recovery evidence.
