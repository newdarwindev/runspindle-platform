# Pinned Langfuse image metadata

The [dependency pins](../telemetry/langfuse-qualification.json) select five OCI
images for Langfuse web, worker, PostgreSQL, Redis and ClickHouse. MinIO uses the
separate pinned source fallback. The [inventory](../telemetry/langfuse-image-inventory.json)
records the configuration digests and 73 ordered layer descriptors for the five
images. Every raw manifest matches its declared SHA-256, every raw configuration
matches its manifest descriptor, and each configuration identifies `linux/arm64`.
Each compressed layer descriptor is paired with its uncompressed digest from the
configuration. [OCI manifest specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md) · [OCI configuration specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md)

## Reproduce

For each pinned image reference, capture the raw manifest from the upstream
registry into a private temporary directory as `<role>.manifest.json`. Capture
the raw blob named by its `config.digest` into `<role>.config.json`. The role names
are `web`, `worker`, `postgres`, `redis`, and `clickhouse`. The observed capture used
anonymous HTTPS requests to Docker Hub's Registry API, fetching only these two
documents by digest. Use the exact bytes returned by the registry.

```sh
python3 -B tools/langfuse_image_inventory.py --metadata "$CAPTURE_DIRECTORY" > "$REPORT"
cmp "$REPORT" telemetry/langfuse-image-inventory.json
```

The verifier reads capped regular files and emits only pinned references,
sizes and digests. Its output excludes configuration labels, environment and
history. Missing, malformed, substituted or wrong-target metadata exits 78.

This is read-only registry evidence. Layer contents, package inventories,
container execution, and Langfuse persistence and restart readback remain open.
