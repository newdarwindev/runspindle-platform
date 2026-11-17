# Native monitoring artifact continuity audit

Base: `0cd1817f6aa69cce3ae47c5ddea8654eb5bc8b67` (Platform #42 merged).
This is offline artifact/evidence-contract maintenance, not current runtime
qualification or closure of Platform #14/#3 or composition H08.

## Audited drift

Comparing all 15 manifest entries to actual source bytes found two stale entries:

- `STATUS.json`: `056cb4a2...` to `a88a334d...`. Changes from Mac admission and
  isolated campaign guard commits (`ecda3ed`, `2517a11`) add bounded source-test
  status and explicit live-disabled/unqualified limits. The native-monitoring
  status section and its blocked live acceptance remain unchanged.
- `tools/collector_privacy_check.py`: `1207ceac...` to `83a478f4...` from
  `a4f8a0a9f846427a3484d8871c156d4b78a4b567`. The actual diff adds exact
  producer/configuration/policy correlation, duplicate/ambiguous identity rejection,
  malformed-hierarchy handling and Boolean-only transport acceptance. It does not
  run or authenticate a native producer. See telemetry-correlation-evidence.md.

The other 13 pre-change artifact hashes matched. The manifest additionally binds
this patch's changed checker, regression tests and corrected specification.

The original checker deliberately compared current and historical harness bytes.
A blind rehash correctly still failed `prior source evidence drift`. The new
checker initially admitted only the exact old/current privacy-harness pair above, rejecting
both an unknown rehashed current harness and a rewritten historical identity.
Other historical source comparisons remain exact equality. Its returned result
explicitly states current harness runtime qualification `not_run` and prior
runtime evidence `historical_only_classifier_changed`.

Historical receipts `docs/maintained-offline-tests.json` and
`docs/native-monitoring-validation.json` are byte-for-byte unchanged. Their old
passing runtime/aggregate entries describe their historical runs, not this tree.
In particular, the old receipt's unchanged-harness statement does not describe
current source. No live run is fabricated to repair the manifest.

## Fresh validation

Python 3.12.14 on Linux x86_64:

- Monitoring checker: passed, 19 source units / 14 surfaces; runtime not run.
- Native monitoring tests: 29 passed normally and optimized, including raw drift,
  rehashed unknown harness, rewritten historical identity and explicit scope tests.
- Telemetry tests: 11 passed normally and optimized; offline synthetic fixtures.
- Strict mypy on checker/tests: passed. Ruff selected checks passed.
- Explicit boundary suite: 445 tests, 424 passed, 21 skipped; Temporal runtime not run.
- Scaffold and whitespace checks: passed.

Required aggregate remains **failed**, not replaced by the boundary run: existing
static-quality debt, unavailable prepared required Temporal lane, and maintained
mutations requiring Linux ARM64 on this x86_64 host. Current native-monitoring
check passes within that failed aggregate. No requirements, quality baseline,
runner admission, service activation or live evidence gates are relaxed.

## Annotation-only continuity after Platform #44

The current endpoint is now `4ccd22cb3bd80727cafd7256963480a6aaa806f3d8dfddeca57e2e76c999efa5`,
not `83a478f4...`. The latter is retained above as the audited intermediate
revision. Only two helper annotations, one local annotation, imports and formatting
changed; no predicate or side-effect behavior changed. The checker still requires
the original `1207ceac...` historical identity and admits only the new exact current
endpoint. Historical receipts remain untouched and current runtime remains not run.
See [harness quality evidence](harness-quality-evidence.md) for fresh results.
