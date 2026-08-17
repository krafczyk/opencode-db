# OpenCode Database Cleanup Tool

`opencode-db` is a standalone Linux command-line tool for making a validated,
sidecar-free SQLite candidate from an explicit OpenCode database. It is owned
by this repository; standalone installation and ordinary use do not modify
OpenCode, MkChad, or container tooling. MkChad deployment can additionally
install the same tool through the child-owned action described below.

The tool is an operator-run recovery aid for the supported normal SQLite WAL
path. It is not an OpenCode integration, a service manager, or a general SQLite
salvage tool.

## Scope and requirements

- Linux only; Python 3.11 or newer.
- No runtime dependencies beyond the Python standard library.
- Every database-accepting command accepts an optional explicit absolute
  database option. When omitted, it selects `$XDG_DATA_HOME/opencode/opencode.db`
  if `XDG_DATA_HOME` is absolute, otherwise
  `$HOME/.local/share/opencode/opencode.db` if `HOME` is absolute. An absolute
  XDG base wins even when its selected file is absent; the tool never searches
  OpenCode or falls back based on file existence. Explicit values never accept a
  relative or in-memory target.
- The operator owns shutdown, restart, and concurrent-use safety. Commands do
  not inspect processes or coordinate other users. The destructive `mv`,
  `repair-move`, and `prune` commands ask for confirmation unless `--yes` is
  explicit; move commands first preview their complete metadata actions.
- The tool never starts OpenCode. After a successful install, start OpenCode
  separately using its unchanged normal command and configuration.

## Project and session inspection

These human-only, read-only commands inspect an existing OpenCode database
without prompting, mutating, checkpointing, migrating, starting OpenCode, or
inspecting processes:

```bash
opencode-db list-projects
opencode-db show-project --project-id ID
opencode-db show-session --session-id ID
opencode-db list-sessions --project-id ID
```

Add `--db /absolute/path/opencode.db` to any inspection command to override the
default database.

The selected canonical database path is printed first. Selection fails closed
when the default base is absent or non-absolute, or when the selected path is
not an existing regular file. Inspection opens SQLite only in `mode=ro` and
refuses failed SQLite integrity or foreign-key checks.
`list-projects` groups safe project metadata, worktrees, decoded sandboxes,
registered project directories, workspace directory summaries, and session
directory/count summaries. `show-project` adds deterministic session summaries
and separate visible V2, legacy, and pending-input counts, but never prints any
transcript text.

`list-sessions` lists all sessions, optionally scoped to one exact existing
project, in `time_updated` newest-first and ID tie-break order. It renders only
safe metadata and optional logical-size estimates, never row counts or
transcript bodies. Use
`--estimate-session-size` with it or `show-session`; use
`--estimate-project-size` with `list-projects` or `show-project`. An estimate
is a logical payload-byte count: the UTF-8/blob representation of every non-NULL
value in the session row and every current known session-owned child row. It
excludes SQLite record headers, pages, indexes, freelist, WAL, and schema bytes.
Project estimates sum their sessions. Estimate commands require the complete
known session schema and refuse unknown session-linked tables or triggers rather
than omitting state.

`show-session` prints safe session and project metadata, those separate counts,
then separate V2 and legacy transcript sections when present. It renders only
user, assistant, and system text; reasoning, tools, shell activity, compaction,
and other non-text records are concise labels without tool inputs/results,
shell command/output, or hidden context snapshots. Unpromoted inbox prompts are
shown separately and promoted prompts are not repeated. Transcript output is
intentionally private: protect stdout and do not redirect it to shared logs.
There is no `--json` inspection mode.

## Session transfer

Session transfer is a project-scoped workflow for moving complete current
OpenCode session state between projects known to separate databases. It uses the
same bounded optional database selection as every other database command and
does not discover a database, project, or directory. Arrange database shutdown
and concurrent-use safety before starting.

Export from the selected existing database and existing source project directory:

```bash
opencode-db export --project-dir /absolute/source/project \
  --export-dir /absolute/private/exports
```

Add `--db /absolute/path/opencode.db` to override the default source database.

The command prints the canonical export directory, resolved source project ID,
and one import-file path, never session content. It creates the export directory
as mode `0700` and publishes one SQLite import file as mode `0600` atomically.
The archive contains all rows and columns from `session`, `message`, `part`,
`todo`, `session_message`, `session_input`, `session_context_epoch`,
`session_share`, `event_sequence`, and `event` for the selected project. Global
sessions are included only when their recorded directory matches the selected
project directory.

Import that exact file into the selected existing target database and project directory:

```bash
opencode-db import --target-project-dir /absolute/target/project \
  --import /absolute/private/exports/opencode-session-YYYYMMDDTHHMMSSZ-HEX.sqlite
```

Add `--db /absolute/path/target-opencode.db` to override the default target
database.

Import resolves exactly one target project, preserves session IDs and parent
relationships, changes imported `session.project_id` to that target, stores the
absolute target in `session.directory`, updates `session.path` relative to
`project.worktree` when those columns are available, and clears `workspace_id`
when that column exists. It replaces only imported session rows
and their child-table rows in one SQLite transaction, so rerunning the same
import is idempotent and unrelated projects remain untouched.
If an imported session ID is already owned by another target project, import
refuses before changing any target row. Archives with a transferred-table
foreign key whose parent is outside the archive are also refused.

Export and import require existing regular SQLite databases opened in
`mode=rw`, enable foreign keys, and validate integrity before success. They
refuse malformed archives, ambiguous project matches, incompatible schemas, or
unknown tables that carry `session_id` or reference session-owned tables. The
transfer archive is private session data: keep its path private and delete it
only when it is no longer needed.

Normal WAL replay, checkpoint, and SQLite backup are the only recovery method.
Rollback-journal evidence, malformed inputs, changed source files, unsupported
scratch storage, unknown retained schemas, and candidates that cannot validate
are refused. The tool never creates an empty, in-memory, or replacement
database as a fallback.

## Sibling project move

`mv` updates only the selected project's current structured location metadata
after the operator has copied a flat family of sibling Git checkouts. It never
copies, moves, creates, repairs, renames, or removes source or target project
checkout entries, nor does it change their contents. SQLite may perform normal
writes to the selected database and its SQLite-managed standard sidecars. The
operator first copies the source family, then runs `mv`, verifies normal OpenCode
use, and finally performs any old-directory cleanup separately.

```bash
opencode-db mv --project-id ID \
  --target-project-dir /absolute/new-parent/project \
  [--db /absolute/path/opencode.db] [--method sibling] \
  [--application-timeout-seconds SECONDS] [--yes] [--progress]
```

`--project-id` and `--target-project-dir` are required. `--method` defaults to
and currently accepts only `sibling`. `--db` follows the same bounded default
selection as the other database commands. The target main checkout must retain
the source main checkout basename, and every known structured project location
must be an immediate sibling in both source and copied target families.

Before any mutation, `mv` writes a deterministic complete source-to-target
mapping to stdout with affected structured categories and counts. Without
`--yes`, both stdin and stdout must be terminals; only an exact lowercase `y`
after the input line ending is removed authorizes the transaction. Any other
input, end-of-input, or interruption cancels without applying metadata changes.
`--yes` bypasses only that prompt, never the preview, filesystem checks, Git
correspondence checks, schema checks, or freshness-bound transaction.

The post-confirmation SQLite writer transaction has a 10-second deadline by
default. `--application-timeout-seconds` accepts finite positive seconds up to
86,400 and changes only that complete application deadline; writer-lock
acquisition and individual Git probes retain their separate bounded timeouts.

`--progress` is opt-in and writes only fixed phase labels and aggregate counts
to stderr. Terminal stderr may redraw an ASCII bar; redirected stderr receives
bounded newline-delimited records. Progress never changes stdout mappings,
authorization, validation, or transaction behavior. The rewrite changes only
the supported project worktree, sandbox, project-directory, session-directory,
and non-null workspace-directory fields. Historical messages, prompts, tools,
output, and other free-form content remain unchanged.

### Repairing a re-registered source family

If OpenCode accesses a retained source checkout after `mv`, it can register that
old main checkout and its Git worktrees again even though the primary worktree
and sessions were moved. `repair-move` removes that source-family contamination
without changing either checkout family:

```bash
opencode-db repair-move --project-id ID \
  --source-project-dir /absolute/old-parent/project \
  --target-project-dir /absolute/new-parent/project \
  [--db /absolute/path/opencode.db] \
  [--application-timeout-seconds SECONDS] [--yes]
```

Run it while OpenCode is stopped. The selected project's primary worktree must
already equal the target, source and target main basenames must match, and every
structured location must be an immediate child of one of those two parents.
Each source/target pair must exist as a Git checkout with the same project
identity; the target may have advanced to another branch or commit after the
move.

The preview labels every source owner as `drop` or `rebase`. An old sandbox is
dropped when its target is already the primary or a recorded sandbox. A source
`project_directory` row is dropped only when its target twin has the same type
and strategy; conflicting twins refuse the whole repair. Other source rows are
rebased to their target sibling. After confirmation, the command revalidates the
complete selected state and applies all actions in one bounded transaction.
Source and target filesystem content, timestamps, opaque history, and unrelated
projects remain unchanged. Opening the retained source family again can
re-register it, so delete, archive, or avoid that family after verification.

## Standalone install

Build a committed local wheel from a temporary source tree without contacting a
package index, then install that wheel into an isolated environment:

```bash
mkdir -p /tmp/opencode-db-v1
BUILD_ROOT=$(mktemp -d /tmp/opencode-db-v1/build.XXXXXX)
git archive --format=tar HEAD | tar -xf - -C "$BUILD_ROOT"
python3 -m build --no-isolation --outdir "$BUILD_ROOT/dist" "$BUILD_ROOT"
python3 -m venv /tmp/opencode-db-v1/venv
/tmp/opencode-db-v1/venv/bin/python -m pip install --no-index --no-deps \
  "$BUILD_ROOT/dist/opencode_db-0.1.0-py3-none-any.whl"
/tmp/opencode-db-v1/venv/bin/opencode-db --help
```

The `--no-isolation` build assumes the local build backend is already
provisioned. Building from the temporary archive keeps setuptools intermediates
out of the source checkout. The package is not published by this repository.

## MkChad-managed deploy install

The standalone wheel and virtual-environment installation above remains fully
supported for independent use. MkChad release and developer deployment may
additionally invoke the child-owned action from its pinned checkout. It installs an
exact launcher at `~/.local/bin/opencode-db` without building a wheel, contacting
a package index, or adding runtime dependencies:

```bash
case ${XDG_DATA_HOME:-} in
  /*) DATA_HOME=$XDG_DATA_HOME ;;
  *)
    case ${HOME:-} in
      /*) DATA_HOME=$HOME/.local/share ;;
      *) printf '%s\n' "XDG_DATA_HOME or HOME must be absolute" >&2; exit 1 ;;
    esac
    ;;
esac
COMPONENT="$DATA_HOME/mkchad/components/opencode-db"
RECOVERY_DIR=/private/caller-created/recovery-directory
"$COMPONENT/bin/install_opencode_db.sh" --check --recovery-dir "$RECOVERY_DIR"
"$COMPONENT/bin/install_opencode_db.sh" --recovery-dir "$RECOVERY_DIR"
"$COMPONENT/bin/install_opencode_db.sh" --check
```

The preflight form is read-only and validates Python 3.11+, the pinned source,
target safety, and replacement eligibility. The apply form requires a mode-0700
private recovery directory; if it replaces a noncompliant regular launcher, it
retains that file as `opencode-db` in that directory. The final check compares
the installed launcher byte-for-byte with the pinned launcher and runs only
`opencode-db --help` under a short timeout. At runtime the launcher derives the
pinned checkout from absolute `XDG_DATA_HOME`, or from absolute `HOME` when XDG
is absent or relative, sets `PYTHONPATH` to only that checkout's `src`, and
executes `python3 -m opencode_db` with unchanged arguments.

## Operator workflow

First arrange OpenCode shutdown if it is needed. Then preview the bounded
default database or provide an explicit absolute path. Preview immediately
captures the main file and any present
`-wal`, `-shm`, and `-journal` sidecars, works only on a separate scratch copy,
and retains the source snapshot privately beside the target.

```bash
opencode-db cleanup preview --json
```

Add `--database /absolute/path/opencode.db` to override the default cleanup
target.

Preview has three classifications:

- `complete`: normal SQLite handling and validation prove preservation of the
  captured source set. This does not prove history that was already absent when
  capture began.
- `uncertain`: a readable, validated candidate exists, but captured sidecar
  evidence prevents that proof. Installation requires approval of the exact
  report digest.
- `invalid`: normal recovery or validation failed. No installation is offered.

Preview includes only lightweight recognizable fields: project ID, name,
worktree, an optional credential-redacted origin, the four newest sessions, and
bounded foreign-key/integrity issue counts. It does not print message bodies,
prompts, credentials, arbitrary rows, or SQLite error text.

Installation is a separate explicit action. Use the exact candidate ID returned
by preview. A complete candidate needs no extra confirmation:

```bash
opencode-db cleanup install --candidate candidate-YYYYMMDDTHHMMSSZ-HEX
```

For an uncertain candidate, copy the exact `report_sha256` from that same
preview result. This is approval of that candidate/report pair, not a broad
override:

```bash
opencode-db cleanup install --candidate candidate-YYYYMMDDTHHMMSSZ-HEX \
  --approve-uncertain-report LOWERCASE_SHA256
```

Before moving active files, installation rechecks the captured source bytes. It
retains the full original source snapshot as rollback evidence, writes durable
per-file intent before each move, and validates the installed database with
SQLite. It does not start or validate through OpenCode.

If installation is interrupted after active-file mutation, do not guess from
filesystem paths. Inspect the exact recorded operation and choose one explicit
recovery action:

```bash
opencode-db cleanup status --operation install-YYYYMMDDTHHMMSSZ-HEX --json
opencode-db cleanup resume --operation install-YYYYMMDDTHHMMSSZ-HEX
opencode-db cleanup rollback --operation install-YYYYMMDDTHHMMSSZ-HEX
```

`status` is read-only. `resume` accepts only the durable recorded before/after
states. `rollback` is always explicit and restores only hash-verified retained
source bytes. For an incomplete preview scratch operation, use its exact
operation ID with `cleanup abort`; cross-host scratch is reported as requiring
manual cleanup rather than removed remotely.

```bash
opencode-db cleanup abort --operation operation-YYYYMMDDTHHMMSSZ-HEX
```

## Retained artifacts and pruning

Private target-scoped evidence is retained under
`<database-parent>/.opencode-db/<target-id>/`. It includes source manifests and
snapshots, candidates, reports, and installation intent/recovery state. There
is no age- or count-based deletion.

Only `prune-backup` removes retained recovery evidence. It accepts one complete
snapshot ID selected through the target catalog, never a path, prefix, or glob,
and refuses snapshots required by an incomplete installation. A failed or
interrupted prune may be retried with the same exact ID.

```bash
opencode-db cleanup prune-backup \
  --snapshot snapshot-YYYYMMDDTHHMMSSZ-HEX --json
```

## Active session pruning

`prune` is an active-database retention command. Arrange OpenCode shutdown and
writer concurrency before running it. It first opens the selected existing
database read-only to calculate and flush an aggregate preview: scoped sessions
to prune and keep, plus the oldest surviving scoped update time in local ISO
8601 form with a numeric UTC offset (or `none`). With `--estimate-size`, the
preview also reports projected deleted logical bytes and projected logical
database bytes after pruning. It never prints session IDs, titles, transcripts,
payloads, or per-session deletion lines.

```text
opencode-db: prune preview
sessions_to_prune: N
sessions_to_keep: N
oldest_surviving_session_updated: LOCAL_ISO8601_WITH_NUMERIC_OFFSET | none
projected_logical_bytes_deleted: N
projected_logical_database_bytes_after_prune: N
```

The projected fields appear only with `--estimate-size`.

Read-only planning and writer-side revalidation each accept at most 250,000
scoped candidate sessions, 16 KiB per persisted session ID, and 64 MiB of
captured candidate evidence. Each phase has its own 300-second execution
deadline by default. `--timeout-seconds` accepts finite positive seconds up to
86,400 and gives planning, application, and requested vacuum separate full
windows; time spent reviewing or confirming the preview consumes none of those
windows. Slow or oversized evidence fails with a bounded phase-specific
diagnostic before authorization or deletion.

Without `--yes`, both stdin and stdout must be terminals after that preview and
only an exact lowercase `y` authorizes any remaining mutation; other input, EOF,
or interruption cancels without opening SQLite writable. `--yes` bypasses only
the prompt, not preview or revalidation. A zero-selection request without
`--vacuum` returns its compatible zero result after preview without probing the
terminal or opening SQLite writable. A zero-selection request with `--vacuum`
still requires authorization because compaction changes the database. After
authorization, the command revalidates the reviewed selection under its writer
transaction; a changed selection refuses without deletion and instructs the
operator to rerun the command.

The writable phase opens only the selected existing database through SQLite
`mode=rw`, enables foreign keys, uses a bounded writer wait, validates integrity
and foreign keys before and after its immediate transaction, and deletes only
complete selected session state. It refuses an incomplete schema, unknown
session-linked table or trigger, malformed data, or an exact missing project
instead of guessing ownership. It also refuses non-unique session IDs and
known-table foreign keys that cross the selected and retained ownership boundary.

```bash
opencode-db prune [--db /absolute/path/opencode.db] \
  ([--project-id ID] \
    (--oldest N|TIME | --keep-newest N|TIME | --target-size SIZE) \
    [--estimate-size] [--vacuum] | --vacuum-only) \
  [--timeout-seconds SECONDS] [--yes]
```

Exactly one selector is required unless `--vacuum-only` is used. Positive `N`
means sessions; the canonical
count ordering is `time_updated` descending with ID ascending tie-breaks, and
`--oldest N` deletes from that ranking's tail. `TIME` is a positive integer plus
`d`, `m`, or `y`, where a day is 24 hours, a month is 30 days, and a year is 365 days.
`--oldest TIME` deletes the inclusive window from the oldest matching timestamp
through that timestamp plus the duration. `--keep-newest TIME` deletes sessions
older than now minus the duration. `SIZE` uses the explicit grammar
`N[B|KiB|MiB|GiB|TiB]`; it retains a newest prefix whose complete session logical
estimates fit the target. If the newest session itself exceeds the target, it and
all older candidates are pruned.

`--estimate-size` reports projected logical bytes before authorization and
committed logical bytes after pruning without rendering row data. Ordinary
deletion makes SQLite pages reusable but usually does not shrink the database
file.
`--vacuum` runs only after a successful revalidated delete transaction (or a
revalidated zero-selection request), physically compacts the database, and
reports resulting file bytes. Selector decisions always use logical estimates,
never physical file size. If deletion commits but the later vacuum fails, the
command reports the committed session count and exits with an operational failure
that explicitly warns not to repeat the destructive prune request and directs the
operator to retry compaction with `prune --vacuum-only`. Vacuum-only mode accepts
no selector, project scope, estimate, or `--vacuum`; it validates and confirms the
selected database, changes no session rows, and uses its own `--timeout-seconds`
window. SQLite vacuum can still fail for reasons such as insufficient temporary
storage even when its timeout is long enough.

## Results, deadlines, and storage

Cleanup commands accept `--json` for exactly one newline-terminated
schema-version-1 JSON object on stdout. Top-level `export`, `import`, `prune`, and
the inspection commands do not accept `--json`; `mv` and `repair-move` also have
human-only output and do not accept `--json`. Their separate human-only success
output is written to stdout and their bounded diagnostics to stderr. Exit classes
are `0` success, `2` syntax/input shape, `3` uncertain decision required, `4`
safety-precondition refusal (including move validation and destructive-command
detached-stream refusal or cancellation),
`5` bounded operational SQLite or Git failure, and `6` manual recovery required.

The default deadline is 30 minutes. `--deadline-seconds` accepts finite whole
seconds from 1 through 86400. SQLite busy waits are finite. The tool requires
private retained storage, admitted local scratch storage, and sufficient space
and inodes before sensitive writes. Recovery can temporarily multiply disk use:
plan capacity for the original source set, retained snapshot, scratch copy,
candidate, staging, and rollback files. Set `--scratch-dir` to an explicit
absolute local path when the default `$TMPDIR/opencode-db` location is not the
right local filesystem.

See [`docs/protocol.md`](docs/protocol.md) for the retained protocol, result
schema, compatibility rules, and exact recovery/pruning eligibility.

## Private observed-case acceptance

The deterministic test suite generates disposable WAL fixtures and never reads
operator databases. A separate opt-in gate, `tests/private_acceptance.py`, is
for a user-supplied **copied** fixture on the affected filesystem. It requires
explicit absolute input, work, and scratch paths plus an expected classification
and candidate-presence oracle; it operates only on a fresh copy under the
provided work path, verifies the supplied source bytes remain unchanged, and
emits only bounded classification/outcome JSON.

```bash
python3 tests/private_acceptance.py \
  --database /explicit/copied/opencode.db \
  --work-directory /explicit/private/work/opencode-db-case \
  --scratch-dir /explicit/local/scratch/opencode-db-case \
  --expected-status complete --expect-candidate present
```

It neither discovers workspace/live paths nor starts OpenCode. Its fixed
30-minute deadline covers source hashing, copying, and cleanup except time spent
inside one blocked filesystem syscall. A zero exit proves only the tool-side
classification, candidate-presence oracle, and unchanged source bytes; release
acceptance also requires the operator to bind that run to separate original and
cleaned unchanged-OpenCode reopen observations. The host-owned observed-case
gate has already established only this bounded fact:
normal cleanup checkpointed 575 WAL frames, the sidecar-free candidate passed
integrity and foreign-key validation and started OpenCode, source hashes were
unchanged, while the original copied state failed with `ServeError` on the
affected HPC filesystem. If that private fixture or its expected oracle is not
available, this gate is unavailable and release acceptance remains incomplete.
