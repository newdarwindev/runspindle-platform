# Disconnected clean Git source observation

`tools.runner_fleet.git_observer.observe_checkout` feeds the existing
`environment_applicability.Observation` comparison primitive. It does **not**
modify automatic admission, catalog records, image qualification, workflows,
runner registration, deployment or host settings.

The caller supplies a canonical absolute checkout, exact 40-hex commit and
operator-reviewed repository, environment and external closure identities.
The repository label is an operator assertion, not remote authentication.
Environment compatibility and the entire transitive setup/external dependency
closure still require operator review; this observer does not infer them.

The observer validates commit, tree and blob Git object hashes and inventories
all tracked regular files, including modes and SHA256 byte digests. It checks
HEAD, index, and two complete raw filesystem inventories. Extra files, including
ignored files and executable inputs, fail closed. Symlinks, gitlinks/submodules,
nonregular files, missing files, dirty bytes/modes and staged changes fail closed.
Directory presence can affect setup behavior and namespace-package discovery.
Both raw scans require exactly the directory paths in the committed tree; even
extra empty or ignored directories are rejected.
This is deliberately unsuitable for dirty build worktrees or cache-filled trees.

A trusted installed `/usr/bin/git` and an exclusively owned, quiescent local
checkout are prerequisites. Rechecks are **not atomic filesystem attestation**
and do not defeat a same-UID adversary or coordinated ABA mutation. No locking
or later assignment guarantee is provided; independently observe assigned source
and retain the coordinator's authority/identity gates.

Git runs with bounded stdout and stderr, a shared 30-second deadline, disabled
replacement objects, lazy fetch, optional locks, fsmonitor, hooks and all remote
protocols; no worktree commands, filters, package installation or network calls
are requested. Inherited Git environment variables/global/system configuration
are excluded. Local object storage must already contain the complete closure.
Filesystem traversal, metadata, individual blobs and aggregate bytes are bounded.
Checks are read-only, not a claim that arbitrary filesystem reads cannot block
on defective storage. No Git or filesystem state is repaired.

Tests use tiny disposable synthetic repositories. Passing them is component
verification, not CI, image, live workload or production qualification.
