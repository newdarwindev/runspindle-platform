# Verified input reuse for image preparation

The image builder accepts `--input-cache-root PATH`. The default is
`~/.cache/runspindle/inputs/sha256`, independent of the preparation state directory.
Choose a user-owned cache on the existing dedicated storage volume when the
home filesystem cannot retain the required free-space reserve. This option does
not create a service, change mounts, alter permissions on existing resources, or
enable a runner.

The actual pre-guest path in `build_image.py` calls `cached_staging.prepare_inputs`:

1. Verify pinned closure manifests; restore **only missing** preparation inputs
   from exact SHA-256 entries. Existing staged bytes are checked, not overwritten.
2. Acquire Ubuntu only if neither staging nor cache supplies its pinned bytes.
3. Run the existing complete quality/Rust/builder/archive staging verifier.
4. Import validated inputs for future preparation states. No guest starts before
   these steps and the existing sealed-base/source/capacity gates pass.

The inventory currently comprises 22 files: Ubuntu, runner/Rust/Node/Temporal
archives, source/Cargo/builder archives, eight quality wheels, package manifests
and license/requirements inputs. It is derived from the existing pinned manifests.
This caches **input bytes only**, not successful checks, mutation-test results,
accepted artifacts or mutable compilation targets. Every job still executes its
checks, and existing per-job writable targets remain independent.

Entries are digest-addressed, owner-only-directory, read-only files. Each hit
rehashes bytes. Reflink/COW materialization is attempted; unsupported filesystems
use a verified copy. Materializations use distinct inodes, never hardlink shared
inputs into a writable job. A job-local mutation cannot change another job's
input or the cached entry. Publication is atomic under an advisory cache lock.
Corrupt entries fail closed; the implementation does not silently replace them.

Limits are 8 GiB per input, 16 GiB aggregate cache, 4,096 entries and 30 GiB free
filesystem reserve after either import or materialization. COW accounting is
conservative. There is **no automatic eviction or deletion** of existing cache
entries. Import temporarily needs additional disk space; separate preparation
copies can consume more space on filesystems without reflinks. Reserve/capacity
failures require choosing adequate existing storage or explicit operator action,
not changing limits automatically.

## Verification and measurement

```sh
python3 -m unittest discover -s tests -p test_input_cache.py
python3 -m unittest discover -s tests -p test_cached_staging.py
```

The consumer integration test uses a complete synthetic 22-input pinned closure,
real archive verification and a fresh preparation directory. It observes zero
acquisition calls, all 22 restored files, the full staging verifier invocation,
and rejection of a corrupt pre-existing staged archive. Other tests exercise
concurrent imports, read-only/copy isolation, poisoned entries, symlinks/FIFOs,
exact entry limits and both cache/destination disk reserve denial.

[Recorded local benchmark](input-cache-benchmark.json) uses the actual pinned
9.42 MB Node input and disposable directories on adequate local storage. “Cold”
means an empty application cache, not a cold OS page cache; the cold path includes
import plus materialization, while warm reuse only materializes. Full digest
verification remains enabled. This is **not measured CI speedup**: no image build,
network acquisition, guest, registration or workflow was executed. Existing
unchanged staging directories already reuse their archives, so this cache's main
benefit is reuse across fresh preparation states and avoiding repeated acquisition.
