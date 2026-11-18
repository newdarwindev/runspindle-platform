# Environment qualification is not a job result

Status: **proposed, source-only**. No installed catalog, admission behavior,
workflow, permissions, runner or service changes. The implemented v1 catalog
continues requiring the exact qualified source SHA. This document does not relax
that rule, authorize deployment or claim automatically tested future commits.

## Problem and owner

A passed commit cannot qualify an untested commit. Conversely, requiring a passed
commit before allowing its first CI execution prevents ordinary continuous
integration. Separate these two facts while retaining one fleet admission owner
and its existing durable resource reservation. No second ledger/service is needed.

1. **Immutable environment evidence** establishes that one exact worker image and
   controller/security contract can safely execute the supported workload class.
2. **Compatibility evidence** establishes that one requested commit's declared
   setup/dependency/workflow inputs fit that environment, without running its code
   on the host. It grants no test verdict.
3. **Job outcome evidence** records what GitHub actually assigned and what that
   exact commit/job executed. It does not retroactively authorize the assignment.

## Proposed v2 environment record

A separate explicit schema version would be introduced in the existing root-owned,
sealed catalog. v1 records remain exact-source; no implicit conversion/fallback.
Each v2 record would bind:

- exact image SHA, architecture, immutable backing image identity/lifetime;
- image recipe and build-source SHA; package/SDK/browser/runner/dependency closure
  digests, complete rights/notices and exact installed bytes;
- installed controller/helper hashes, expected guest/host isolation settings,
  network policy identity, credential delivery rules and resource limits;
- required environment probes and their complete source-bound evidence: boot,
  instance attestation, offline tool/dependency operation, sandbox/egress denial,
  bounded execution and independent owned cleanup;
- supported repository and workflow class, compatibility policy digest and
  independently reviewed GitHub authority policy (below);
- revocation/drain state. Any missing, malformed or conflicting evidence blocks.

A Tether container test of 33 upstream tests, a scaffold job, or a successful VM
boot alone does not satisfy the complete environment proof. These remain useful
partial evidence, not reusable automatic-fleet qualification.

## Exact compatibility inputs, not a blanket source exemption

A trusted host-side observer reads Git objects for the exact requested commit.
It never imports the checkout, executes a repository setup script, follows a
working-tree symlink or accepts guest-supplied hashes as evidence. Names, modes,
object kinds, byte sizes and digests are bounded and checked before admission.

A reviewed repository profile declares a closed set of setup/dependency inputs:

- workflow YAML and local reusable workflows/actions, their manifests and all
  setup/validation entrypoint scripts;
- package/workspace manifests and lockfiles, toolchain/config files, build scripts,
  image definitions, qualification and validation manifests;
- explicit absence commitments for optional setup paths (for example Cargo config
  or build.rs when unsupported), and closed directory/glob discovery rules so a new
  workspace, lockfile or local action cannot escape the fingerprint;
- immutable remote action/reusable-workflow commit pins and exact container image
  digests. Floating action tags or unresolved dynamic dependencies cannot be
  called exact compatibility and block this strict profile until corrected.

Every declared input, discovered member and absence commitment must match the
qualified compatibility fingerprint. A mismatch invalidates **applicability**, not
prior evidence: keep the existing immutable image but prepare/review a new profile
or image candidate. Never download missing dependencies or repair a qualified
worker during a job. Unknown/unsupported project layout fails closed.

Only explicitly classified application/test source paths may change without
refreshing compatibility. Those bytes are still untrusted job code confined to
the disposable worker. Application code may fail, request an unavailable module,
or attempt unsupported effects; none grants network, secret, package-install or
host authority. A passing environment never converts such failures into success.
This profile is deliberately conservative and may rebuild too often; optimize its
coverage only with explicit review and mutation tests, not ad hoc exclusions.

## GitHub assignment and credential authority are separate

Labels are routing, **not** a security boundary or exact-job admission. A pre-job
hook runs too late to constrain action preparation and cannot fix this. No claim
that workflow `contents: read` bounds every job a repository runner can receive.

Before registration, independently establish either server-enforced workflow and
credential scope, or an explicitly owner-approved, reviewed repository-level
worst-case authority boundary covering **all eligible jobs**: GITHUB_TOKEN writes,
secrets/environments, OIDC, reusable workflows, artifact/cache writes and private
checkout access. Personal-account repositories may lack the needed server-side
restriction. Unknown authority blocks registration even if image/compatibility
checks pass. VM isolation protects the host, not a GitHub token from its own job.

Use non-default capability labels, one ephemeral runner/job, no host Docker socket,
read-only shared inputs and job-private writable state. Never place the host GitHub
administrative credential in a worker. No broader credential scope is proposed.
Demo persistent enrollment remains owner-performed under the standing custom rule.

## Proposed lifecycle and evidence

1. Under the existing canonical fleet lock: validate drain/catalog/revocation,
   capacity, image seal and compatibility against exact requested source; record
   immutable evidence references in the existing reservation before external work.
2. Verify existing networking only; no automatic repair. Launch one disposable
   worker with sealed inputs, independent lifetime bound and private output.
3. Recheck immutable environment/authority bindings before authorized registration.
4. Observe **actual** assigned repository, run ID/attempt, job ID, head SHA and
   runner ID/name. Requested intent is not proof of scheduler assignment. Record
   mismatch as such; never attribute another job's result to the requested commit.
5. Preserve raw outcome vs proof separately: GitHub conclusion, executed tests,
   required coverage, timeout/unknown effects and cleanup. Missing coverage or
   uncertain cleanup cannot produce a qualified/pass receipt.
6. Stop and deregister independently; retain originals and backing references under
   reviewed no-copy policy. Release capacity only after verified owned cleanup;
   preserve uncertainty and use explicit recovery after interruption.

Compatibility can make future commits **eligible to be tested**, not passed. A
single successful job cannot represent all partitions or all repositories.
Automatic launch remains unimplemented until the coordinator, authority audit,
image catalog, assignment reconciliation and recovery are deployed and exercised.

## Required tests before implementation/adoption

- application-only change with identical closed inputs: eligible, verdict unknown;
- each lock/toolchain/recipe/workflow/setup-script change: ineligible;
- added optional config, nested workspace/lockfile, symlink or floating action:
  ineligible before reservation/registration;
- wrong image/architecture/controller/security policy, revoked or malformed record:
  ineligible; no repair/download fallback;
- workflow token scope cannot constrain a different eligible workflow: registration
  remains blocked without the independent authority approval/boundary;
- wrong actual assignment/run attempt/head, omitted partition or lost API response:
  no inferred success, no replay of uncertain work;
- cache poisoning, shared writable inode, changed sealed input or dependency miss:
  fail closed; immutable cache is input reuse, not reused verdict;
- crash before/after registration, stale source evidence, concurrent reservation,
  stop/remove failure and interrupted original retention: preserve ownership and
  capacity, no blind recovery or base deletion;
- matched cold/warm **complete workflow** trials after reliable execution, counting
  preparation, queueing, materialization, verification and every test/mutant.
  Reduced VM count or input-stage microbenchmarks cannot prove a 50% CI speedup.

No operational change follows from approving this design alone. Implementation,
independent review, exact qualification and required action-time deployment
approval remain separate steps.
