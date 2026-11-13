# Native monitoring and distribution acceptance

Status: Runspindle design specification. No service installation, source enrollment, live telemetry coverage, restart persistence or production deployment is established here.

## Component boundaries

Platform owns distribution and reviewed monitoring configuration. Host owns native Codex session attachment and exact installed-client identity. Core owns durable job identity. Console displays evidence and freshness, including unknown states; it does not authorize execution. Tether can project approved sanitized status through the existing Slack integration.

Prefer supported native Codex telemetry and standard Collector components. Do not introduce transcript reconstruction services, custom receivers, uploaders or wrappers as a shortcut around missing native support. Unsupported privacy boundaries leave a feature unavailable.

## Data and transport boundaries

Keep diagnostics, bounded content capture and usage accounting separate. Diagnostics should use allowlisted numeric/status metadata and exclude conversation text, sensitive paths, tool arguments, errors and credentials. Content collection requires explicit source and destination authorization and an independently qualified projection. Prompt redaction alone does not sanitize an envelope.

Potential adapters include Langfuse for qualified telemetry, Tokscale for separately scoped usage accounting, and Homarr for links to existing interfaces. AgentsView, if selected, requires independently approved local roots and is not proof of native OTLP coverage. These are candidate integrations rather than installation commitments. Never read prohibited session storage to implement this plan.

Verify endpoint protocols, authentication, required headers and exact supported settings against the installed pinned versions before generating deployable configuration. Older documentation is not proof of current compatibility. Do not copy private backend URLs, project identifiers, authentication values or private runbook links into public distribution assets. Use placeholders and synthetic identities.

## Evidence and readiness

Track CLI, IDE/app-server and hosted surfaces independently. Preserve source version, event/trace/span identity, policy revision and effective configuration digest. Local receiver success is not backend persistence. Distinguish rejection, buffering, replay, duplication and loss. Native export alone does not guarantee durable delivery, anonymization or canonical costs. Never add overlapping accounting totals.

Before a qualified deployment, verify effective configuration, TLS/authentication denial, privacy-canary exclusion, shutdown/flush and backend-unavailable behavior. Obtain backend readback of exact synthetic IDs. Keep backend readback, authenticated UI operation, service presence and restart persistence as separate evidence. Preserve existing active sessions and provider authentication during checks.

## Distribution gates

A distribution manifest must identify pinned component revisions, dependency/license inventory, supported operating environments, configuration schema, migration/rollback procedures and which checks actually ran. Development and production acceptance require separate recorded gates; a stub or unit test is not a deployment. No specification authorizes a persistent job, telemetry enrollment, paid service or production activation.

## Source provenance

Adapted from owner-selected source material identified by the opaque source IDs in `../provenance.json`. Exact original locations, revisions and hashes are retained in a private provenance record. This specification does not import executable dependencies or change upstream licenses.
