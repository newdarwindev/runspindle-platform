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

## Verify layers and read installed packages

The [layer report](../telemetry/langfuse-image-layer-audit.json) records checks
against **61 distinct registry blobs** covering all **73 ordered layer references**.
Place the selected gzip blobs from the upstream OCI Registry API in a temporary
directory. Name each file by the hexadecimal portion of its compressed SHA-256.
Keep the raw manifest and configuration capture available for the same run.

```sh
python3 -B tools/langfuse_image_layers.py --metadata "$CAPTURE_DIRECTORY" \
  --layers "$LAYER_DIRECTORY" > "$REPORT"
cmp "$REPORT" telemetry/langfuse-image-layer-audit.json
```

The scanner verifies the compressed size and SHA-256 before reading the tar
stream and verifies the entire uncompressed stream against the configured
diff ID. It holds selected metadata in memory and never extracts a filesystem.
Whiteouts and replacements apply in layer order. The 16 selected worker hard
links receive their bytes from earlier regular files in the same verified
layer, with no host path resolution. [OCI layer specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/layer.md)

| Image | Installed OS packages | Distinct physical Node package versions |
| --- | ---: | ---: |
| Langfuse web | 18 | 759 |
| Langfuse worker | 17 | 1,825 |
| PostgreSQL | 147 | 0 |
| Redis | 89 | 0 |
| ClickHouse | 110 | 0 |

OS counts come from `apk` or `dpkg` installed records. Node counts come from
visible package manifests under `node_modules`, including the physical pnpm
store. They measure artifact contents. Runtime imports, bundled code without a
package manifest, license closure, container execution, and Langfuse persistence
and restart readback remain open. MinIO retains its separate source fallback.
