# RUNSPINDLE PLATFORM

## Reproducible execution, inspectable operations

**PLATFORM** is Runspindle's dependency, runner, distribution and observability
boundary. It connects a reproducible development run to the exact code, tools and
observations that produced its result. It does not turn an installed service or a
passing fixture into a claim of production readiness.

**Current status:** implemented bounded tooling and local qualification, not a
deployed or production-qualified fleet. Ordinary text describes implemented work;
*italics identify upcoming work or qualification*.

[![Historical conceptual slide illustrating bounded offline development and explicit acceptance limits](https://newdarwindev.github.io/runspindle_for_bloomberg/gallery/19.webp)](https://newdarwindev.github.io/runspindle_for_bloomberg/assets/Runspindle_Gallery_Index.html#page-19)

Conceptual gallery page 19 is a **14 November 2026 historical slide snapshot**,
not current release evidence. Its depicted commits and test scope are historical.
Use the source receipts linked below for current implementation boundaries.

## Existing foundations, Runspindle responsibilities

**ISOLATED EXECUTION AND RUNNER STATE.** [QEMU](https://www.qemu.org/) supplies the
virtual-machine backend and systemd supplies host service management. PLATFORM
implements the preparation and transactional manager around them. Its
[SQLite schema admission](specs/fleet-state-schema.md) rejects damaged runner state
rather than recreating missing occupancy tables and advertising false free capacity.
*Live rollout, all architecture lanes and native Mac qualification remain gated.*

**DURABLE DEVELOPMENT LANES.** [Temporal](https://temporal.io/) supplies workflow
persistence. PLATFORM's [disposable Temporal lane](docs/TEMPORAL_LANE.md) records
pinned preparation, real restart behavior and owned-resource cleanup. This is
bounded development evidence, not a production Temporal deployment.

**OBSERVABILITY WITH EVIDENCE BOUNDARIES.** [OpenTelemetry](https://opentelemetry.io/)
supplies instrumentation and Collector transport; [Langfuse](https://langfuse.com/)
is the selected model-execution exploration surface. PLATFORM provides
[monitoring contracts](specs/native-monitoring.md), identity/privacy checks and
[audited artifact continuity](docs/native-monitoring-artifact-audit.md). Historical
runtime receipts remain historical after a harness change. *Actual backend readback,
loss accounting and live monitoring need separate current-runtime qualification.*

**MAINTAINED OPERATIONS.** [Distribution contracts](specs/distribution.md) and
[maintained offline work](specs/maintained-offline.md) keep versions and verification
explicit. *A deployed fleet of repeatable workflows is the product goal, not a claim
made by the source tree. New services, credentials and schedules require their own
authorization.*

## Product story and visual references

A run should leave a record. The [Runspindle for Bloomberg source library](https://github.com/newdarwindev/runspindle_for_bloomberg)
illustrates that product thesis with fictional workflows and synthetic values:

- [Bounded offline development, gallery page 19](https://newdarwindev.github.io/runspindle_for_bloomberg/assets/Runspindle_Gallery_Index.html#page-19).
- [Cost per verified completion, page 20](https://newdarwindev.github.io/runspindle_for_bloomberg/assets/Runspindle_Gallery_Index.html#page-20) and [full-cost model](https://github.com/newdarwindev/runspindle_for_bloomberg/tree/main/economics).
- [Product concept slides](https://github.com/newdarwindev/runspindle_for_bloomberg/blob/main/assets/runspindle_bloomberg_product.pdf), [research proposal](https://github.com/newdarwindev/runspindle_for_bloomberg/blob/main/assets/runspindle-paper.pdf) and [complete visual gallery](https://github.com/newdarwindev/runspindle_for_bloomberg/blob/main/assets/Runspindle_Revised_Gallery.pdf).
- [Asset provenance](https://github.com/newdarwindev/runspindle_for_bloomberg/blob/main/ASSETS.md): linked source assets, no new license grant, measured savings, live financial integration or Bloomberg endorsement implied.

## Engineering entry points

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

No fleet deployment or live qualification is claimed.

## Desired technical direction

Reproducible operation of maintained workflows: this component contributes to turning authorized one-off work into
repeatable, maintained cross-application workflows. Scoped access, managed
sessions, explicit human exceptions and evaluated DSPy candidates are intended
to compose into that capability; no automatic improvement or live integration
is implied.

Read [the component-specific technical intent](specs/desired-intent.md) for its
interfaces, privacy boundaries, failure cases and acceptance obligations. Current
implementation and evidence limits above remain unchanged.
