# platform specification

Status: bounded Temporal development lane locally tested; remaining platform implementation and live acceptance not verified.

## Responsibility

Dependency composition, local runners, distribution and deploy overlays.

## Shared identifiers

Correlate task, intent revision, selected host, native session, browser instance, lease epoch, trace/span, artifact digest and Slack destination without placing credentials or raw private content in public receipts.

## Required behavior

Preserve explicit authority and unknown outcomes. Distinguish request admission, execution, validation, acceptance and delivery. Exact state transitions belong in component-specific specs added beside this file.

## Acceptance

1. Unit/contract tests with synthetic data.
2. Isolated integration tests against pinned upstream dependencies.
3. Bounded live acceptance in an authorized target.
4. Failure, restart, uncertain-effect reconciliation and rollback evidence.

A pinned Linux arm64 Temporal development lane now has real readiness, killed-server persistence and cleanup tests. This is bounded development evidence, not the full platform gates. See [lane instructions](../docs/TEMPORAL_LANE.md) and [exact observations](../docs/temporal-lane-evidence.json).

Detailed adaptation: [native-monitoring.md](native-monitoring.md).

## Engineering direction

[Desired intent](desired-intent.md) connects this component to scoped access,
managed sessions, explicit human exceptions and evaluated maintained workflows.
It adds design and verification obligations without changing runtime schemas,
existing acceptance results or operational permissions.
