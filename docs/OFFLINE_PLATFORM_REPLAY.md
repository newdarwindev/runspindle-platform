# Offline Platform validation replay

The offline image probe optionally accepts `--bundle`, `--bundle-sha256` and
`--source-sha` together. A local Git bundle is limited to 8 MiB, read once through
a no-follow descriptor, SHA256-verified and embedded as frozen bytes in cloud-init.
The guest initializes a new repository, fetches only origin/main from this local
bundle and checks out the exact source commit. Git
system/global configuration and hooks are disabled; no submodules or network
fetches are invoked. The existing QEMU command exposes no NIC or host share.

This replays the two command steps of `.github/workflows/platform-ci.yml` whose
SHA256 is `c41f2e2057821d6d3b76067ef3e76ed5eaa7f64029c1ecf72ed9ab2ddedc7b64`:

1. `python3 tools/check_scaffold.py`
2. `python3 tools/validate.py`

It does not emulate actions/checkout or the separate partitioned `validation.yml`.
The guest verifies workflow bytes before running these commands. It runs as the
cloud-init root user, not the normal runner account; quality tools and Rust are
prepended to PATH. No GitHub identity, check result or runner metadata is invented.

Replay retains 1 GiB guest RAM, a 4 GiB host address-space cap, a 240-second
guest supervisor with five-second kill grace, no core dumps and an 11 GiB
available-host-memory requirement. The revised per-writer disk limits and
39 GiB storage admission are specified below; tooling-only mode is unchanged.
Validation may exceed these bounds or fail because a required environment is
missing. Such outcomes stay failed; Temporal, architecture and mutation gates
are not downgraded. Complete guest output, seed and overlay remain retained.
A passing replay is isolated validation, not live CI/network/production
qualification and never promotes the image.

## First bounded experiment

A real no-NIC guest used source `9b20cd6a1e826e8e0e7df743423458f75229c8ba`,
bundle SHA256 `6ea07c119f47e5fcefa16478f3b146d43ca408546ef2fcb53752d1a1b7869b3b`,
and image SHA256 `a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0`.
Local source import and checkout succeeded, and the scaffold command printed a
passing receipt. Full validation did not complete: the overlay reached exactly
512 MiB, guest disk writes produced I/O errors, and the independent supervisor
terminated the guest at 240 seconds (exit 124). No successful replay marker was
produced. The base image digest remained unchanged afterward; no guest remained.

All experiment evidence was retained privately. This failure demonstrates the
current tool-probe disk allowance is insufficient for this validation workload;
it does not identify which validation phase generated the writes. Do not claim
that the complete validation passed, relax gates, or promote the image from this
experiment. A differently budgeted replay requires separate engineering review.

## Conservative replay writer budget

The tooling-only mode keeps its original 512 MiB file cap and 34 GiB admission.
Replay mode now uses an explicit, separately tested allocation:

| Host writer file | Hard maximum |
| --- | ---: |
| QEMU overlay | 2 GiB |
| QEMU combined console/stderr log | 2 GiB |
| Dedicated replay output log | 2 GiB |
| Image-info, creation and seed logs | 512 MiB each |
| Seed ISO | 512 MiB |
| Frozen inputs and result metadata | 16 MiB budget (bounded inputs) |

The maximum accounted allocation is 8 GiB + 16 MiB. Admission requires 39 GiB
free, preserving a 30 GiB reserve plus filesystem/accounting headroom. QEMU's
kernel RLIMIT_FSIZE applies independently to all three of its writable files; it cannot
write to the sealed base or read-only seed. The other three child commands retain
the original cap. The seed builder has both its output ISO and output log counted.
The guest remains at 1 GiB RAM, 4 GiB host address-space limit and 240-second
lifetime. This is a budget correction, not a successful validation result.

The reviewed-budget retry also failed, but did not exhaust disk allowance:
its overlay ended at 862,846,976 bytes and the host console log at 97,533 bytes.
Source import and scaffold again succeeded. At guest uptime approximately
207 seconds the replay script could not write its final marker to its existing
`/dev/ttyS0` descriptor (EIO); no valid validation receipt reached the host.
The supervisor stopped the guest at 240 seconds. The base image SHA256 remained
unchanged and no guest remained. This is an output-transport failure with unknown
full-validation outcome, not a passing run or evidence of disk-budget exhaustion.
Console-login hangup is a hypothesis requiring verification; a dedicated,
separately budgeted non-network output channel would avoid sharing login output.


## Dedicated non-network result transport

Replay mode now adds a virtio-serial port named `org.runspindle.replay`. Guest
commands and the final receipt write to that port, not the login console. The
host appends to a newly, exclusively created private `replay.log`; no host file
sharing, socket, NIC or listener is added. Console diagnostics remain separately
retained in `guest.log`. Both output files and the overlay have explicit 2 GiB
kernel caps; the revised admission covers all three plus helper outputs. Parsing
uses only the dedicated output and still rejects missing, duplicate, stale or
failed receipts. This change does not retroactively validate either failed run.
