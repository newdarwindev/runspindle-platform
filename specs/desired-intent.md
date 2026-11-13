# Desired intent: Reproducible operation of maintained workflows

Status: engineering direction and acceptance obligations, not a new implementation
claim, grant of authority, or replacement for existing versioned contracts.

## System intent and evidence boundary

Runspindle is intended to turn an authorized one-off task into a repeatable,
maintained workflow by composing scoped access, managed native/browser sessions,
explicit human takeover, and evaluated workflow candidates. The useful capability
is the composition: cross-application execution can retain identity and evidence
across interruptions, while approved examples can inform later DSPy experiments.
This is a design objective, not evidence of emergent intelligence, autonomous
self-improvement, deployed integration, or measured gains.

A workflow revision must identify its inputs, permitted effects, target bindings,
exception policy, acceptance criteria and resource budget. Repetition does not
create a recurring schedule or expand permission. Human intervention is first an
execution event; only a separately reviewed, consented and sanitized example may
enter a dataset. A successful evaluation is evidence for review, not authority to
promote, serve, spend, publish or act in another account.

## Component responsibility

The platform boundary should make a qualified workflow installation reproducible,
observable and recoverable across explicitly selected targets. It composes pinned
upstream services and component versions; it does not transform a successful
local development lane into production readiness or silently enroll persistent
agents. Maintaining a workflow includes drift detection, owned upgrades, bounded
resources and a tested stop/rollback path.

## Interfaces and acceptance obligations

- Bind release artifacts to composition/component commits, upstream digests,
  toolchains, OS/architecture, license inventory and SBOM. A floating dependency
  or mismatched installed artifact invalidates the claimed release identity.
- Keep runtime secrets, host inventory, account configuration, datasets and real
  traces outside public Git. Grant each adapter only the scoped access it needs;
  telemetry sanitization and retention are explicit deployment concerns.
- Qualify Temporal persistence, worker restart and reconciliation separately from
  browser fencing, host identity, provider usage accounting and delivery. One
  healthy dependency does not prove system acceptance.
- Use disposable, isolated synthetic development lanes. Keep local self-hosted
  runner isolation and no-public-fork execution restrictions. Do not change runner
  security or introduce a cloud fallback to make a check green.
- Persistent schedules/services, credential enrollment, public exposure, paid
  providers, backups and production deployment need their own explicit authority.
  A recurring-workflow design does not itself install a recurring job.
- Before release, test partial startup, incompatible versions, unavailable vault,
  interrupted handoff, exhausted budgets, cancellation, restart and rollback with
  exact configuration and independently observed outcomes.

## Present scope

A bounded disposable Temporal development lane has recorded persistence/restart
and cleanup evidence. The remaining platform adapters and integrated product
acceptance are incomplete. See [distribution](distribution.md) and
[Temporal lane scope](../docs/TEMPORAL_LANE.md); do not generalize that evidence to
production durability or a deployed Runspindle system.
