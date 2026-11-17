# Annotation-only privacy harness maintenance

Base: `0223e943986f2a42361f049d3c3df62cc61252c2`.
No runtime qualification or issue closure is claimed.

Two existing helpers now declare object inputs and precise dictionary/list results;
malformed runtime inputs are still checked by the same predicates. A local result
annotation and standard import ordering/formatting complete the change. Independent
review compared normalized ASTs after removing annotations/import ordering and
confirmed behavior is equivalent. The format-sensitive guard-removal mutation
anchor was updated, and the actual mutant still fails its independent oracle.

The monitoring continuity gate now requires the exact current privacy-harness hash
`4ccd22cb3bd80727cafd7256963480a6aaa806f3d8dfddeca57e2e76c999efa5` with the original
historical `1207ceac...` identity. Unknown rehashed current sources and rewriting
historical receipt identity remain denied. The previous `83a478f4...` intermediate
is documented, not admitted as current. Both historical receipts remain byte-identical.
The checker still reports current runtime qualification `not_run`.

Fresh Python 3.12.14 Linux x86_64 results:

- 11 telemetry plus 29 monitoring tests passed normally and optimized, also
  independently repeated by the reviewer, including the actual deliberate mutation.
- Static quality passed: zero new diagnostics, 775 observed historical diagnostics;
  the baseline file is unchanged. This is ratchet success, not zero total debt.
- Explicit boundary suite: 445 tests, 424 passed, 21 skipped. Temporal not run.
- Monitoring checker, scaffold and whitespace checks passed.

Required aggregate is not green. Required prepared Temporal is unavailable and
maintained mutations require Linux ARM64; this host is x86_64. A preliminary run
also reported source instability because the audit prose was edited during its run;
that receipt is invalid for exact-source claims. The subsequent frozen-source run
reported only the Temporal and ARM64 failures, with no source-instability failure.
No runner, service, Docker, Collector or live telemetry ran.
