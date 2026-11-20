# Distribution contract

Status: planned, not deployed.

A release must bind the composition commit, seven exact component commits, upstream source/image digests, toolchain versions, target OS/architecture, license inventory and SBOM. Publish checksums and a reproducible artifact manifest; verify installed bytes against the accepted release. No floating tags or private submodules in the public distribution.

Upstream composition includes Temporal, Codex CLI/app-server, OpenBao, PinchTab, KasmVNC, standard OpenTelemetry Collectors, Langfuse and Python DSPy. Exact versions/digests and version-specific licenses are not yet selected or qualified. Native-desktop Guacamole/noVNC fallback is conditional, not a KasmVNC substitute.

Dev installation must be isolated, synthetic and disposable. Prod requires an exact owner-approved target, private installation values kept outside Git, verified auth/TLS, backups only when explicitly requested, smoke/rollback evidence and operator sign-off. Installation must not silently enroll hosts, create durable agents, transmit data or expose a service.

No install or deploy command is offered as working until these gates are implemented.

## Disposable metadata rollback readback

For Platform #16 and #4, selecting an already verified metadata version now
requires exact installed readback after any interruption. Recovery verifies
the retained manifest's identity, the predecessor location and the installed
file checksums before publishing the version pointer. A missing, substituted or
linked predecessor tree leaves the current pointer and unresolved journal intent
in place. Successful rollback switches the pointer without rewriting verified
files. Recovery of a new installation can still resume staging from retained
bundle bytes.

The regression interrupts rollback after the SQLite intent commits, removes a
predecessor file and reopens the target in a fresh Python process. Independently
read filesystem and SQLite snapshots show the current pointer, pending intent
and missing file are preserved. A valid predecessor keeps its modification time.
Other cases check that a substituted archive and a linked predecessor block the
pointer switch. Removing the verified-version readback reproduces the original
unexpected recovery.

Run `python3 -B -m unittest discover -s tests -p test_metadata_pointer.py`.
The fixture uses actual scratch SQLite and filesystem operations with synthetic
architecture admission. Linux arm64 installation, power-loss recovery and
installed-product services require separate qualification.

Local verification uses base `38e608f08ce6d075f589dac0e2f9c08e88a7f6a1`
and Python 3.13.18 on macOS arm64. Ruff and scaffold passed. Repo-wide static
quality reported the same seven Linux `os.listxattr` diagnostics as the base.
All 14 focused tests passed in normal and optimized Python. Full discovery in
each mode passed 493 of 531 tests, with 13 skips and the same 25 failed cases as
the unchanged macOS baseline.

```text
543af7a28c768047c4332e4fcf8f936177400af69e9c8563dea129d717e37ffb  tools/development_bundle.py
181c9cd4e319fc2c093daf73ac6df00a5b2c8192cde83d73816405ce507855c7  tests/test_metadata_pointer.py
9cbe1331549216b953f2922f1e8321357ca82bb985e1ea595c87fcee8a8fdc84  requirements-quality.txt
```
## Disposable install journal recovery

The metadata and streaming development installers share a version 1 SQLite
journal. On reopen, both compare the retained table and trigger definitions
with the version 1 schema before accepting the journal. A missing retention
trigger, weakened immutability trigger or other schema mismatch blocks recovery.
The installer creates this schema only in an empty unversioned journal.

The metadata regression opens a three-release scratch journal in a fresh
process after removing or weakening its immutability trigger. Both cases
leave the journal contents, active pointer and installed payloads untouched.
The same test reaches the constructor successfully on base
`38e608f` with the damaged journal. Run it with
`python3 -m unittest discover -s tests -p test_metadata_pointer.py`.
The test injects target admission and uses actual SQLite and files. Native
target recovery and installed-service qualification remain separate gates.
