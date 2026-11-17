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

All prior probe bounds remain: 1 GiB guest RAM, 4 GiB host address-space cap,
512 MiB per writer file cap, 240-second guest supervisor with five-second kill
grace, no core dumps, 34 GiB storage admission and 11 GiB available host memory.
Validation may exceed these bounds or fail because a required environment is
missing. Such outcomes stay failed; Temporal, architecture and mutation gates
are not downgraded. Complete guest output, seed and overlay remain retained.
A passing replay is isolated validation, not live CI/network/production
qualification and never promotes the image.
