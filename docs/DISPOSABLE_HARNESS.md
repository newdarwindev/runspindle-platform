# Disposable synthetic harness — Platform #1

This lane implements the local fake acceptance contract from [issue #1](https://github.com/newdarwindev/runspindle-platform/issues/1).
It reuses the existing Collector diagnostic fixture and canary scanner, and adds
bounded dependency doubles, a real disposable SQLite spool/backend, and dictionary
filesystem/process/service-manager adapters. It does not launch upstream services.
All data and identities are invented. No private history, provider credentials,
host enrollment, downloads, network changes, backups or persistent services occur.

Run from the repository root:

```sh
python3 tools/check_disposable_harness.py
```

The command runs the synthetic tests and seven mutation challenges, rejects
socket audit events in its own process, and writes
`.verification/disposable-harness.json`. Repeat with `python3 -O` to check that
Python assertion removal cannot bypass implementation guards. The declared lane
pins the observed CPython 3.14.8 Linux x64 interpreter executable by SHA-256 in
[`fixtures/harness/manifest.json`](../fixtures/harness/manifest.json). Other
interpreters are **blocked**, never counted as qualification. This executable pin
is not a complete OS or shared-library supply-chain inventory. The portable unit
tests can also be discovered by the existing repository test command; those unit
results do not qualify another interpreter or native target.

No third-party packages are needed by the synthetic lane. The repository's
separate static checks require the unchanged `requirements-quality.txt` pins.
`python3 tools/validate.py` remains the full required aggregate; its prepared
Temporal and Linux arm64 installation requirements are unchanged. The explicit
`python3 tools/python_tests.py --temporal boundary` command discovers all component
tests while marking real Temporal tests not-run. Neither is replaced by this lane.

## Criteria and evidence boundaries

| Issue criterion | Implemented synthetic evidence | Separate qualification / limitation |
|---|---|---|
| Pinned manifests | Exact fixture versions, artifact/source SHA-256, interpreter pin, four fake OS/architecture targets, notice inventory and synthetic SBOM; duplicate JSON keys, corrupt/floating/missing entries, links and path escapes fail closed | Synthetic source hashes describe invented bytes. Existing actual Temporal/Collector pin manifests remain separate. Unselected upstream versions block their integration lanes |
| Disposable runner | Injected clock and ID, per-run endpoint namespace, real private temporary directory, readiness/deadlines, endpoint/payload/queue/backend limits, default-deny transport dispatch, cleanup after exceptions and cancellation | Endpoint namespace, processes and services are doubles. No kernel network namespace or OS resource enforcement is claimed. Existing sandbox isolation is retained |
| Dependency failures | Healthy/unavailable/slow/malformed/auth-denied/TLS-denied cases for all seven dependencies (42 combinations) | HTTP status and certificate rejection are modeled; no TLS handshake or real dependency compatibility is asserted |
| Correlated telemetry | Task/session/event/trace/span IDs plus producer/version/config/policy; real SQLite spool and independent read-only backend connection; emitted/queued/rejected/accepted/persisted states, flush/shutdown, reopen/replay, lost acknowledgement, duplicates, unavailable backend and accepted-without-readback | JSON traces for one synthetic envelope shape only; no protobuf or full OTLP implementation, native instrumentation, provider usage, Langfuse API or Collector qualification |
| Whole-envelope privacy | Canary challenges cover resource/scope schema URLs, names, attributes and keys, metadata, paths, tool errors, events, links, trace state and status; synthetic allowlist removes unapproved fields; unmatched backend records are still scanned | Passing this allowlist does not qualify upstream masking. Current native/backend and schema-URL qualification remain blocked until separately tested against exact configuration/source |
| Fake packaging | macOS/Linux × amd64/arm64; checksum/SBOM/notice checks; missing dependency, wrong target, corrupt payload, partial install, migration failure, interrupted upgrade, exception/cancellation at each transition, explicit rollback and unrelated-file preservation | Files/processes/launchd/systemd are dictionaries. No installed-service, native byte, restart or durable installer-journal claim |
| Runner policy | Actual workflow/config fixtures require exact isolated self-hosted labels, no hosted/personal fallback, no PR/fork triggers, read-only content permissions and no persisted checkout credentials; existing Mac job predicate rejects foreign heads/events | Structural policy evidence only; no runner provisioning, registration, job dispatch, fleet activation or authoritative remote assignment observation |

The runner model permits at most eight endpoints, 64 KiB request/response bodies,
32 spool identities and 100 backend records. Its default virtual deadline is ten
seconds, with bounded readiness polling. It accepts injected in-process doubles,
not arbitrary untrusted programs; virtual deadlines do not preempt arbitrary
Python callbacks. The POSIX tests launch only short Python children with five-second
supervisor waits. Their directories belong to the parent, so SIGTERM unwinding and
parent cleanup after a SIGKILL are separately checked. Catchable cancellation of
the standalone suite unwinds cleanup. Killing the supervisor itself with SIGKILL
or losing the host is outside this evidence and needs an independently provisioned
janitor in an actual isolated integration runner; none is installed here.

## Telemetry semantics

Emission commits a synthetic event to a bounded local spool. A successful response
only changes delivery state to `accepted`. The receipt opens the backend database
independently and requires the complete sanitized identity envelope to match before
reporting `persisted`. Replaying after a lost acknowledgement can create two stored
copies; the receipt reports one unique persisted event, one duplicate, and usage
exactly once. Conflicting reuse of an event ID is rejected. Deleting persisted
records changes subsequent observations to accepted-without-readback and zero
observed usage; a prior HTTP result is never promoted to durable evidence.

The allowlist is intentionally specific to the invented producer. It is not a
general sanitization library. Existing Collector 0.163.2 evidence stays historical
and tied to its original source/configuration. Main's changed diagnostic oracle
does not inherit that receipt merely by updating checksums.

## Optional upstream lanes

`integration_plan(name, opt_in=False)` reports `not_run`. An explicit request
reports a **blocked prerequisite plan**, never launches a service or falls back to
a double. It points to the separate existing pin/lane where one exists:

| Upstream | Existing pin/implementation | Unfulfilled execution requirement |
|---|---|---|
| Temporal | `temporal/dependencies.json`, `tools/temporal_lane.py` | Prepared exact bytes, supported isolated host and explicit integration selection |
| Collector | `telemetry/dependencies.json`, `tools/collector_privacy_check.py` | Prepared pinned image and dependencies, supported isolated Docker lane, exact source/config readback qualification |
| Langfuse | `telemetry/langfuse-qualification.json` | Complete qualified dependency/notice closure and independent authenticated API readback |
| OpenBao, PinchTab, KasmVNC, DSPy | No new selected/qualified integration in this harness | Select exact upstream source/image, toolchain, target, license/checksum closure and isolated adapter before execution |

The actual runner prerequisites (internal/default-deny networking, disposable
volumes, bounded processes and cleanup supervision) remain those of the existing
Temporal/Collector lanes. No real-provider prerequisite blocks a fake test, and
no fake test clears an upstream prerequisite.

## Acceptance record and remaining blockers

The JSON report records exact commit plus dirty flag, all harness/config/fixture
input hashes, seed, timestamps, scenario assertions, mutation outcomes and separate
not-run/blocked/unknown fields. Dirty source is identified by input hashes and is
not called exact clean-commit evidence. See
[`disposable-harness-acceptance.json`](disposable-harness-acceptance.json) for the
checked-in summary and baseline comparison.

Before this change, main `16cd5667bce6a71f23bda52b67ad4ca844b341ea`
already failed native-monitoring validation: its artifact inventory references old
`STATUS.json` and Collector diagnostic bytes. The historical Collector receipt is
also explicitly bound to the old diagnostic source. These manifests, receipts and
guards remain unchanged. The full repository static gate also reports diagnostics
on unchanged main files; no debt baseline is expanded here. Required Temporal
preparation and native Linux arm64 installation are unavailable on this host.

Issue #1 stays open: independent final-head review, baseline reconciliation,
required aggregate/native gates, real namespace qualification and optional upstream
qualification have not been completed. The new synthetic lane cannot close those
gates. No production/live acceptance or merge is requested.

## Provenance and maintenance

New harness code, tests and fixture text are original work. No upstream source,
binary, private data or legacy history is imported. Existing
[`provenance.json`](../provenance.json) and `LEGAL.md` continue to govern existing
source rights. Fixture notices grant no blanket third-party license. The manifest
is a static input inventory; the one-command suite checks every declared byte and
notice. The acceptance summary is generated from local test outcomes and must be
updated with an explicit source revision and the same evidence boundaries.
