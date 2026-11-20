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
- A builder verifies a staged bundle on the destination filesystem before
  atomically publishing it. One builder can claim a destination, and competing
  builders clean up only their private staging directories.
- Archive staging under the installation lock rejects non-regular, foreign-owned,
  and multiply linked paths. A retry discards a regular partial stage and reuses a
  verified retained archive. A corrupt retained archive is preserved for inspection.

Installed-payload readback now checks the exact file and directory inventory
before hashing any installed file. Expected directories follow from the
manifest's file paths. Missing paths, unexpected paths, links, special nodes,
and size or mode mismatches deny readback. Each accepted path is hashed within
its declared size and compared with its manifest digest. The same check protects
fresh staging, retained upgrades and rollback through their existing callers.

Checksums, canonical manifests, exact component pins, retained generation fencing,
source licenses, normal install/recovery checks and unsupported-product labels
are preserved. Same-size substitution still fails checksum verification.
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
python -m unittest discover -s tests -p test_installed_stream_inventory.py
python -m unittest discover -s tests -p test_portable_install_invariants.py
python -m unittest discover -s tests -p test_atomic_bundle_publication.py
python -O -m unittest discover -s tests -p test_atomic_bundle_publication.py
python tools/check_maintained_mutations.py --suite portable
python -O tools/check_maintained_mutations.py --suite portable
python tools/validate.py
```

Atomic publication evidence was collected against parent commit
`38e608f08ce6d075f589dac0e2f9c08e88a7f6a1` with Python 3.13.18 on macOS ARM64.
All seven publication tests passed in normal and optimized Python, including a
two-process race and failed-copy cleanup. All six portable mutations were
detected in both modes. The overwrite mutation let both child processes report
successful publication and failed the single-winner assertion. The full local
validation command reported missing `yamllint`, missing required Temporal
coverage and the Linux ARM64 installed-mutation platform gate. Native and
installed-service acceptance are pending.
