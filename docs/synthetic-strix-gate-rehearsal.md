# Offline Strix evidence rehearsal

From the repository root:

```sh
for case in passed queued-unassigned wrong-label stale-revision incomplete-cleanup missing-check; do
  python3 tests/test_strix_gate_rehearsal.py "$case" || exit
done
python3 -m unittest discover -s tests -p 'test_strix_gate_rehearsal.py' -v
```

All revisions, runner labels, assignments and cleanup claims are invented JSON
records. The fixture closes its gate only for the exact revision, a completed
job assigned to the expected runner label, each named check passing, and
positive evidence for cleaning up the slot, process and registration.
A denial can pass the rehearsal without passing that gate.

No tests read or modify Actions, runner services, live logs, credentials,
network, CI configuration or deployment. To clear the real blocker, verify
the current head/base and required checks from GitHub, actual runner
registration/label and assignment, available capacity, and independent
teardown evidence for the exact run.
