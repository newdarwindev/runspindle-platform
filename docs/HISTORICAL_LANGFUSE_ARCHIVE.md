# Historical Langfuse archive handoff

This runbook records an owner-authorized, one-time historical migration. It
uses a dedicated private project with an environment per source. The
[native monitoring and content admission contract](../specs/native-monitoring.md) continue to govern live
collection. This archive path is outside that enrollment and qualification.

## Inventory and source choice

Inventory the primary workstation, its mounted volumes and reachable configured
hosts. Record host, directory, byte count, original time range and access
failures in the private receipt. Consult session filenames and indexed dates
for Codex history. Confirm copied export dates from the contents. Hash
independently stored exports before planning import. A ChatGPT HTML export can
represent the same conversations as its companion JSON, and multiple backups can
contain that same JSON. Select one authoritative JSON export. Include explicitly
selected older Codex rollouts, even when their combined size is small.

| Route | Use | Tradeoff |
| --- | --- | --- |
| Original ChatGPT JSON converted to OTLP | Historical sessions with original branch links and structured message data | Requires a source adapter, stable row identity and byte-level readback |
| Langfuse Codex plugin pinned at [0.4.3](https://github.com/langfuse/codex-observability-plugin/tree/e1d02be28e6bacbfe350a1bc1ccf861e54edee17) | Recover Codex turn, generation and tool spans from rollouts | Decompress copies first. The rendered spans omit some source records, so preserve the original compressed bytes in indexed chunks when source removal is required |
| [AgentsView conversation export](https://www.agentsview.io/docs/conversation-export/) | Readable transcript extraction when flattening is acceptable | Export still needs an OTLP adapter. Prefer the original JSON for graph ancestry |
| [Langfuse dataset import](https://langfuse.com/docs/evaluation/get-started/offline) | Curated evaluation examples | Dataset items do not create historical traces or sessions |

## Format and delivery

1. Read the ChatGPT export once. Ensure source conversation IDs are unique.
   Give each conversation one session, one trace and an explicit app-root span.
   Preserve its header and each mapping entry, including the source node
   key, parent and original timestamp. When a timestamp is unavailable,
   use the nearest dated ancestor or conversation start for span ordering.
   Keep the missing source time in the source payload.
2. Derive versioned trace and span IDs from source identity. Record a
   SHA-256 for each serialized payload before sending. Split oversized
   payloads into indexed string chunks, retain the original node keys, and
   nest continuations under their originating span. Send explicit session
   and environment attributes on each span. The v4 root marker is
   `langfuse.internal.is_app_root=true`.
3. Run the pinned Codex plugin on decompressed copies with isolated config,
   media upload disabled and a loopback OTLP receiver. Compare completed
   turns to the captured root spans and check original dates. The staged
   envelope includes private content. If originals will be removed, retain
   the original compressed rollout in indexed base64 chunks under its first
   turn trace. Verify these chunks against the original file hash.
4. Check the intended project and TLS trust. Query for the exact source
   session IDs across projects and reconcile any matches before importing.
   Use bounded batches of OTLP/HTTP JSON at
   `/api/public/otel/v1/traces` with project authentication and
   `x-langfuse-ingestion-version: 4`. Preserve historical span timestamps
   and confirm the target can display the relevant date range.
5. Use a small pilot for each environment. Verify the persisted root marker,
   hierarchy, session and environment, visibility, dates and payload
   checksums. On timeout, rejection or an ambiguous connection close, read
   back the destination and submit only missing span identities after
   reconciliation. A successful HTTP response is provisional, and stable
   IDs do not provide idempotency in v4.
6. Reconcile the complete inventory against persisted rows. Compare physical
   row count, unique trace/span pairs, unique sessions, root counts, privacy
   fields and expected payload digests. Hash large payloads on the storage
   side when available. Confirm representative reads through the Langfuse
   Observations API with explicit endpoints, environments and date bounds.

See the [historical migration cookbook](https://langfuse.com/guides/cookbook/example_data_migration),
[trace update and deduplication behavior](https://langfuse.com/faq/all/tracing-data-updates)
and [data retention and access windows](https://langfuse.com/docs/administration/data-retention).

## Environment inventory and counts

Count across projects with the project ID in each identity. One conversation is
one distinct, populated session ID in its project. One trace is one distinct
trace ID in its project. A trace qualifies as non-empty when at least one row
contains a user message, assistant response, tool call or tool response. Treat
numeric diagnostic metadata and synthetic readiness or service health payloads
as operational data. A session ID may still exist in a diagnostics-only
environment.

Use the v4 tables for this read-only reconciliation. Include legacy sources
separately if the deployment has historical writes there. A lightweight total
query avoids exporting message bodies.

```sql
SELECT
    environment,
    uniqExactIf(tuple(project_id, session_id), notEmpty(session_id)) AS conversations,
    uniqExact(tuple(project_id, trace_id)) AS traces
FROM events_core
WHERE is_deleted = 0
GROUP BY environment
ORDER BY environment
SETTINGS max_threads = 1, max_block_size = 1024;
```

Calculate qualifying IDs in a separate scoped query using the deployed producer
names and payload shape. For the qualified plugin routes the relevant rows are
`TOOL`, `AGENT` or `GENERATION` with recorded content. On the native message
route match its explicit user/assistant event names and require a populated
payload. On the ChatGPT archive route validate the source message node and its
content, rather than counting root headers or archive chunks. Count each
qualifying trace once with `uniqExact(tuple(project_id, trace_id))`. Record the
classification rules with the inventory, as scope names and storage shapes vary
across deployments.

For a scoped v4 readback of physical duplication and persisted payloads use
`events_full`. Compare the payload digest to the exact string emitted by the
adapter. For privacy, return only the digests and identifiers needed for the
receipt.

```sql
SELECT
    trace_id,
    span_id,
    environment,
    session_id,
    is_app_root,
    public,
    lower(hex(SHA256(output))) AS output_sha256
FROM events_full
WHERE project_id = {project_id:String}
  AND environment = {environment:String}
  AND is_deleted = 0
SETTINGS max_threads = 1, max_block_size = 1024;
```

## Source cleanup and evidence boundary

Wait for verified readback and explicit removal scope before deleting anything.
Match source and duplicate files by hash. For mixed backup archives, prepare
replacements that retain attachments and other unimported material, validate
their manifests and restore metadata, then replace the authorized originals.
Prune verified derived chat copies through the archive application's batch
operation and compact the database. Inventory encrypted copies on offline
media separately. An offline copy is a pending cleanup item. Revoke any
temporary key and remove temporary transcript copies after the cutover.

The private execution receipt keeps the exact inventories, installed versions,
trace and payload digests, readback results, cleanup actions and offline
exceptions. This procedure does not qualify the native collection path,
process-crash recovery, independent restore or this component's pinned
Langfuse candidate.
