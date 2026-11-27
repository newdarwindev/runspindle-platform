# Offline dependency and trace readback rehearsal

From the repository root:

```sh
for case in verified wrong-digest missing-notice ack-only wrong-trace; do
  python3 tests/test_synthetic_trace_readback.py "$case" || exit
done
python3 -m unittest discover -s tests -p 'test_synthetic_trace_readback.py' -v
```

The invented MinIO manifest must match the expected image label, digest and
notice before the fixture writes. After a simulated acknowledgment, a separate
read opens the temporary sink file and expects exactly one matching ID and
payload. The acknowledgment without a file and a mismatched readback are not
durable readback. The fixture does not retry and removes its temporary bytes.

No actual image, package, container, external telemetry, network, storage
service, credentials or production data is used. Local readback does not prove
the real Langfuse dependency chain; that still requires pinned and authorized
dependency evidence and a separately approved downstream trace readback.
