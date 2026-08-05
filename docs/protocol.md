# Retained Protocol

This document specifies the version-1 local protocol used by `opencode-db`.
It is intentionally a fail-closed retained-evidence protocol, not an OpenCode
database schema specification. The command never discovers targets, starts
OpenCode, inspects processes, or coordinates database users.

## Target and identities

Each command starts with one exact absolute `--database` path. Preview requires
that path to be an existing regular file. Status, abort, resume, rollback, and
`prune-backup` can resolve a recorded target while an incomplete installation
has moved the active main file. Relative paths, `:memory:`, missing preview
targets, paths used as IDs, prefixes, and globs are refused.

The control store is `<database-parent>/.opencode-db/<target-id>/`. The
`target-id` is derived from the canonical absolute target path and is used to
bind every retained record to one target. The catalog maps that target to exact
opaque IDs; IDs are sortable UTC timestamps with random hexadecimal bytes and
are not filesystem paths.

| Identity | Binds | Use |
| --- | --- | --- |
| target | canonical absolute active database path and target ID | command and catalog scope |
| snapshot | source manifest and immutable copied main/WAL/SHM/journal set | recovery and rollback authority |
| candidate | target-scoped clean candidate bytes | explicit installation selection |
| report | candidate, snapshot, validation, preview, and report SHA-256 | completeness decision and uncertain approval |
| install operation | target-scoped durable intent log | status, resume, and rollback selection |

Snapshot and candidate IDs must resolve through the selected target catalog.
Cross-target replay is refused before mutation.

## Source snapshots and candidates

Capture manifests the `main`, `wal`, `shm`, and `journal` names before copying,
then streams every present standard file into a private snapshot and records
post-copy identities. A manifest records schema/tool versions, canonical target
and target ID, presence, byte size, and SHA-256 for all four names. A changed
presence, size, or digest means the source changed and cannot become an
installable candidate.

Snapshots, candidates, reports, target catalogs, and installation state are
private retained evidence. Directories are created `0700`, files `0600`, and
sensitive writes use process umask `077`. They persist until exact eligible
`prune-backup`; there is no automatic retention expiry.

Cleanup opens only a disposable admitted-local scratch copy in SQLite `mode=rw`.
It preserves captured SHM as evidence but does not reuse it as SQLite's scratch
SHM. Normal recovery obtains passive and truncate checkpoint evidence, backs up
the recovered view into a fresh candidate, closes it, reopens it, and checks
that candidate sidecars are absent. The active database and retained source
snapshot are never writable SQLite inputs.

The validation object has exactly these fields, each `pass`, `fail`, or
`not_run`: `integrity`, `foreign_keys`, `checkpoint`, `clean_reopen`, and
`sidecars_absent`. Reports also record SQLite/library versions, checkpoint
counts, file metadata, source/candidate/report identities, and the bounded
preview. They exclude user-table SQL, arbitrary row values, message bodies,
prompts, tokens, credentials, environment dumps, and unbounded exception text.

`complete` means normal SQLite handling and validation prove preservation of
the captured source set. `uncertain` means a valid candidate exists but captured
sidecar evidence, such as stale SHM without WAL or a trailing WAL fragment,
prevents that proof. `invalid` means unsupported journal evidence, malformed
WAL/checkpoint evidence, integrity failure, foreign-key failure, or another
normal-recovery failure. Neither result claims historical completeness before
capture; reports explicitly use `completeness_scope: captured_source_set` and
`historical_completeness: not_proven`.

The optional preview contains only `projects`, `recent_sessions`,
`table_issue_counts`, and `database_issue_count`. Projects expose `id`, `name`,
`worktree`, and optional credential-redacted `origin`; sessions expose `id`,
`title`, `project_id`, and `time_updated`, limited to the four most recently
updated non-archived sessions. Missing OpenCode tables/columns make the affected
domain summary `unavailable`, not a generic cleanup failure.

## Installation and recovery

Installation selects one exact candidate. The catalog rechecks target binding,
source manifest, candidate hash, report schema, and report hash. An uncertain
candidate also requires `--approve-uncertain-report` equal to its exact report
SHA-256. Installation rechecks source identities immediately before intent and
active moves; a changed source refuses replacement.

The database set cannot be swapped atomically. Before each same-filesystem
active-path move, the installation log persists the sequence, source and
destination paths, expected before/after presence and hashes, and next intended
mutation. The intent log is phase authority; observed filesystem paths only
reconcile against it. Each durable transition is synced before the move it
authorizes.

Phases are `prepared`, `quarantining`, `promoting`, `validating`, `installed`,
`install_incomplete`, `rolling_back`, `rolled_back`, and
`manual_recovery_required`. A fresh process may resume only recorded
before/after states. An ambiguous, changed, or missing artifact is retained and
reported as manual recovery required. `rollback` is explicit, stages immutable
snapshot copies, and restores only bytes matching the source manifest.

Successful installation validates the active database with non-migrating SQLite
open/checks and leaves the immutable snapshot retained as rollback evidence.
It does not start or validate through OpenCode; the operator starts OpenCode
separately afterward.

Scratch state records operation ID, creating host, and scratch path in the
catalog before sensitive work. Terminal previews remove their own scratch.
`abort` removes only an exact operation's same-host scratch; a different host is
reported as `scratch_cleanup_required` and is not removed remotely.

## Result and exits

`--json` emits exactly one UTF-8, ASCII-safe, newline-terminated object on
stdout, capped at 64 KiB. It has no unknown version-1 fields:

```text
schema_version, command, ok, status, exit_code, target, snapshot_id,
candidate_id, operation_id, report_sha256, completeness, validation, preview,
next_actions, diagnostics
```

`schema_version` is integer `1`. `diagnostics` is a bounded list of closed
`{code, message}` records. Machine-mode syntax failures use the same result
shape. Human output is rendered from the same result to stderr. Future result,
catalog, manifest, report, or install schemas and unknown enum values are
read-only compatibility failures: readers do not migrate them in place or
mutate active files.

| Exit | Class | Examples |
| --- | --- | --- |
| 0 | terminal success | complete preview, installed, rolled back, status, backup pruned |
| 2 | syntax/input shape | malformed command, relative path, incomplete ID |
| 3 | decision required | validated uncertain preview or missing exact uncertain approval |
| 4 | safety precondition refusal | changed source, unsupported journal, invalid target, mismatched evidence |
| 5 | operational/validation failure | invalid candidate, deadline, bounded I/O or validation failure |
| 6 | manual recovery required | unreconcilable active installation or cross-host scratch cleanup |

Stable status values are `complete`, `uncertain`, `source_changed`, `invalid`,
`installed`, `install_incomplete`, `manual_recovery_required`, `rolled_back`,
`backup_pruned`, `status_ok`, `aborted`, `scratch_cleanup_required`,
`target_invalid`, `precondition_refused`, `syntax_error`, and
`operational_failure`.

Operations have a default 1800-second deadline and accept only finite whole
seconds from 1 to 86400. Retained storage, scratch, staging, and rollback need
free-byte/inode preflight. Local scratch admission is limited to documented
Linux local filesystems; remote or unclassified scratch fails closed.

## Exact backup pruning

`cleanup prune-backup --database ABSOLUTE_PATH --snapshot ID` resolves one
complete snapshot ID through the target catalog. It refuses foreign/unknown IDs,
path-shaped IDs, prefixes, globs, changed or unexpected group members, and a
snapshot referenced by a nonterminal installation. Otherwise it removes only
manifest-bound files in that exact group and then its catalog record. An
interrupted prune has no journal or tombstone: retry the same exact ID to remove
the remaining eligible files. It never opens or mutates the active database.

`cleanup prune` is intentionally absent and reserved for future
active-database retention. This protocol contains no active-database pruning,
automatic cleanup, automatic rollback, or general SQLite salvage.
