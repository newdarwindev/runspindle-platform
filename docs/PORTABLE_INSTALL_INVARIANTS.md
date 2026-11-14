# Portable synthetic installation invariants

`tests/test_portable_install_invariants.py` adds method-level component evidence
for the distribution intent: journal payload families must not mix, observed
bytes and modes must match a manifest, and readback must name the observed digest.

Fixtures use an in-memory SQLite journal and temporary synthetic data files.
They deliberately construct method receivers without the production constructor.
The attribution fixture supplies a synthetic ARM64 manifest target only while
calling readback. No install/recover/rollback or payload execution occurs. A
separate negative test verifies the production factory still rejects an
unsupported execution target.

The suite includes three subprocess mutation challenges. Disabling the journal
family check, ignoring the file/mode comparison, or fabricating the readback
digest must produce an assertion failure, not an error or skip.

Run with:

    python3 -m unittest discover -s tests -p test_portable_install_invariants.py -v

These tests run through ordinary unittest discovery. They do not change or
replace `check_maintained_mutations.py`, the native ARM64 installation tests,
architecture guards, CI partitions, or image qualification. Native installation
and its mandatory mutation evidence still require a disposable Linux ARM64 lane.
