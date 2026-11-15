# Partial offline demo inputs

`fleet/demo-browser-inputs.json` pins genuine upstream Playwright 1.56.1 NPM tarballs and the browser archives selected by its `browsers.json`: Chromium/headless-shell 1194 and Playwright recording ffmpeg 1011. Pins were recorded from downloaded bytes, with NPM SHA512 checked against the demo suite's committed lock. The manifest records exact demo source/lock references and upstream/redirect URLs. The separate system ffmpeg/ffprobe used for presentation rendering is not supplied by the Playwright ffmpeg archive.

Run the read-only input auditor with:

```sh
python3 -m tools.runner_fleet.demo_inputs --downloads /approved/staged/demo-inputs
```

It checks exact inventory, hashes/sizes, NPM package identity/SHA512/notices, embedded browser revisions, archive paths, and the Playwright ffmpeg LGPL notice. It never extracts or executes packages, installs anything, changes an image validator, or enrolls the demo. Synthetic negative tests exercise changed bytes, symlink input, path traversal, missing notices, changed architecture/inventory, integrity mismatch and embedded browser revision mismatch. Passing this audit always returns `qualification: blocked`, `activation_allowed: false` and explicit false assertions for Chromium notice closure, Noble system dependencies and guest qualification. This is input preparation only.

The Chromium archives have no standalone license/notice files in their inventories. Preserve their bytes and resolve the matching Chromium and bundled-third-party notice obligations before redistribution in a guest image. NPM notices and Playwright ffmpeg's COPYING.LGPLv2.1 do not establish Chromium notice closure.

The existing fleet egress policy permits Ubuntu archive/security, Node and GitHub endpoints, but not registry.npmjs.org, cdn.playwright.dev or its observed playwright.download.prss.microsoft.com redirect. No egress policy changes accompany this preparation. The current demo workflow's npm/browser download steps will remain blocked in the fleet until an owner-reviewed offline staging/installation mechanism exists, or the owner separately approves a network change. Host retrieval of input archives is not proof of guest egress permission.

The system closure still needs ffmpeg, ffprobe, gh and their full Ubuntu Noble amd64 dependencies, plus the native libraries and fonts specified by the pinned Playwright package. Resolve against signed Noble metadata in an appropriately isolated, reviewed environment; record the exact resolver image, keyring, InRelease/Packages proofs, package versions, complete dependency graph, package hashes and copyright inventories. Do not reuse Kali-host ffmpeg binaries or a Debian Rust builder as Noble guest qualification.

A bounded read-only, non-root, capability-free Docker download-only resolver attempt encountered an inherited apt-clean hook's `Permission denied` on its read-only /var/cache/apt path. It was stopped, with logs, partial outputs and container/volume retained. Do not retry with altered hooks, privileges, paths or routes as a fallback. An owner-reviewed resolution and applicable authorization must precede any new attempt. Its partial package downloads are not a qualified closure.

Once independently audited system inputs and notices exist, review an offline guest installation recipe separately. The recipe must verify every staged archive before installation, run no installation-time network retrieval, preserve browser sandboxing and notices, bind exact source/controller/recipe/tool hashes, and require fresh sandboxed browser/media/gh checks and ephemeral teardown in the qualified disposable guest. No recipe here is wired into image construction or deployment. Image capacity and provenance gates, required remote CI and owner-only persistent demo activation remain separate.
