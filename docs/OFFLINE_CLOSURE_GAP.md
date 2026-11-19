# Read-only offline closure-gap audit

`tools/runner_fleet/closure_gap.py` compares an exact Cargo.lock with a fleet
crate-archive manifest and checks already-local missing archives by their locked
SHA256. It never downloads, extracts, builds, registers a runner, edits a manifest
or changes the fleet. Output includes both input digests, per-archive gaps and
verified byte counts, but no local paths or Git URLs. Unsupported registries/Git
sources remain explicitly uncovered; workspace sources are not audited.

```
python tools/runner_fleet/closure_gap.py --lock /absolute/Cargo.lock \
  --manifest /absolute/rust-closure.json --cache /absolute/cargo/registry/cache
```

Metadata is limited to4MiB, package inventory5000, registry directories16,
individual archive16MiB and aggregate reads512MiB. Canonical paths, non-symlink
regular descriptors, no FIFO blocking, checksum matching and bounded unchanged
reads are required. This is a local trusted-owner diagnostic, not a hostile
same-user filesystem boundary. Malformed inputs reject with sanitized errors.

**Registry coverage is not offline build qualification.** A complete archive list
still lacks proof of Cargo index metadata, Git/source closure, native toolchain,
image/instance identity, actual workload success and scheduler admission.

## Host OTel source gap observed

For Host commit329794eb3e536471746da86104ac84f159edba41,
lockSHA2561d73174df98c69aceeccaf70fd6e71d337ff83acdc7f32b44ad415bb88ba225d:
71 locked registry packages; installed manifest covered55;16 missing archives
were all already local and SHA256-verified, totaling1,268,602bytes. No fetching
or image preparation was needed for that audit. The installed manifest digest
wasd2ddeab3255cd69d082502259083d0a3fa9cf72b53601af90046e4d40cd90eff.
A future reviewed closure refresh must pin its source, index, notices and bytes,
then qualify the exact image. Never allow guest network fallback on a missing pin.

## Scoped CI admission remains a separate blocker

At inspection, owner newdarwindev was a personal User, Host had zero registered
runners and the fleet manager was inactive. Existing Host queued work was older
main source156b63b, not PR30's329794e. No run was dispatched by this audit.
GitHub's workflow-restricted runner groups are organization/enterprise facilities:
[official group access documentation](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access).
No organizational transfer, permission change or new runner enrollment is implied.

Nonce labels are routing, not server-enforced exact-job authorization. A pre-job
hook cannot protect preparation: Runner PrepareActionsAsync occurs before it,
and always() steps may continue. Without a verified restriction or separately
reviewed safe untrusted-job boundary, a live single-job admission claim is blocked.
The approved unregistered diagnostic is not permission to activate a broad queue.
No whole-workflow or50% total-CI performance improvement is claimed here.
