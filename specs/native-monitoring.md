# Native monitoring: compatibility and acceptance

Status: original concept-level adaptation of SRC-011, with every selected source
section accounted for. This is a document lift and offline evidence contract,
not client enrollment, operational configuration, or live acceptance. Original
source wording and private linked runbooks are excluded pending rights clearance.
The [lift manifest](../telemetry/native-monitoring-lift.json) distinguishes these
states and binds the artifacts. Exact original identity stays in private provenance.

## Scope and ownership

Platform owns reviewed distribution/configuration proposals. Host owns exact
installed-client and session identity; Core owns job identity; Console shows
observations and their freshness. Tether may relay separately authorized status.
None of these responsibilities turns observation into permission to act.

Use native supported exporters and standard Collector processors. Do not add a
custom receiver, transcript reconstruction, uploader, relay, queue implementation
or wrapper to compensate for unsupported behavior. Existing standard Collector
queue tests remain separate from native-client delivery guarantees. Preserve
legacy material, existing sessions, queues, credentials and databases unchanged.
No service, scheduled task, plugin, credential, endpoint, security setting or
provider configuration is activated by this document.

## Compatibility matrix

The versioned [machine-readable matrix](../telemetry/native-monitoring-compatibility.json)
records source evidence separately from runtime support. **No installed client
is qualified.** A reviewed public source pin is not evidence about an installed
binary, downstream package or hosted service.

| Surface / route | Pinned review | Current Runspindle disposition |
| --- | --- | --- |
| Codex CLI | 0.143.0, revision `c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda`; schema and exec initialization | Source settings recognized; installed identity, effective policy, export and shutdown untested |
| Codex app-server | Same source pin; separate initialization and shutdown paths | Source support only; not transferable from CLI evidence |
| IDE / desktop packaging | No package/build pin | Unknown; bundled app-server and policy must be verified separately |
| Hosted sessions | No accessible supported deployment contract/pin | Unknown and unavailable in this lane; local settings do not establish hosted coverage |
| Standard Collector trace projection | contrib 0.163.2; existing source/image/SBOM pins | Prior synthetic file-export and durable-queue observations only; unchanged scripts, not rerun here |
| Native logs to traces | No reviewed converter configuration | Unqualified; raw OTLP logs are not traces; no log pipeline added |
| Bounded message capture | No qualified producer/projection pair | Unavailable; requires separate content consent and complete envelope qualification |
| Langfuse backend | Existing 3.63.0 candidate, revision `a136ac8184b876a90afafe8cd2ebfef8a7b05858` | Blocked dependency/notice closure and API readback; current v4 guidance is not a 3.63 compatibility result |
| Tokscale usage | No selected executable pin | Separate accounting candidate; no local reads, submit, OAuth or autosubmit run |
| Homarr navigation | No selected executable pin | Candidate links/bookmarks/iFrame only; no custom ingestion widget or authenticated UI claim |
| AgentsView | No selected executable pin | Local reader requires separately authorized roots; prohibited session storage remains prohibited |
| Langfuse Codex plugin | No reviewed plugin commit or dependency closure | Excluded from execution: transcript-based content collection, not native OTLP diagnostics |
| Full conversation coverage | No qualified implementation | Unavailable; no promised completion date or all-session capture |
| Prometheus/Grafana, Loki, Tempo | No selected backend pins | Optional candidates only; protocols, auth, retention and readback unverified |

## Configuration prerequisites

The pinned [Codex schema][schema] recognizes `otel.exporter` for logs,
`otel.trace_exporter` for traces, `otel.metrics_exporter`, `otel.log_user_prompt`,
`otel.environment`, `otel.span_attributes` and `otel.tracestate`.
`otlp-http` requires endpoint and protocol (`json` or `binary`); `otlp-grpc`
requires endpoint. Both have headers and optional TLS settings. TLS keys are
`ca-certificate`, `client-certificate`, and `client-private-key`. These are schema
facts, not recommendations to enable any of them. Metrics have separate defaults;
turning off trace export does not prove all telemetry is off.

Use only supported user/admin configuration after identifying the exact installed
build, configuration precedence and enforced policy. Record a sanitized effective
configuration digest, producer identity, policy revision, target and consent scope.
Do not expose authentication values, private certificate paths or raw configuration
in receipts. Public examples use `<COLLECTOR_ORIGIN>`, `<TRACE_DESTINATION>` and
`<APPROVED_CREDENTIAL_REFERENCE>` rather than usable URLs or credentials. No
activation-ready configuration is included.

The [provider source][provider] has explicit flushing/shutdown logic and batched
log export. That does not guarantee successful drain, replay or delivery. Startup
dotenv loading, host-label propagation and environment-based batch tuning are
**not qualified here**. Verify exact settings and behavior in new approved test
processes; do not restart or kill active sessions. Labels must be bounded,
non-personal identifiers, not hostnames, user names or paths. App-server has its
own [startup/shutdown code][app-server]; independently test its lifecycle. Do not
infer IDE or hosted support from this repository pin.

The existing [Collector profile](../telemetry/collector.yaml) is trace-only and
uses standard OTLP HTTP, memory limiting, filter/transform processors and a file
exporter. It is for disposable synthetic tests, not a live multi-pipeline template.
Any future change needs an exact reviewed config diff, schema validation and
behavioral canary tests; preserve unrelated pipelines and security settings.

[Langfuse's current transport guide][langfuse-otel] describes HTTP JSON/protobuf
traces, project Basic authentication, and a v4 ingestion header. These details
must be matched to the selected backend release; they are not authorization to
send data or evidence that the blocked 3.63.0 candidate implements v4 semantics.
Raw logs/metrics and gRPC are not interchangeable with that trace API. A standard
log-to-trace conversion would need its own versioned projection and privacy review.

## Event and usage semantics

A trace/span describes an instrumented operation. A span event is nested metadata,
not a message archive. An OTLP log is a separate signal with a body and attributes;
converting it to a span does not recover omitted conversation structure or usage.
The [pinned event producer][events] contains request, prompt and tool events and
usage fields. Their existence is not proof that every event is emitted, exported,
retained or suitable for collection on every surface.

For a future authorized numeric diagnostic profile, define a closed allowlist of
bounded counters/durations/statuses. Bind each observation to a synthetic or
approved opaque task/session/event/trace/span identity plus producer, configuration
and policy. Missing or mismatched identity is unknown, not successful correlation.
Never interpret event count, retries, sampling or duplicates as model-call count.

Token input/output, cached-input subsets, reasoning subsets and reported totals
need versioned units and overlap rules. Missing values remain unknown, not zero.
Keep native diagnostic counts, plugin-derived accounting and Tokscale aggregates
as independent ledgers until matching identity/window/deduplication is proven.
Never add overlapping totals. Costs require separately pinned pricing and actual
usage attribution; this lift computes neither cost nor canonical usage.

## Privacy and unresolved gaps

Prompt suppression addresses one field only. Tool names/arguments/results,
status errors, model metadata, resource/scope attributes, schema URLs, span names,
tracestate, event attributes, links, logs and queue bytes can carry content or
identifiers. Test both keys and values across the whole envelope, malformed input,
internal diagnostics and retained data. Do not substitute a redaction label for
verified bytes.

The existing standard profile clears tested fields and drops linked spans. Its
allowlisted identity values are trusted **synthetic inputs**: arbitrary native
identity values are not sanitized or pseudonymized by this evidence. Logs,
metrics, real producer envelopes, allowed-field values, retention/deletion,
auth/TLS failure diagnostics and downstream content remain unqualified. If a
required boundary cannot be met with supported settings and standard processors,
that route stays unavailable.

Keep content collection separate from numeric diagnostics, with explicit scope,
recipient, retention and consent for any future run. [Current vendor plugin
instructions][langfuse-plugin] describe transcript reads and full prompt/tool
content; even satisfying their Node.js 22+/Codex 0.143+ prerequisites would not
qualify privacy, hook trust or access. No plugin or local session reader is run.
Private linked runbooks and readiness documents are unreviewed dependencies,
not imported configuration or implied authority.

## Acceptance procedure and evidence levels

1. **Preparation:** bind exact client package/source, backend, Collector image,
   dependency notices and config digests. Stop on unknown pins/rights. Record the
   authorized surface and synthetic fixture IDs. No live setup is part of this lift.
2. **Configuration:** verify effective settings and enforced policy without changing
   authentication. A parser accepting settings is not a successful export test.
3. **Envelope:** challenge every allowed and disallowed field, malformed requests,
   internal logs and stored queue bytes. Unknown privacy outcomes block qualification.
4. **Transport:** record accepted/rejected/unknown independently from persistence.
   Test TLS/auth rejection in a separately approved isolated lane. Do not log secrets.
5. **Flush:** emit fresh synthetic IDs in a new test process, observe before and after
   graceful shutdown, then independently read exact IDs. A shutdown call alone is not
   proof. Abrupt termination may lose volatile data; distinguish it from graceful drain.
6. **Outage:** with a separately approved backend outage, observe queue/buffer state,
   bounded retry and recovery. Reconcile exact IDs before retrying uncertain effects.
   Queue presence does not imply backend persistence; expose duplicates and loss.
7. **Readback:** match task/session/event/trace/span and config/policy identity in the
   intended backend, after its documented ingestion delay. File-export readback only
   proves the file lane. Backend API readback does not prove authenticated UI behavior,
   retention, restart durability, hosted coverage or end-to-end product acceptance.
8. **Decision:** record each surface and test as passed, failed, unknown, blocked or
   not run with exact artifacts and limits. Only separately authorized live evidence
   can promote an operational claim. No source migration closes a deployment gate.

## Reproducible offline checks

Run from the repository root:

```sh
python3 tools/check_native_monitoring.py
python3 -m unittest discover -s tests -p 'test_native_monitoring*.py' -v
python3 -m unittest discover -s tests -p 'test_telemetry_evidence.py' -v
python3 tools/check_scaffold.py
python3 tools/validate.py
```

The new tests reuse the existing synthetic fixture/readback classifier. Their
flush/outage snapshots are **offline evidence-contract tests**, not executions of
a native client or Collector. They reject transport-only and wrong-ID persistence
claims, privacy canaries, unpinned support claims and artifact drift.

Previously recorded Collector observations live in
[maintained offline evidence](../docs/maintained-offline-tests.json): envelope
projection, malformed-input rejection, normal shutdown, abrupt volatile-buffer
loss and standard durable-queue recovery. The lift hashes and checks their evidence
and unchanged relevant source files, but does not re-execute those services.
The reused classifier matches five fixture IDs; it does not validate native schema
or producer/configuration/policy identity. Those extra checks remain acceptance
requirements, not implementation claims.
`tools/collector_privacy_check.py` and `tools/collector_recovery_check.py` remain the
single isolated integration harness; do not duplicate them with a custom receiver.
Their full execution requires separately authorized prepared immutable images and
disposable network/runtime resources. No TLS/auth denial, native flush, Langfuse
readback, authenticated UI or live deployment result is invented here.

## Source accounting and rights

The [manifest](../telemetry/native-monitoring-lift.json) maps all 19 source units
across the introduction and five source sections to destination anchors. A private
ledger preserves the exact original revision, bytes hash and line ranges. Original
expression is excluded because the selected record does not establish a reusable
license. This original adaptation carries concepts and safe exclusions only;
linked private dependencies are not lifted or silently represented as complete.
Public upstream citations are references, not code or binary imports. Existing
third-party notices remain untouched and no blanket relicense is granted.

Historical source review, adapted document, original offline checker/tests,
prior synthetic runtime observations and unrun live gates are separate manifest
fields. The exact resulting commit/tree and artifact hashes are recorded privately
after commit; the public manifest binds bytes without an impossible self-hash.
Cross-component copies should reference this canonical document and preserve its
unqualified status. Broader SRC-001 migration is unchanged.

[schema]: https://github.com/openai/codex/blob/c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda/codex-rs/core/config.schema.json
[provider]: https://github.com/openai/codex/blob/c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda/codex-rs/otel/src/provider.rs
[app-server]: https://github.com/openai/codex/blob/c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda/codex-rs/app-server/src/lib.rs
[events]: https://github.com/openai/codex/blob/c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda/codex-rs/otel/src/events/session_telemetry.rs
[langfuse-otel]: https://langfuse.com/integrations/native/opentelemetry
[langfuse-plugin]: https://langfuse.com/integrations/developer-tools/codex
