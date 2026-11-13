# runspindle-platform

Dependency composition, local runners, distribution and deploy overlays.

**Status: public scaffold, not a deployed or production-ready implementation.**

This repository is one boundary of [Runspindle](https://github.com/newdarwindev/runspindle). Repository boundaries do not require one microservice each.

- Read [specifications](specs/README.md), [agent guidance](AGENTS.md), [provenance](provenance.json), [status](STATUS.json) and [legal status](LEGAL.md).
- Run `python3 tools/check_scaffold.py` for structural validation only.
- Operational entrypoints and acceptance gates deliberately fail closed until implemented.
- GitHub Actions use isolated **self-hosted local runners only**. No GitHub-hosted fallback. Runner capacity is not established by publishing workflows.
- No credentials, real sessions, messages, datasets, browser profiles or live host inventory belong here.

No old repository is retired by this scaffold. Migration and archival are separate verified gates.

A bounded [disposable Temporal development lane](docs/TEMPORAL_LANE.md) now has pinned preparation, real persistence/restart and cleanup evidence. Other platform adapters and product acceptance remain incomplete.

## Disposable Strix runners

See [readiness tooling and remaining deployment work](docs/RUNNER_FLEET.md).
The QEMU backend, transactional systemd manager and shared push validation entrypoint are implemented. Live rollout is gated on private execution, cleanup and recovery evidence; startup and required checks remain disabled until qualification. Both Mac lanes remain pending.
