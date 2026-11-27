# Optional fleet observation adapter

`ObservedManager` is an explicit composition-root subclass of the current
`Manager`. Neither installed manager, service entry point, probe hash guard nor
backend is changed. No activation, registration, security settings or admission
policy changes are made. This source adapter is not a qualified fleet image.

It observes tick, cleanup and reconciliation method completion, retaining at
most 128 timestamped records with closed operation/outcome enums. Default Manager
has no telemetry. It never calls an exporter inside lifecycle work. Buffer
capacity or lock contention loses observations without blocking. Failures in
observation do not change the original method result or exception.

`acknowledged` means only the original method returned: cleanup may have retained
an uncertain resource or refused an unregistered probe. It is NOT successful
cleanup, accepted job, verified guest or authority to retry. `error` means an
exception escaped, potentially after effects; cancellation is `unknown`.
Durable state remains authoritative. There are no resource IDs, repo names,
job IDs, image hashes, paths, reason strings, errors, tokens or private content
in observations. A crash loses undrained metadata; tracing is not an audit log.

The caller may explicitly drain and call `export` outside execution. This uses
the actual optional OpenTelemetry API with a caller-supplied tracer and empty
Context (no ambient parent or baggage). Exactly three attributes match
`runspindle.telemetry.v1`: version, component=platform and outcome. Provider
resource/service version is the caller's responsibility. No global provider,
exporter, credentials, background worker or default network is installed.
Caller-supplied exporter can block/fail: a separately authorized bounded
supervisor must own it. This source does not provide that supervisor.

Optional pinned SDK verification: install `requirements-telemetry.txt` in an
isolated development environment, then run
`python -m unittest tests.optional.fleet_otel_sdk`.
The test uses real InMemorySpanExporter, not Collector or a remote service.
The default synthetic manager tests use real SQLite and mocked host effects;
no actual guest/service/API is started. Cross-component propagation, metrics,
Collector readback and fleet closure qualification remain unimplemented.
