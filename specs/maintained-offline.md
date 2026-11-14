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
file-exporter readback of matching task/session/event/trace/span IDs establishes
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
bytes and modes before switching the version pointer. A rollback intent records
the exact verified predecessor; missing/corrupt predecessor files are not
regenerated. Fresh processes test interruption after intent, staging, pointer and
rollback intent. Competing owners, stale generations, conflicting readback,
unsafe links and failed rollback have negative tests. Bounded local Linux arm64
containers use denied egress, disposable targets and executable temporary mounts;
no persistent runner or production security configuration changes.

The official selected MinIO release source is now acquired at immutable revision
`0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`. The source record inventories 256
module archive/mod hashes and exact notice evidence. Three Go-derived modules
explicitly reference the retained Go BSD notice; `mpb/v8` includes UNLICENSE.
`go-localereader v0.0.1` declares MIT in README but omits the complete original
notice, and its versioned LICENSE URL returned 404. This unresolved notice keeps
the build and Langfuse service execution blocked. The source-qualification helper
returns 78 on incomplete closure. No MinIO binary, storage substitute, Langfuse
API persistence or H08 success is claimed. Worker/OS/database inventories and
actual API export/outage/restart readback remain required after that blocker.

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
