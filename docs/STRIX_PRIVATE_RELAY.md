# Private Strix validation relay

The Host pilot has a public reporting workflow and a private execution workflow.
Generate and review both from the Platform checkout.

```sh
python3 -B -m tools.render_strix_workflow public
python3 -B -m tools.render_strix_workflow private-host
```

The public workflow goes at `.github/workflows/validation.yml` in Host and
retains `validation / strix-linux-x64`. Its only repository setting is
`RUNSPINDLE_STRIX_APP_ID`, the numeric ID of the result publisher.
The private workflow goes at `.github/workflows/strix-validation.yml` in the
approved private execution repository. Its first revision supports Host and
the seven partitions in Host's existing validation manifest.

Configure the relay with the repository and workflow IDs, accepted public
reporter blob, private default-branch commit, whose GitHub identity dispatches
the private workflow, and the publisher App ID. Use the existing fleet state
and canonical lock. Supply fresh role-scoped credentials to the API adapters.
Public reads require Actions and contents read. Dispatch needs Actions write
in the private repository. The result publisher needs checks write in the
public repository. Runner enrollment and teardown use the separate
private-repository administration authority.

For each discovered public attempt, dispatch once and verify the resulting
private run. Admit its exact queued jobs through the disposable-VM supervisor.
Publish after the private workflow reaches a terminal state and the supervisor
has durably reconciled its assigned runners and VM processes. The check
publisher writes a separate result keyed by originating repository ID, run ID,
attempt and commit. The Actions reporter accepts only the configured App's
terminal result for that key. A cancelled or missing partition remains red.

The dispatch and publication intents survive lost responses. On restart, use
the retained request key, workflow inventory and Checks API to recover the
existing operation. If the lookup yields zero or multiple matches, leave the
request unresolved for an operator. Use the canonical supervisor to reconcile
any runner and VM tied to the request before releasing capacity.

Source verification covers synthetic GitHub responses, durable intent,
reporter rendering and shell syntax. Measure the Host workload and queue
before treating the reporter's 145-minute deadline as production capacity.

## Direct registration

The rollout also supports direct runner registration for the private canary and
the eight public Runspindle repositories. The owner removed the SSH replay
evidence prerequisite. Use the canonical fleet lock and the existing isolation,
capacity, and cleanup checks. A normal public validation push selects a
dedicated label for its run, attempt, and job. The registrar configures an
ephemeral runner with only that label, then reads the exact offline
registration back from GitHub before the supervisor releases the listener.

The registration adapter consumes a short-lived repository token through the
existing guest SSH stdin channel. Shell output from configuration is withheld
because this step handles credential material. The startup path opens its log
with noclobber enabled. Use the fleet ledger to record the runner ID and reconcile
an interrupted registration before allocating the slot again. The installed
manager still needs to use this adapter in place of its generic-label registrar.
