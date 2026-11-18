# One unregistered guest attestation probe

This is a bounded diagnostic, not image promotion, CI acceptance or a queue
runner. Default invocation hashes the sealed image and compares its original
manifest against the selected repository's image pins. It does not open the
capacity database or change services/network settings.

Execution is gated separately. An owner must explicitly approve both the host
security/network changes and the retained disposable-disk snapshot before
`--execute` is used. A CLI flag is not proof of that approval.

## Exact scope requiring approval

The existing network helper configures both dedicated `rsvm0`/`rsvm1` TAPs and
`10.231.0.1/30`, `10.231.1.1/30`; replaces the `inet runspindle_fleet` nftables
table with its existing deny-forwarding/IPv6/source rules and existing proxy
port3128 allowance; creates the canary namespace/veth/198.18.231.0/30 route if
absent; and starts the two existing restricted proxies and two canary services.
It does not add allowed domains, publish a guest port or bridge an unrelated
interface. These baseline network settings/services remain after the guest
stops: this is not a claim of automatic network rollback.

Only one guest is reserved while the fleet manager is already inactive and the
canonical shared database has no occupied resources. The global candidate
manifest, qualified images and manager activation are untouched. The guest gets
no GitHub runner registration, token, repository job or workload. Observations
read its exact instance bindings, installed Cargo/Rust/Ruff versions and
Rust/builder/quality closure metadata. Dependency metadata is not a substitute
for a complete dependency build or installed-package qualification.

Work is bounded to360 seconds; cleanup state updates total at most30 seconds
and the owned destruction attempt at most180 seconds, and
attempt owned guest destruction even when capacity-database updates fail.
Existing lifecycle cleanup preserves a recoverable disk snapshot plus original
logs/seed/ephemeral key artifacts in private local retention. This retention must
be included explicitly in owner approval, rather than inferred from permission
to run CI. Unknown cleanup never frees a slot or claims success.

## Recovery

SIGTERM/SIGINT unwind through cleanup. The reviewed host helper independently
sets a transient unit maximum of25 minutes plus20 seconds stop timeout and
control-group killing; actual unit properties are checked after creation. If the
wrapper is uncatchably killed, the guest can survive until this host deadline.
This does not guarantee a nine-minute stop or automatically recover capacity.
Uncatchable process/host failure requires
explicit recovery using the recorded resource identity, exact same pinned image
and manifest, and `--execute --recover RESOURCE_ID`. Recovery creates no guest,
never registers a runner, and only handles that reservation. Its image/directory
identity is durable in the shared state. Ordinary manager cleanup refuses these
probe reservations rather than guessing the image/path. The reviewed manager
guard must be installed before execution. The CLI enforces its frozen digest
for both installed/local code, the exact active service command, and the frozen
host-helper digest before opening the capacity database. A service restart must
not silently switch to an older controller.

The normal production entry point rejects alternate/new state directories.
Do not call internal Python functions to evade these checks or confirmation.
Current unit evidence uses actual temporary SQLite state and fake backends.
Passing it does not authorize network changes or assert a live probe ran.
