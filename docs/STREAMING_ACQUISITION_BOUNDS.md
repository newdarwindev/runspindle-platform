# Bounded streaming acquisition (partial Platform #16)

The existing development-bundle format and Linux ARM64 installation gates remain
unchanged. This change closes two concrete portable implementation gaps:

- Build hashing and payload acquisition now open bounded regular-file descriptors
  without following final-component symlinks or blocking on FIFOs. Acquisitions
  copy exactly the recorded size and reject shrinkage or an extra byte. A growing
  input cannot be copied indefinitely before the subsequent digest verification.
  File count and cumulative declared size are checked during build preparation.
- Retained archive acquisition uses the exact encoded header plus declared payload
  length, not unbounded `copyfileobj`. Installed-tree verification rejects extra
  special nodes instead of silently ignoring them as non-files.

Checksums, canonical manifests, exact component pins, retained generation fencing,
source licenses, normal install/recovery checks and unsupported-product labels
are preserved. Same-size substitution still fails checksum verification. Failed
builds remove their incomplete output; source data and notices are not changed.
There is no service activation, fleet operation or target-architecture emulation.

Tests use real disposable files/FIFOs, source mutation between hashing and copy,
actual fresh child processes, byte-output observations and an intentional removed
growth guard. Socket-node testing reports a separate skip when sandbox socket
capability is denied; no socket qualification is claimed from FIFO success.
Existing ARM64 installation tests remain skipped on unsupported hosts.

Bounds concern bytes, not a hard wall-clock deadline for filesystem operations.
Parent directories/filesystem are trusted; hostile ancestor replacement, concurrent
modification of the installation tree, kernel stalls, power loss, arbitrary service
startup and full product installation remain unqualified. Prototype bundle tests
are not production acceptance, and Platform #16 remains open.

Reproduce the portable checks:

```
python -m unittest discover -s tests -p test_streaming_acquisition_bounds.py
python -O -m unittest discover -s tests -p test_streaming_acquisition_bounds.py
python -m unittest discover -s tests -p test_streaming_bundle.py
python -m unittest discover -s tests -p test_portable_install_invariants.py
python tools/validate.py
```
