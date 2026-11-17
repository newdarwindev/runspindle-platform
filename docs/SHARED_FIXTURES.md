# Reviewed fixture-only upgrade, 2026-11-17

The current bundle is exported from composition commit
[`e283957d80aebc48fb0bd3b6e2a600850ff6dc97`](https://github.com/newdarwindev/runspindle/tree/e283957d80aebc48fb0bd3b6e2a600850ff6dc97/fixtures/v1),
reviewed tree `dee5e9fc7ab96420075dea3cc7ee59d0cb5012b2`.
[Explicit old/new compatibility diff](shared-fixture-compatibility.json), including
observed old/new validator decisions on identical golden fixtures, records
unchanged contract/golden/vector semantics, new `faults.json`, hardened independent
identity/receipt validation and changed origin metadata. No automatic pin upgrade
or runtime compatibility is implied. Verified prior source accepts floating-point
independent revision/receipt counters that the upgraded validator correctly
rejects; both accept the same valid golden fixtures. All six exported files are byte-preserved;
the reviewed exporter filters golden/vector entries for this consumer.

[Current test receipt](shared-fixture-upgrade-tests.json): full Python normal and
optimized suites each pass 447 tests (13 existing skips); 8 focused
fixture tests, scaffold and strict typing for the new test file pass.

Seven fixed schedules (seed 90210) are executed by a separate in-memory model.
The oracle compares observed transitions and actual per-call spy identity/order,
not copied expected output. Extra effects, reordered steps, stale sequence,
unauthorized retry and wrong effect identity are deliberately rejected. Malformed
independent expected identities and receipts cannot exploit Python boolean/integer
equality. Restart remains explicitly blocked for this model, not durable recovery.
Python socket/DNS entrypoints are denied in these tests; this is not an OS sandbox
or qualification of untrusted/native adapters. No services or live effects run.

Run independently of siblings or a running composition:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_shared*fixtures.py'
PYTHONDONTWRITEBYTECODE=1 python3 -O -m unittest discover -s tests -p 'test_shared*fixtures.py'
python3 tools/check_scaffold.py
```

The exact fixture pin, inventory and every file hash are validated before fixture
code executes. Component runtime/dependency pins are unchanged. Full-system,
production, actual transport and live acceptance are not inferred. Original
synthetic source rights/provenance are retained; no third-party source imported.

## Historical prior-pin documentation (retained)

The following text and earlier receipts describe the prior `e8cd577…` fixture
pin only, not the current upgrade. Their test counts/remaining gates are historical.

## Pinned synthetic fixture conformance

Partial work for [issue #1](https://github.com/newdarwindev/runspindle-platform/issues/1); the full component harness remains open. The original synthetic test-only subset is copied without modification from composition commit `e8cd5779968fede213443db13a591c992cd0e183`, `fixtures/v1`. No legacy code, private source, credentials or runtime adapter is imported. Source status and opaque historical provenance remain unchanged.

`fixtures/shared-v1.pin.json` pins the source repository/commit, schema, component and origin/subset manifest SHA-256 values. The consumer verifies the independent reviewed pin chain and all file hashes before importing the vendored validator. Changing content and rehashing the local manifest alone does not update the reviewed pin. Pin changes require source/compatibility diff review and old/new vector testing; internal hash consistency is not source authentication.

Run with preinstalled Python >=3.9, no sibling checkout, package installation or network:

```sh
python3 tools/check_shared_fixtures.py
python3 -m unittest discover -s tests -v
python3 tools/check_scaffold.py
```

Golden envelopes are checked against independent original identity/state/receipt observations. Every applicable negative vector must fail with its specified error; corrupted validator bytes, a self-rehashed changed manifest, wrong origin pin and an unexpected bundle file are separate negative regressions. Tests deny socket construction and assert zero network calls. Temporary fixture copies are removed after positive and exceptional tests. No child processes or services are started. Fixed fixture IDs/time and source seed 90210 are synthetic. Exact input hashes/tool versions and commands are recorded in `shared-fixture-evidence.json`.

This is Python fixture-format conformance only. No cross-language/runtime producer-consumer mapping, authenticated authority, external effects, durable restart, upstream compatibility, isolated integration, GitHub CI, live acceptance or deployment is established. Operational acceptance entrypoints and workflows are unchanged.

Remaining #1 blocker: Qualified upstream source/image/toolchain pins, permitted telemetry/client surfaces, real backend readback and disposable target installer/store adapters are unselected. The schema-URL masking gap stays unresolved. No service, install plan or installed-byte rollback is qualified. The test schema is provisional and does not decide these runtime interfaces. Fixture pins can be consumed in isolation; actual component adapter suites must be implemented and reviewed separately. No passing fixture can replace their blocked gates.

## Verified loader correction

The consumer executes the exact in-memory source snapshot verified against the
pin chain. It bypasses Python bytecode caches, which are not part of the fixture
manifest. A regression plants valid timestamp/size-matching unreviewed bytecode
that raises if executed; fixture conformance still passes using the reviewed
source. This fixes a test-loader trust gap without changing fixture or runtime
contracts. Six consumer tests now pass. Historical adoption evidence remains
unchanged; `verified-loader-evidence.json` records this correction.
