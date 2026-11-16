# Exact synthetic telemetry readback correlation

Partial offline Platform #14/F13 progress, based on
`14e620c9a5a35d2584544831cfe1fa260a2b86b7`. No runner or service code changed.

## Demonstrated gap

The file-readback oracle previously counted records matching task/session/event/
trace/span as persisted even when producer, configuration digest or policy digest
belonged to another synthetic context. Duplicate OTLP attribute keys silently used
the last value. A wrong policy digest was reproduced as `persisted_file_only` on
the unchanged code. This did not authenticate a source or establish Langfuse
persistence, but it could overstate the diagnostic's exact correlation.

The oracle now requires all eight existing identity/context values. Duplicate
attribute keys, missing or non-string required identity values, ambiguous required
AnyValue fields and malformed matching hierarchy branches do not correlate.
Unrelated non-identity attribute values are not generally validated; this is not
a full OTLP schema validator. Nonmatching records do not inflate matching/duplicate
counts. All original records, including unmatched records, are still scanned for
synthetic privacy canaries. HTTP acceptance and readback remain independent;
only the Boolean `True` reports transport accepted.

## Reproduction and observed evidence

```
python3 -B -m unittest discover -s tests -p 'test_telemetry*.py' -v
python3 -O -B -m unittest discover -s tests -p 'test_telemetry*.py' -v
python3 tools/check_scaffold.py
```

All 11 tests passed normally and optimized (Python 3.14.8). New tests cover every
required resource field, both orderings of conflicting and identical duplicate
identity attributes, required AnyValue ambiguity, malformed matching structures,
exact-only duplicate counting, whole-envelope privacy and transport separation.
Independent process spies observed zero subprocess calls from correlation.

A deliberate context-guard removal makes the wrong-policy record appear persisted
and must fail the independent expected-unknown assertion. The existing
`fabricated_persistence` mutation also remained detected using its bounded portable
mutation runner. Scaffold and whitespace checks passed.

Independent source review found no concrete blocker; reviewer separately ran six
focused tests normally and ten focused/existing tests optimized. The reviewer did
not execute the temporary-module mutant. These are offline synthetic oracle tests,
not a new actual Collector execution receipt.

## Remaining gates

No Collector, Docker, Langfuse, native producer, network, service, install or live
integration ran. Existing real downstream persistence/readback and dependency/
notice prerequisites remain separate and blocked/unqualified here. No production
authenticity, privacy sanitization of arbitrary input, general OTLP validity or
full H08 acceptance is claimed. Full platform/runner suite, static tooling and CI
were not run for this bounded non-runner patch. Issues #14/#3 remain open.

## Input SHA-256

- `tools/collector_privacy_check.py`: `83a478f438bd4e45a005ea308114dee37dfd35ef7c8225fda44e5bc2f7e4a07e`
- `tests/test_telemetry_correlation.py`: `5e59695fadbd5bd73688d0a0b7237273c79ce7443534eddd7c9e7886a58ce41c`
