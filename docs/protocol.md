# Retained Protocol

This document specifies the version-1 local protocol used by `opencode-db`.
It is intentionally a fail-closed retained-evidence protocol, not an OpenCode
database schema specification. The command never discovers targets through
OpenCode, starts OpenCode, inspects processes, or coordinates database users.

## Project and session inspection

The human-only read-only inspection grammar is:

```text
opencode-db list-projects [--db ABSOLUTE_DB]
opencode-db show-project [--db ABSOLUTE_DB] --project-id ID
opencode-db show-session [--db ABSOLUTE_DB] --session-id ID
```

No inspection command accepts `--json`. An omitted `--db` selects
`$XDG_DATA_HOME/opencode/opencode.db`; when `XDG_DATA_HOME` is absent or not an
absolute path it falls back to `$HOME/.local/share/opencode/opencode.db`. If
neither base is absolute, or the selected file is absent or not regular, the
command refuses. Successful output starts with the selected canonical database
path. The database is opened only through SQLite `mode=ro`; inspection never
prompts, mutates, checkpoints, migrates, invokes OpenCode, or inspects
processes. Failed SQLite integrity or foreign-key checks are safety refusals.

`list-projects` emits one deterministic block per exact `project.id`. It prints
available safe project metadata and all supported registered structure:
worktree, decoded absolute sandboxes, `project_directory` directory/type/strategy
records, workspace directory/count summaries, and session directory/count
summaries. It never reads arbitrary message or event data.

`show-project` requires exactly one matching project row. It prints the same
project structure followed by deterministic session summaries. Every session
summary starts with `session_id` and provides available safe metadata plus
separate `v2_turns`, `legacy_turns`, and `pending_inputs` counts. Turn counts
include only visible user/assistant rows. It never prints transcript text.

`show-session` requires exactly one matching session row and its one project
row. It prints session metadata, project metadata, then separate turn/input
counts. Current V2 rows are rendered only in `session_message` `seq` order;
legacy rows are rendered separately in `message` `time_created,id` order with
their `part` children in OpenCode's deterministic `id` order. User, assistant,
and system text is shown. Reasoning, tools, shell activity, compaction, and
other non-text records are concise labels only: tool inputs/results, shell
commands/output, and hidden context snapshots are never dumped.
Unpromoted `session_input` prompts are a separate pending section; promoted
inputs are never duplicated. `session_context_epoch.baseline` and snapshots are
never read or printed. If both transcript systems exist they remain separate,
not a synthesized chronology. Unsupported required columns or malformed JSON
are bounded safety refusals rather than guessed or silently omitted output.
Transcript output is intentionally private; operators must protect stdout and
must not redirect it to shared logs.

## Session transfer

Top-level transfer commands have separate, human-only output from the closed
cleanup result schema and accept no `--json` option:

```text
opencode-db export [--db ABSOLUTE_DB] --project-dir ABSOLUTE_PROJECT_DIR --export-dir ABSOLUTE_EXPORT_DIR
opencode-db import --target-project-dir ABSOLUTE_TARGET_PROJECT_DIR [--db ABSOLUTE_DB] --import ABSOLUTE_IMPORT_FILE
```

An explicit `--db` value must name an absolute non-memory path. An omitted
`--db` uses the bounded XDG/HOME selection defined below. Both resulting
transfer targets must be existing regular SQLite files and are opened with
SQLite `mode=rw`; the tool never creates a replacement, empty, or in-memory
database. Project directories must be existing absolute directories. Export
resolves one project ID from canonical equality with
`project.worktree`, `project_directory.directory`, or `session.directory`.
Multiple IDs or no ID are refusals. A global project resolves only from an exact
session-directory match. Global session rows are selected only when
their directory canonically matches the source project directory, preventing
unrelated global state from crossing projects.

An export creates the requested directory with mode `0700`, then atomically
publishes exactly one mode-`0600` SQLite archive. Its closed schema contains the
version, canonical export directory, and source project ID in metadata plus these current
session-owned tables: `session`, `message`, `part`, `todo`, `session_message`,
`session_input`, `session_context_epoch`, `session_share`, `event_sequence`,
and `event`. It retains every table column and SQLite scalar or blob value. The
human result reports the canonical export directory, source project ID, archive
path, and session count only; it never reports session contents.

Import validates the closed archive schema before changing the target database.
It requires target table column and foreign-key layouts to match the archive,
refuses unknown tables that carry `session_id` or reference a session-owned
table, and refuses integrity or foreign-key failures. In one deferred-
foreign-key transaction it deletes only rows owned by imported session IDs,
replaces their `session` and child-table state, maps `session.project_id` to the
resolved target project, sets `session.directory` to the absolute target,
updates `session.path` relative to target `project.worktree` when present, and
clears `workspace_id` when present.
Before deletion, an existing imported ID must already belong to the target
project or be a global session whose canonical directory is the target; another
project's ID is refused without mutation. Export and import also require every
foreign key between archived session-owned tables to have its parent in the
transfer scope; `session.project_id` is intentionally outside that scope.
Session IDs and parent relationships are retained. A failed insert, check, or
commit rolls back the entire import; repeating a successful archive import is
idempotent and does not alter unrelated project state.

## Sibling project move

The human-only metadata move grammar is:

```text
opencode-db mv --project-id ID --target-project-dir ABSOLUTE_TARGET_PROJECT_DIR [--db ABSOLUTE_DB] [--method sibling] [--yes] [--progress]
```

`--project-id` and `--target-project-dir` are required. `--method` defaults to
and accepts only `sibling`; `--json`, relative paths, duplicate options, unknown
options, and unsupported methods are usage errors before SQLite or Git access.
An explicit `--db` is absolute and non-memory. An omitted `--db` uses the same
bounded XDG/HOME default described in Target and identities.

The operator owns filesystem placement: copy the complete flat sibling checkout
family first, invoke `mv` to change metadata, verify the result, then separately
remove old directories if desired. The command never copies, moves, creates,
repairs, renames, or removes source or target project checkout entries, nor does
it change their contents. SQLite may perform normal writes to the selected
database and its SQLite-managed standard sidecars. The command validates every
supported structured project location, compares each source/target Git checkout
locally, then writes a deterministic complete mapping preview to stdout. Paths
are single-line ASCII-escaped and quoted; each mapping reports the affected
structured category counts.

Without `--yes`, both stdin and stdout must be terminals. The command refuses
without reading stdin when either is detached. With both terminals, only exact
lowercase `y` after line-ending removal authorizes application; `n`, any other
input, EOF, and interruption cancel. `--yes` bypasses only the prompt, never
the preview, eligibility, Git, schema, or freshness checks. After authorization,
the command revalidates and atomically updates only supported structured
worktree, sandbox, project-directory, session-directory, and non-null
workspace-directory fields. Historical messages, prompts, tools, output, and
other free-form content are not rewritten.

`--progress` is opt-in. It writes only fixed phase labels and aggregate counts to
stderr: bounded collection, Git-pair validation, revalidation, and update
groups. Terminal stderr may redraw one ASCII bar and finishes with a newline;
redirected stderr receives capped newline-delimited records without carriage
returns. Progress contains no IDs, paths, remotes, or database content and does
not alter stdout, prompting, validation, or transaction behavior.

## Target and identities

Every database-accepting command accepts an explicit absolute database option or
an omitted bounded default. The default is
`$XDG_DATA_HOME/opencode/opencode.db` when `XDG_DATA_HOME` is absolute;
otherwise it is `$HOME/.local/share/opencode/opencode.db` when `HOME` is
absolute. An absolute XDG base wins regardless of selected-file existence; the
tool does not search HOME based on the file. Neither usable base is a bounded
refusal. Default selection only constructs a string and never stats, opens, or
canonicalizes it. Explicit paths remain non-memory and absolute. Preview,
install, export, import, and inspection require a present regular file through
their existing validators. Status, abort, resume, rollback, and `prune-backup`
can resolve a recorded target while an incomplete installation has moved the
active main file. Relative paths, `:memory:`, missing preview targets, paths
used as IDs, prefixes, and globs are refused.

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

Cleanup commands with `--json` emit exactly one UTF-8, ASCII-safe,
newline-terminated object on stdout, capped at 64 KiB. Top-level transfer and
inspection commands instead use their documented human-only output. `mv` is also
human-only and has no JSON result. The cleanup result has
no unknown version-1 fields:

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
| 4 | safety precondition refusal | changed source, unsupported journal, invalid target, mismatched evidence, malformed transfer archive, ambiguous project, incompatible schema, `mv` validation refusal, cancellation, or detached-stream refusal |
| 5 | operational/validation failure | invalid candidate, deadline, bounded I/O or validation failure, transfer filesystem/SQLite/integrity/publication failure, bounded `mv` SQLite or Git execution failure |
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

`cleanup prune-backup [--database ABSOLUTE_PATH] --snapshot ID` resolves one
complete snapshot ID through the target catalog. It refuses foreign/unknown IDs,
path-shaped IDs, prefixes, globs, changed or unexpected group members, and a
snapshot referenced by a nonterminal installation. Otherwise it removes only
manifest-bound files in that exact group and then its catalog record. An
interrupted prune has no journal or tombstone: retry the same exact ID to remove
the remaining eligible files. It never opens or mutates the active database.

`cleanup prune` is intentionally absent and reserved for future
active-database retention. This protocol contains no active-database pruning,
automatic cleanup, automatic rollback, or general SQLite salvage.
