# Distribution contract

Status: planned, not deployed.

A release must bind the composition commit, seven exact component commits, upstream source/image digests, toolchain versions, target OS/architecture, license inventory and SBOM. Publish checksums and a reproducible artifact manifest; verify installed bytes against the accepted release. No floating tags or private submodules in the public distribution.

Upstream composition includes Temporal, Codex CLI/app-server, OpenBao, PinchTab, KasmVNC, standard OpenTelemetry Collectors, Langfuse and Python DSPy. Exact versions/digests and version-specific licenses are not yet selected or qualified. Native-desktop Guacamole/noVNC fallback is conditional, not a KasmVNC substitute.

Dev installation must be isolated, synthetic and disposable. Prod requires an exact owner-approved target, private installation values kept outside Git, verified auth/TLS, backups only when explicitly requested, smoke/rollback evidence and operator sign-off. Installation must not silently enroll hosts, create durable agents, transmit data or expose a service.

No install or deploy command is offered as working until these gates are implemented.

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
