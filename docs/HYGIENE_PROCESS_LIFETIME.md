# Bounded Platform hygiene output

This source-only change addresses the hygiene wrapper's process-lifetime and
output-close risks. It follows the bounded-resource and unknown-outcome contracts
in [desired intent](../specs/desired-intent.md), and adapts the design reviewed in
[root PR66](https://github.com/newdarwindev/runspindle/pull/66) at
`641f8d16219d33827b9f5177025eb0385ad2ef53`. Platform's
[explicit Temporal selection](TEMPORAL_LANE.md) remains a separate evidence gate.
No incident, PID-reuse observation, operational activation or live qualification
is claimed. Existing source rights and [provenance](../provenance.json) remain
unchanged; this is owner-directed adaptation within Runspindle, not a blanket
license or an import of legacy source/history.

## Ownership and finite cleanup

The wrapper is the sole waiter for its child and requires default SIGCHLD handling.
It checks that prerequisite without installing a handler. Concurrent external
waiters or concurrent signal-policy changes are unsupported; an observed policy
change fails closed. While the leader is unreaped its PID remains reserved. Once
`poll` or `wait` reaps it, that identifier no longer authorizes a group signal.

Output uses an unbuffered, nonblocking pipe, read and closed in the owning thread.
There is no background buffered reader whose lock could block stdout close. Every
loop observes a monotonic deadline, including under continuous output.

- A completed check with EOF receives no cleanup signal.
- Timeout attempts one SIGKILL only for an owned, unreaped group, then waits at
  most two seconds. It does not retry an uncertain stop.
- After leader exit or a timeout stop attempt, output drain is bounded to two
  seconds. Without an earlier failure, a retained writer yields failed
  `output_incomplete` evidence; an earlier timeout remains primary. Cleanup is
  unknown, and no signal is sent to a reaped leader's former group.
- Cancellation preserves the original exception through bounded cleanup and pipe
  closure. A new cancellation during cleanup is not hidden by an already handled
  caller exception.

These bounds concern waits and pipe reads after process creation, not OS creation
or an uninterruptible kernel operation. Group signaling is not descendant
containment. Descendants may escape the group or close their output while still
alive. Neither EOF nor leader reaping proves that all descendants exited.

## Receipts and Temporal evidence

The retained diagnostic tail is limited to 65,536 bytes. Temporal markers are
observed across the whole stream independently of that tail, using a partial line
bounded to 8,192 bytes and a duplicate count saturated at two. An overlong marker
is invalid but still counts toward duplicate rejection.

`output_complete` records observed EOF, not successful process-tree cleanup.
Missing EOF, read/close failure or unavailable ownership cannot yield a passed
receipt. Fixed error codes in `signal_error`, `reap_error`, `output_read_error`
and `output_close_error` exclude exception text and private child output.
Original nonzero exit codes, primary timeout reasons, sanitized diagnostics and
resource/summary fields are preserved.

Temporal coverage additionally requires a passing collector receipt, EOF, zero
exit, the canonical command, a fresh invocation binding, one valid whole-stream
marker and the exact five started/passed test IDs. A complete-looking marker
cannot override timeout, nonzero exit, read/close failure or changed ownership.
The launcher, workflow selection, baseline and opt-in policy are unchanged.

## Bounded verification

The focused suite uses fake processes, an injected clock, and a test-owned retained
pipe writer. It never executes real Temporal test bodies. Its negative cases cover
signal/reap ordering, timeout, missing/denied stop, bounded failed reaping, retained
writers, continuous output, read/close failures, cancellation, changed ownership,
whole-stream duplicates, stale invocation and malformed coverage.

Run the focused suites directly with Temporal/provider opt-ins absent:

```
python3 -B -m unittest discover -s tests -p 'test_hygiene*.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_hygiene*.py' -v
python3 -B -m unittest discover -s tests -p 'test_python_test_selection.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_python_test_selection.py' -v
```

These checks are synthetic source evidence. Aggregate validation, real Temporal,
fleet/guest/service operation, installed acceptance, provider calls and exact-head
remote CI are separate gates and are not established by this change.
