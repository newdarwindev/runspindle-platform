# Offline guest tool probe

The explicit `tools.runner_fleet.offline_probe` command boots one pinned, sealed
QCOW2 image without a network device and observes **Cargo 1.94.1, Rust 1.94.1 and
Ruff 0.14.5** inside the guest. It does not dispatch GitHub jobs, register a runner,
use credentials, modify firewall or services, contact providers, install software,
change the selected/rollback image, or qualify CI/production.

## Invocation and limits

Provide the absolute existing image path, its independently verified SHA256, and
an existing user-owned `0700` evidence directory on sufficiently large storage:

```sh
python3 -m tools.runner_fleet.offline_probe \
  --image /absolute/sealed-candidate.qcow2 \
  --sha256 EXACT_64_CHARACTER_SHA256 \
  --evidence-parent /absolute/private-evidence-directory
```

The image must be a regular root-owned read-only file, without symbolic path
components, backing files, external data files, snapshots, dirty/corrupt metadata,
or a size above 32 GiB. Its digest is checked before and after a successful boot.
KVM access must already exist. The probe never changes permissions to obtain it.

Admission requires 34 GiB free disk (30 GiB host reserve plus 4 GiB for bounded
new files) and 11 GiB available host memory. Six child-produced files, including
the overlay, seed and complete command/serial captures, each have a kernel-enforced
512 MiB size ceiling. Metadata is small and fixed. A full file stops the command;
it is never truncated, rotated or removed. Child processes have a 4 GiB virtual
address-space cap, 240 CPU-second cap, and disabled core dumps. The VM uses two
vCPUs and 1 GiB guest RAM. These limits may prevent an image from booting; failure
is evidence, not permission to silently enlarge the limits.

A separate GNU `timeout` supervisor bounds each command (60 seconds; guest boot
240 seconds) and kills its process group after another five seconds if needed.
It remains alive if the Python parent is killed. The evidence-directory lock is
inherited by the supervisor, so another probe using that parent cannot overlap an
orphaned command. SIGTERM/interrupt in the normal CLI kills and reaps the group.
A forcibly killed Python parent can leave the bounded supervisor running until
its deadline and may prevent writing the final receipt. Inspect retained logs;
never infer success from a missing receipt. This is not a machine-wide scheduler:
other evidence directories and unrelated host writers are outside its lock.

The probe has a ten-minute overall cooperative deadline, including hashing and
parsing. External commands retain their independent deadline if the parent dies.
Images, logs and all inputs are preserved in a unique directory on every outcome.
The small overlay depends on the original sealed image; retain both. A new
invocation creates new evidence and never retries or overwrites an earlier run.

## Evidence boundary

`status: passed` means this exact image booted offline and the three fixed version
commands succeeded with the expected versions. `ci_qualified`, `network_tested`
and `promoted` always remain false. This does not demonstrate repository tests,
runner assignment, egress isolation, a current Cargo dependency closure, live
provider access, native sessions, or fleet lifecycle cleanup.

The synthetic test module covers mismatch/malformed input, command failure,
parent death, inherited locks, kernel file-size bounds, output retention and
no-network QEMU arguments. Actual target execution must be recorded separately.

## Target observation: 2026-11-17

On Strix, the independently hashed candidate
`a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0`
passed read-only `qemu-img check` with zero check errors and booted without a NIC.
The first attempt with a 2 GiB guest stopped during boot with QEMU thread creation
failure. The second, with a 1 GiB guest and unchanged hard host limits, returned:

| Tool | Guest observation |
| --- | --- |
| Cargo | `cargo 1.94.1 (29ea6fb6a 2026-03-24)` |
| Rust | `rustc 1.94.1 (e408947bf 2026-03-25)` |
| Ruff | `ruff 0.14.5` |

All three version commands returned zero. QEMU then exited with signal 6 during
shutdown and reported `qemu_thread_create: Resource temporarily unavailable`.
Both complete attempts remain retained privately. Both probe receipts correctly
report **failed**. Address-space pressure is a hypothesis, not a proven diagnosis.
No clean lifecycle, CI qualification, image promotion or network acceptance is
claimed. The base digest remained unchanged after both attempts. Neither guest
remained running. Existing fleet drain/selection and registrations were untouched.

Sixteen focused synthetic tests, including independent parent-death and lock
checks, passed. The full repository suite passed 463 tests in normal and optimized
modes, with 13 explicit optional/target skips. Pinned strict checks found no new
adapter diagnostics; the aggregate still reports the existing formatting finding
in unchanged `tools/check_shared_fixtures.py`. Tests do not erase target failures.
