# Pinned document renderer prerequisite

Status: source-only candidate. No wheel acquisition, installation, image build,
guest qualification, CI dispatch, activation or promotion is evidenced here.
This is a separate document closure, not a quality-gate rollout.

## Exact inputs and interpreter

`requirements-documents.txt` pins ReportLab 4.4.9, Pillow 13.0.0 and
charset-normalizer 3.5.2 with one SHA-256 per selected wheel.
`fleet/document-closure.json` pins official PyPI wheel URLs, sizes, hashes,
metadata sources and required notice members. Its digest is pinned by
`fleet/image-inputs.json`. Existing quality requirements and closure are unchanged.
No optional renderer extras, source distribution builds or floating dependencies
are selected. See [complete notice obligations](../fleet/DOCUMENT_NOTICES.md).

The candidate requires Linux x86_64 CPython **3.12.3**, with the isolated interpreter
`/opt/runspindle-documents/bin/python`. The current source artifact baseline was
rendered with CPython **3.12.14** and the same three package versions. That does not
prove that Python 3.12.3, different wheels, the old image or a new image reproduce
those bytes. The exact 3.12.3 comparison is still **not run**. A mismatch must stop
qualification; do not refresh artifacts or relax the equality test to make it pass.

## Future authorized preparation

No CI-time install or fallback is added. During a separately authorized build:

1. Stage only the pinned wheel bytes and `requirements-documents.txt` outside the
   guest. `runner_fleet.staging.verify` invokes the document verifier before any
   transfer, and preparation invokes staging verification again inside the guest.
2. Verify all filenames, sizes, SHA-256 values, package metadata and required
   notices. Reject missing/extra wheels, symlinks, unsafe ZIP paths and mismatches.
3. After checking the exact interpreter version, create the dedicated environment
   and use offline pip with `--no-index --no-deps --only-binary=:all: --require-hashes`.
   The pre-existing preparation recipe still governs unrelated prerequisites.
4. Compare every immutable installed wheel member against its verified archive,
   including fonts and notices. Record the closure, Python, packages and notice
   hashes in `/opt/runspindle-documents/verified.json` and the final image manifest.
   New recipe/controller/tool-closure identities are unavoidable, not replacements
   for any old image's evidence. Preserve the original candidate evidence.

## Required qualification and authority

Before this prerequisite can unblock root validation, separately authorize and
perform the bounded candidate build, fresh guest tests, exact root regeneration
and full validation, integrity/isolation/resource cleanup checks, and image review.
Run root's mandatory generator test twice against untouched checked-in artifacts
using this exact interpreter; it compares every output byte and preserves canonical
mirrors. Record both commands, source SHAs, dependency/notice identities and results.
A local 3.12.14 pass is development evidence only.

Manager draining/stopping, staged package acquisition, temporary guests/keys and
runner registration, any required network/service actions, and later restoration
or image promotion require the applicable separate approval. The existing candidate
campaign admits the configured repository queue, not a promise to run only root
PR 53. Do not run it under source-only permission. This patch neither authorizes
that campaign nor silently activates current Platform main's other changes.

[Local source validation record](document-renderer-validation.json) separates
existing-environment checks from all pending runtime qualification.
