# CI image candidate, 2026-11-14

Evidence class: disposable image preparation and isolated guest probes. This is not fleet rollout, GitHub CI qualification, integrated product acceptance, or production activation.

- Candidate image: `sha256:a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0` at `/var/lib/runspindle-fleet/images/candidate-45811b5b28f5.qcow2` on Strix, root owned and mode 0444. The recorded digest matched the sealed file.
- Exact image preparation source: `45811b5b28f5e2777d3caec22f3af2a423520dba`; controller digest `0fef20a99b8a7c21c9f0cf6d3ebfcd87bfe76d481d167db83c23ef3322ac89d0`; recipe digest `0fa21327ff73e2aade11eceea18dfc5bd54d9a14f78a5d4a50c74316081555d4`; tool closure digest `809d24f076692a9ae1309dcae2386689fdaa83b1bf495bf996c9077f438783ea`.
- The staged x64 builder archive digest is `9e872bae015df2787ef50be0a1fcd5d96b0f3f2a2f5b11414ef388f1dd61527c`, with Docker loaded image ID `sha256:9045681d1e2443e55bffe0c86dd93ee262157ea807fbf55c4e58e82699c9a53e`. Builder tool, package, and license pins are in `fleet/builder-closure.json`.
- Preparation emitted a private package/license manifest. A separate fresh guest passed boot, network and mount isolation, exact `/etc/runspindle-instance.json` image/source/controller/recipe/closure bindings, quality tool versions, platform tests, and offline locked core and host Rust tests. Private receipt: `~/.local/state/runspindle-fleet/candidate-attested-probe.json` on Strix. Private build and test logs remain in the recorded resource directories.
- Local platform unit suite: 76 tests, 13 skipped; scaffold and Ruff checks passed. The unexecuted tests require prepared external dependencies; no skipped test is counted as acceptance.
- Earlier attempts exposed a Podman versus Docker loaded-image ID difference and a sealed-image path inaccessible to QEMU. Both were fixed and retested. Their private manifests and logs remain; the unqualified provisional image files were removed after digest verification to preserve host disk capacity.
- The existing qualified image and rollback pair remain unchanged. After candidate probing, the existing manager is restarted with its startup policy retained and `drained=true`, with no active reservations.

Remaining: independent review of the final PR head; full fleet preflight, cancellation and recovery qualification; exact-image GitHub CI on all eight repositories; dependent PR merges and composition pin updates. No candidate promotion or manager resume has occurred.
