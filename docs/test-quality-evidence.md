# Typed synthetic test boundaries

Base: `fc5d2e119daea05c4878269059afd63564959cba`.
Bounded validation-quality maintenance; no issue closure or production qualification.

Three test files now satisfy strict static checks without changing runtime code:

- Correlation fixtures expose explicit callable/JSON types. Intentionally malformed
  inputs remain accepted by the test boundary and passed to the real classifier.
  Dynamic mutation loading checks missing source/spec/loader explicitly; these
  guards also run under optimized Python. The wrong-policy mutation must still
  fail the independent oracle, and the no-subprocess spy remains active.
- Lifecycle mocks resolve the same exact module attributes via dotted patch names,
  avoiding implicit re-export assumptions without broadening mock scope.
- Test launcher imports are sorted.

Python 3.12.14, Linux x86_64 observed results:

- Correlation 7, lifecycle 36 and test-selection 6 tests passed normally and optimized.
- Strict mypy on all three changed tests passed; Ruff checks/format passed.
- Explicit boundary suite: 445 tests, 424 passed, 21 skipped; Temporal runtime not run.
- Scaffold and whitespace checks passed.
- Static diagnostics above the unchanged 776-entry baseline fell from 70 to 9.
  The remaining nine concern the existing untyped/unformatted privacy harness;
  that runtime source is intentionally unchanged in this patch.
- Required aggregate remains failed: those nine static diagnostics, missing
  prepared Temporal coverage, and maintained mutations requiring Linux ARM64.

No quality baseline, suppression, historical receipt, runtime manifest, installed
runner, service, source permission or qualification gate changed. Focused success
is not a claim that the required aggregate passed.
