# Bounded candidate queue campaign

Status: code-only candidate. Synthetic tests are not live fleet, cancellation,
recovery, candidate-image acceptance, or rollout qualification.

## Scope

This command observes a temporary candidate fleet serving the existing repository
queue. It does **not** isolate an exact workflow run. The plan must list all eight
configured repositories, in configuration order, and explicitly acknowledge
repository-wide server assignment. Execution requires a second CLI acknowledgment.

Provisioning retains the existing source classifier: same-repository push or
workflow_dispatch on main or ci/*, matching the configured isolated Strix labels.
GitHub can assign another matching job in that repository after registration.
Generic-label/group assignment is not a hard trust or exact-run gate. The command
records the actual claimed run, attempt, workflow, source SHA and job. Watched
attempts are queried through attempt-specific job endpoints even after a rerun.
Recent-created-run discovery catches new jobs between polls; an unwatched old run
rerun entirely between polls can remain unattributed. Missing attribution is
blocked evidence, never inferred from the requested job or counted as success. An observed
untrusted assignment ends admission and triggers cleanup, but detection is after
assignment and cannot undo execution. Do not claim otherwise.

GitHub runner v2.338.2 pre-job hook failure is not used as an authorization gate:
later always()/failure() steps can still execute. There is no hook patch, runner
fork, new credential, permission change, or persistent service in this change.

## Preconditions and execution

A separately authorized operator must drain the existing manager, let its work
finish, reconcile all resources/registrations, and stop it. This command refuses
an active manager, occupied slots, unknown guest state, surviving fleet runners,
or non-drained/error state. It holds the same exclusive reservation lock as the
normal manager. It never clears the durable drained flag, calls reconcile on
foreign resources, changes service settings, or writes qualified/rollback images.

From a reviewed clean platform checkout:

    PYTHONPATH=tools python3 -m runner_fleet.candidate --plan /absolute/private/plan.json

This checks the plan and immutable candidate without running guests. To execute
only after separate approval of all effects:

    PYTHONPATH=tools python3 -m runner_fleet.candidate --plan /absolute/private/plan.json --execute --acknowledge-repository-queue

Required plan fields:
- repositories: the complete owner/name list from fleet/config.json, unchanged.
- acknowledge_repository_queue: true.
- admission_seconds: positive integer, at most 1200.
- cleanup_seconds: positive integer, at most 300.
- image_path: existing sealed candidate-[12 lowercase hex].qcow2 in the fleet
  images directory.
- image_sha256, controller_sha256, recipe_sha256, tool_closure_sha256: exact
  64-character hashes from the reviewed candidate manifest.
- image_source_sha: exact 40-character preparation source revision.
- execution_source_sha and execution_controller_sha256: separately approved
  clean wrapper checkout commit and controller-byte hash. These are not the
  image preparation source or preparation-controller digest. The command checks
  the execution checkout before execution, before each guest and after cleanup.
  Dirty or changed execution source is rejected; no candidate manifest relabeling
  is performed. Receipts keep both identities explicitly separate.

The existing candidate manifest in the shared state directory must match all
bindings and the candidate bytes. The image must be root-owned, non-symlinked and
read-only. The qualified and previous image/manifest pairs are hashed before and
after; a mismatch blocks restoration readiness.

## Bounds, effects and restoration

At most two reservations exist. Admission lasts at most 20 minutes. The normal
15-minute job policy remains; additionally an independent main-process timer
ends the whole campaign no later than 900 seconds after a guest first enters
starting-phase processing (before registration). This conservative deadline may
end a job early. It interrupts slow inventory and registration calls rather than
waiting for a poll. The campaign also has a 35-minute overall execution ceiling.
Cleanup is a separate bounded phase, at most five minutes. Initial and final
read-only verification each have a five-minute budget.

Existing Backend operations may reapply fixed network isolation, start existing
proxy/canary services, boot disposable guests, generate temporary SSH keys and
register ephemeral repository runners using existing authentication. These are
runtime effects requiring approval; source merge alone authorizes none of them.

Cancellation/deadline cleanup targets only this campaign's atomically tagged
reservations. It checks the actual claimed workflow SHA and attempt but never sends remote
workflow cancellation: GitHub's run-scoped API cannot atomically fence successor
attempts. It independently tries guest destruction and deregistration. GitHub
may retain job state until runner loss or workflow timeouts take effect. Failed
or timed-out cleanup retains uncertain capacity; foreign resources are untouched.
SIGKILL, host failure or interruption inside a helper can require later manual
reconciliation. No success is inferred from an uncertain effect.

The command leaves the original manager stopped and durable state drained.
After inspecting the private receipt and verifying idle cleanup and unchanged
images/service settings, the operator separately restarts and resumes the
existing qualified fleet. No automatic restore, promotion, installation or
enablement is performed by this command. A changed image, failed verification or
uncertain lifecycle requires investigation, not an automatic retry.

Receipts are private candidate-queue-[campaign id].json files in the state
directory. Empty campaigns, missing attribution and incomplete cleanup cannot
pass. A successful bounded observation still does not qualify all repositories,
all fault scenarios, or rollout.

## Verification

Synthetic tests cover exact scope acknowledgment, image/source mismatch, default
manager drain behavior, two-slot capacity, atomic ownership, shared-lock refusal,
between-poll claim discovery, actual versus requested identity, untrusted
assignment, conservative independent-deadline arming, cancellation scope and
cleanup uncertainty. Existing component tests remain mandatory. Live behavior,
candidate image integration, service restoration and interrupted-host recovery
remain unexecuted until separately authorized.
