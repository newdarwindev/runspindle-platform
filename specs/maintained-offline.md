# Bounded telemetry and development distribution

This native Python/test mapping implements the platform portion of
`runspindle.maintained-evidence.v1`. Existing Temporal and fixture contracts are
unchanged. Operational install/deploy entrypoints remain fail closed.

`telemetry/dependencies.json` pins standard Collector contrib 0.163.2, its Linux
arm64 image, binary, source revision, official SPDX SBOM and Go build metadata.
The binary extracted without execution matches the official SBOM checksum.
The SBOM supplies exact dependency/license evidence; explicit source closure
records cover the generated OBI release, Go standard library and distribution
source. The avro replacement recorded in Go build metadata is represented by
its actual replacement module in the SBOM. Missing license declarations for
go-asap and Go are resolved by retained version-specific upstream notices.
No custom Collector receiver, native uploader or transcript service is added.

`tools/collector_privacy_check.py` validates and runs actual standard processors
with synthetic OTLP input, an internal network, no published ports, resource
limits and disposable volumes. It checks the entire exported envelope for
canaries placed in resource/scope schema URLs, attributes, names, versions,
tracestate, status errors and span events, plus malformed request metadata and
internal error logs. The pinned standard transform supports both schema URL
setters. An initially attempted `set(events, [])` passed configuration validation
but failed at runtime; the retained profile instead sanitizes span events in
their own context and filters unsupported linked spans. This demonstrates why
configuration acceptance alone is insufficient. Allowed identity attribute
values are synthetic trusted inputs in this lane; this is not a claim of
sanitizing arbitrary identity fields or native producers.

The receiver's HTTP response is transport acceptance only. Independent standard
file-exporter readback of matching task/session/event/trace/span IDs and exact
synthetic producer/config/policy context establishes
`persisted_file_only`; it does not establish Langfuse persistence. Explicit
duplicate submissions produce two observed records and one duplicate; no token
or cost usage is inferred from telemetry count. A malformed envelope receives
HTTP 400 without another backend record. A buffered event accepted before
SIGKILL is classified from actual final file readback: the retained runs observe
loss from a volatile export buffer. Absence before reconciliation remains
unknown, rather than delivered/persisted.

`tools/collector_recovery_check.py` uses two standard Collectors and the standard
`file_storage` exporter queue. With the backend absent, acceptance and a queue
file are observed without backend persistence. A new Collector process after
SIGKILL exports the retained sanitized event when the backend becomes available;
an independent file readback finds exactly one correlated event. This is a
Collector-to-Collector/file diagnostic lane, not a replacement Langfuse backend.
Neither lane qualifies native telemetry or console readback. Both clean up
containers and networks on success, exceptions and cancellation.

Local Langfuse 3.63.0 dependency qualification remains **blocked**. Its exact
source/image candidates, MIT/core and separate EE notices, upstream lock digest
and observed public artifact-access failures are recorded in
`telemetry/langfuse-qualification.json`. The chosen MinIO dependency could not be
resolved from Docker Hub/Quay, and official checksum/signature endpoints return
410 Gone. No Langfuse service, database or object store was executed. Complete
worker/OS/database/storage inventories and actual Langfuse API readback remain
required. No credentials, substitute storage service or floating-tag execution
was introduced.

`tools/development_bundle.py` builds a canonical checksummed **development
metadata bundle**, not product binaries or installed services. It requires seven
exact component commits, composition revision, immutable dependency/toolchain
digests, target and included license bytes. It rejects unknown versions, changed
bytes, pins/architecture mismatches, traversal and floating dependencies before
installation. Unsupported product capabilities remain explicit in every bundle.

`DisposableTarget.create()` permits installation only in a new Linux arm64
temporary target. The owned schema-versioned SQLite journal retains immutable
bundles and predecessor references with generation ownership; intent commits
before copying files. Installed bytes are checked before atomically replacing a
version pointer. Recovery verifies actual files and pointer state before
finishing an interrupted upgrade. Rollback requires retained, checksum-correct
predecessor bytes and refuses absent history, stale generations or unavailable
files. Tests kill fresh Python processes after intent, first file and pointer
replacement, then recover in another process. There is no production target,
host enrollment, daemon, service manager, backup or cloud adapter. Power loss,
hostile filesystem races and concurrent external modification are unqualified.

A retained historical digest cannot acquire a new installation intent. It stays
reachable only through verified predecessor rollback. Repeating the exact
active bundle checks the installed bytes and returns the current receipt
without allocating another generation or rewriting the pointer. A damaged
active copy returns an error and leaves its files and journal unchanged.

Run `python3.13 tools/collector_privacy_check.py --report local/privacy.json` and
`python3.13 tools/collector_recovery_check.py --report local/queue.json` after
explicit preparation of the immutable images in the dependency/toolchain
records. Runtime uses `--pull never`. Run `tests/test_development_bundle.py` and
`tools/check_maintained_mutations.py` inside the pinned Linux arm64 Python image
with no network and a disposable `/tmp`; ordinary host tests report the Linux
installation cases as skipped. Preparation, structural checks, synthetic model
tests, actual upstream diagnostic lanes and full-product acceptance remain
separate. See [exact evidence](../docs/maintained-offline-tests.json).

## Streaming executable development bundle

The separate `runspindle.streaming-development-bundle.v1` interface implements
composition's streaming contract, merged in PRs #40 and #42. The existing JSON
metadata bundle and its validation limits remain unchanged. The streaming format
uses a bounded canonical manifest followed by uncompressed regular-file bytes;
there are no tar/link records. It accepts confined dependency-wheel filenames,
including punctuation and leading dots, without treating them as shell text.
Traversal, noncanonical names, backslashes, drive prefixes, unsafe modes, duplicate
fields, unsupported versions, extra/truncated/changed payloads and mismatched
pins/target/inventories/notices reject before installation.

`tools/streaming_bundle.py` retains the exact archive separately from the SQLite
journal, avoiding executable-size BLOBs. A generation-owned immutable manifest
intent commits before staging. Recovery checks archived, extracted and installed
bytes and modes before switching the version pointer. Extraction applies the
manifest's final file mode before syncing each file, then syncs the staging
directories from the leaves to the root. A directory sync failure leaves the
active pointer in place and keeps the generation's pending intent for recovery
from the retained archive. The streaming tests record syscall order and inject
a staging-directory failure before retrying in a fresh process. These tests
cover the local filesystem interface and process recovery.

A rollback intent records
the exact verified predecessor; missing/corrupt predecessor files are not
regenerated. Fresh processes test interruption after intent, staging, pointer and
rollback intent. Competing owners, stale generations, conflicting readback,
unsafe links and failed rollback have negative tests. Bounded local Linux arm64
containers use denied egress, disposable targets and executable temporary mounts;
no persistent runner or production security configuration changes.

Streaming rollback accepts `predecessor_component_commits` when the retained
predecessor has different pins. `component_commits` still identifies the current
release, and the generation and observed pointer must match that release. The
current retained archive must verify against its journal entry. The predecessor
must verify against its own archive, pins and installed bytes before the rollback
intent commits. A damaged current installed payload is preserved and does not
prevent restoration of that verified predecessor. After an interruption, call
`recover` with the pending predecessor's pins. It rechecks the predecessor and
completes the same journal generation. Existing same-pin callers can omit
`predecessor_component_commits`.

The official selected MinIO release source is now acquired at immutable revision
`0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`. The source record inventories 256
module archive/mod hashes and exact notice evidence. Three Go-derived modules
explicitly reference the retained Go BSD notice; `mpb/v8` includes UNLICENSE.
The `go-localereader v0.0.1` archive declares MIT and omits the full notice. The
upstream maintainer added the complete notice in a GitHub-verified merge whose first
parent is the exact v0.0.1 commit. The merge adds only `LICENSE`. The verifier
accepts the [retained notice](../telemetry/notices/go-localereader-MIT-LICENSE)
only for the existing module archive, `go.mod`, Go sums, and README hashes.
See the [upstream merge](https://github.com/mattn/go-localereader/commit/2491eb6c1c75720122ef321ed7acc3a8d9de95b1)
and the [source report](../telemetry/minio-source-qualification.json).

The helper returns 78 on incomplete notice closure. A prepared source report
records that all notices were verified. The pinned Linux build, worker/OS/database
and storage inventories, and actual Langfuse API export, outage and restart
readback remain pending. The original blocked run remains in the
[historical evidence](../docs/offline-finish-tests.json).

The [MinIO cross-build record](../telemetry/langfuse-qualification.json) captures
the Linux arm64 binary digest from the macOS arm64 Go 1.24.2 toolchain. First run the
source-qualification helper against the complete pinned cache. In the clean MinIO
source checkout, set `GOMODCACHE` to that verified cache and `GOCACHE` to a fresh
directory. Use the exact Go 1.24.2 executable and write the binary outside the
checkout as `$MINIO_OUTPUT`.

```sh
unset GOPATH GOROOT GOFLAGS
export GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOVCS='*:off'
export GOENV=off GOWORK=off GOTELEMETRY=off
go mod verify
flags=$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go '2025-04-22T22:12:26Z')
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -mod=readonly -p 6 \
  -tags kqueue -trimpath -ldflags "$flags" -o "$MINIO_OUTPUT" .
go version -m "$MINIO_OUTPUT"
```

Both the pinned Linux build environment and service execution remain pending.

The user waived independent review for this offline tranche. No independent
review is recorded as having occurred. Full-product and live gates remain blocked.

The installation journal now fences payload families at construction and at each
state read inside owned transactions. Metadata and streaming installers cannot
reinterpret each other's retained payloads, including objects opened before a
competing installation. A real Linux regression first demonstrated acceptance
through an already-open incompatible installer; tests now reject both directions
before intent and preserve the active pointer/generation. Existing metadata
encoding, validation limits and valid journal behavior are unchanged. A fifth
behavioral mutation disables this fence and must be caught by that regression.

Metadata installation readback must match the journal's active or pending digest,
not merely any retained historical bundle. This preserves readback on either
side of a recorded installation/rollback pointer switch while rejecting an
unrelated retained version. `tests/test_metadata_pointer.py` exercises actual
scratch SQLite/files with synthetic architecture admission; it does not qualify
ARM64 installation, product binaries or installed services.

Both installers read the active pointer through the same bounded regular-file
reader. It accepts one `manifest_sha256` field containing a lowercase SHA-256
digest, with a 256-byte file limit. Duplicate fields, malformed identities and
oversized files reject before recovery can act on the pointer. A missing path
represents absence. A dangling link or a special file rejects, and the reader
opens without waiting for a FIFO writer.

Pointer-fence verification against base `9d9c09a531205829ad2ca9dc831575b45b5d4692`:
CPython 3.12.16; exact `requirements-quality.txt` tools; six focused tests passed
in normal and optimized modes. Direct discovery ran 208 tests: 195 passed and
13 native/Temporal installation tests explicitly skipped. Pinned static checks
reported zero new diagnostics and 863 existing diagnostics; scaffold passed.
The existing portable attribution fixture now binds its journal active digest
to its inserted bundle, preserving its unsupported-architecture assertion and
five portable mutation checks. Removing the new pointer guard made the historical
pointer regression fail because the expected `ValueError` was not raised.
Reproduce focused evidence with `python3.12 -m unittest discover -s tests -p
test_metadata_pointer.py` and repeat with `-O`. Full aggregate, actual ARM64
installation, power-loss recovery, services and product qualification were not
run. The source change does not expand the supported installation targets.

## Disposable install journal recovery

The metadata and streaming development installers share a version 1 SQLite
journal. On reopen, both compare the retained table and trigger definitions
with the version 1 schema before accepting the journal. A missing retention
trigger, weakened immutability trigger or other schema mismatch blocks recovery.
The installer creates this schema only in an empty unversioned journal.

The metadata regression opens a three-release scratch journal in a fresh
process after removing or weakening its immutability trigger. Both cases
leave the journal contents, active pointer and installed payloads untouched.
The same test opens the damaged journal on base `38e608f`. Run it with
`python3 -m unittest discover -s tests -p test_metadata_pointer.py`.
The test injects target admission and uses actual SQLite and files. Native
target recovery and installed-service qualification remain separate gates.

## Regular-file inventory admission

Both bundle formats reject `.` as a file: it denotes the destination directory,
not a regular-file payload. Metadata bundles also reject a file whose ancestor
is another file record, matching the streaming format's existing collision
rule. These checks apply during construction and incoming manifest verification,
before installation intent, generation changes, staging or pointer effects.
Valid nested siblings and streaming dependency filename punctuation are unchanged.

`tests/test_bundle_path_admission.py` uses externally constructed, correctly
rehashed malformed bundles, actual scratch SQLite/files, synthetic architecture
admission and no native constructor. It independently observes zero SQL statements,
checkpoint/recovery calls and filesystem changes on rejected installation.
Against base `9d9c09a531205829ad2ca9dc831575b45b5d4692`, CPython 3.12.16 ran
six focused tests in normal and optimized modes. Direct discovery ran 208 tests:
195 passed and 13 native/Temporal cases explicitly skipped. Exact pinned static
checks reported zero new diagnostics and 869 existing diagnostics. Three
independent guard-removal mutations (metadata root-dot, metadata ancestor conflict,
streaming root-dot) triggered assertion failures. Scaffold passed.

Reproduce the focused checks with `python3.12 -m unittest discover -s tests -p
test_bundle_path_admission.py` and repeat with `-O`. Full aggregate, native ARM64,
Temporal, guest/service operation and product installation qualification were not
run. This source-only correction does not change target admission or establish
full product release acceptance.

Offline correlation hardening rejects ambiguous duplicate identity attributes,
wrong or missing producer/config/policy bindings, and malformed matching shapes.
HTTP acceptance remains separate from exact file readback, and whole-envelope
canary scanning still includes unmatched records. This is diagnostic-oracle
coverage, not a new Collector or Langfuse execution receipt. See
[correlation evidence](../docs/telemetry-correlation-evidence.md).
