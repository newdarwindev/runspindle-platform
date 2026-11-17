# Fail-closed fleet state schema admission

Bounded Platform #15 recovery hardening. This is local synthetic SQLite evidence,
not disposable guest, target-host, runner-capacity or service qualification.

A missing resource table or exclusion index must not silently become a fresh,
empty ledger. `State` now admits only its exact existing current version-zero
layout, including slot/job exclusion indices and Mac observation history. The
schema bytes and version are unchanged. No migration or table repair occurs.
An existing empty/partial database, unexpected version, extra table/trigger/column,
missing index or temporary schema is rejected. Transactions validate before and
after mutation; public state reads validate before returning observations.

Only a caller that exclusively created the database file may initialize an empty
schema. Current complete databases reopen without modifying reservations, phases,
control values or job observations. Unknown/cleaning resources remain occupied.
A simultaneous opener can fail closed while the creator has not yet initialized
its file. A crash after file creation but before schema commit leaves an empty
file that subsequent opens reject. Neither situation authorizes deleting the file,
clearing reservations or allocating guests from guessed free capacity.

On a schema error, stop allocation and reconcile actual guest ownership and
retained state explicitly. Recovery or migration from an older schema needs its
own verified procedure. This patch does not activate managers/services, alter
scheduling, create credentials or operate on a user's existing state directory.

Checks use disposable synthetic directories and no network. They cover complete
existing layout, corrupt/partial/extra schema, late DDL damage, transactional
rollback, fresh-process denial, and a deliberate removal of the admission guard.
Independent review also reopened a database created by actual pre-change source.

This is not authentication of SQLite, row-content validation, coherent-rollback
detection, hostile filesystem protection, or proof against power loss. Existing
filesystem permission behavior is unchanged. Direct consumers bypassing the
`State` methods are not covered by its API checks.
