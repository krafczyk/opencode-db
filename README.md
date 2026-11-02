# OpenCode Database Cleanup Tool

`opencode-db` is a standalone Linux command-line tool for making a validated,
sidecar-free SQLite candidate from an explicit OpenCode database. It is owned
by this repository; standalone installation and ordinary use do not modify
OpenCode, MkChad, or container tooling. MkChad deployment can additionally
install the same tool through the child-owned action described below.

The tool is an operator-run recovery aid for the supported normal SQLite WAL
path. It is not an OpenCode integration, a service manager, or a general SQLite
salvage tool.

## WAL repair quick start

**WAL repair is the two-step `cleanup preview` → `cleanup install` workflow.**
Running `opencode-db cleanup` alone prints these instructions; it does not repair
or replace the database.

Stop OpenCode and arrange concurrent-use safety, then run:

```bash
opencode-db cleanup preview
```

Preview retains the original database and sidecars, rebuilds SQLite's WAL index
on a working copy, replays/checkpoints the WAL, and validates a sidecar-free
candidate. **It does not change the active database.** Run the exact `next:`
`opencode-db cleanup install ...` command it prints to install that candidate.
For `uncertain` candidates, review the classification before using the printed
report-digest approval. Invalid candidates cannot be installed. Start OpenCode
separately after a successful install.

To perform the space-heavy work elsewhere, create a private work directory on a
local filesystem with enough room:

```bash
mkdir -m 700 /absolute/spacious/opencode-db-work
opencode-db cleanup preview --work-dir /absolute/spacious/opencode-db-work
# Run the printed install command, which includes the same --work-dir.
```

Add `--database /absolute/path/opencode.db` to select a non-default cleanup
database. `--work-dir` changes work placement, not database selection. See
[Work location and capacity diagnostics](#work-location-and-capacity-diagnostics)
for placement details, including the space still required beside the database.

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
and retains the source snapshot privately beside the target by default. With
`--work-dir`, retained evidence goes under that explicit directory instead.

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

`status` is read-only. For `install-…` operations, `resume` accepts only the
durable recorded before/after states. `rollback` is always explicit and restores
only hash-verified retained source bytes. For `operation-…` preview IDs, `resume`
uses the completed stages described below. To explicitly discard an incomplete
preview, use its exact operation ID with `cleanup abort`; cross-host scratch is
reported as requiring manual cleanup rather than removed remotely.

```bash
opencode-db cleanup abort --operation operation-YYYYMMDDTHHMMSSZ-HEX
```

### Resuming long previews

Preview and preview-resume default to a **four-hour** attempt budget instead of
30 minutes. `--deadline-seconds` can select a different finite budget up to 24
hours. Each resume gets a fresh budget; it does not inherit elapsed time from
the previous invocation.

```bash
opencode-db cleanup preview --work-dir /absolute/private/local-work --progress
# After interruption, use the exact operation ID printed by preview:
opencode-db cleanup status --work-dir /absolute/private/local-work \
  --operation operation-YYYYMMDDTHHMMSSZ-HEX
opencode-db cleanup resume --work-dir /absolute/private/local-work \
  --operation operation-YYYYMMDDTHHMMSSZ-HEX --progress
```

Use the same `--database` selection if it was supplied initially. Resume uses
the **recorded scratch path**, including a separate `--scratch-dir` from preview;
it does not redirect or migrate existing work to another scratch root.

Once the original source snapshot has been accepted, completed stages survive
timeouts, Ctrl-C, and abrupt process termination:

| Completed checkpoint | Work reused after restart |
| --- | --- |
| Accepted retained source snapshot | No new capture of the active database |
| Scratch inputs copied | No repeat of the completed scratch copy |
| WAL recovery completed | No repeat of completed WAL recovery |
| Standalone candidate created | No repeat of SQLite backup |
| Integrity check passed | No repeat of the completed integrity scan |
| Foreign-key check passed | No repeat of that completed check |
| Candidate validated | Validation receipt and bounded preview reused |
| Candidate/report published | Same verified candidate returned; no duplicate publication |

The operation ID is announced after registration, before the source copy.
Progress reports the current stage and elapsed time on stderr, automatically
on an interactive human terminal or explicitly with `--progress`. With `--json`,
stdout remains exactly one result; stderr progress requires `--progress`.
Timeout/interruption results include the operation ID and a copyable resume
command. Status reports the last saved checkpoint without scanning the database.

Initial source capture must finish and pass its pre/copy/post hash comparison
before it is reusable. **Partial source capture is not resumable.** An interrupted
scratch copy, SQLite backup, integrity scan or foreign-key scan restarts that
individual stage. An interrupted WAL recovery restages scratch inputs because
SQLite may have modified them. Resume also rehashes retained inputs and saved
outputs before trusting their receipts; verification I/O is not skipped.

Keep both the retained work store and registered scratch. Scratch now contains
recovery checkpoints, not just disposable intermediate files. Missing scratch
requires rebuilding from the accepted snapshot; malformed or changed checkpoint
evidence is preserved and refused. Resume must run on the recorded scratch host.
Cooperative simultaneous resume/abort attempts refuse while the operation lock
is held; a killed process releases that lock automatically.

The accepted snapshot from an older failed preview can also be reused with its
`operation-…` ID. Already deleted old scratch cannot be recovered. If the active
database has changed since capture, preview still describes the original
snapshot and installation will refuse to replace the changed active source.

## Retained artifacts and pruning

Private target-scoped evidence is retained under
`<database-parent>/.opencode-db/<target-id>/`, or
`<work-dir>/.opencode-db/<target-id>/` when `--work-dir` was selected. It includes source manifests and
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

For long prunes with minimal downtime, use the opt-in
[staged prune and update workflow](#staged-prune-and-update). Ordinary `prune`
below remains the direct active-database operation.

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
diagnostic before authorization or deletion. On large databases, allow planning
for one health scan and application for two scans plus deletion.

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
`mode=rw`, enables foreign keys, uses a bounded writer wait, runs SQLite
`quick_check` and foreign-key validation before and after its immediate
transaction, and deletes only complete selected session state. It refuses an
incomplete schema, unknown session-linked table or trigger, malformed data, or
an exact missing project instead of guessing ownership. It also refuses
non-unique session IDs and known-table foreign keys that cross the selected and
retained ownership boundary. `quick_check` validates table and page structure
but omits the full index/table consistency work of `integrity_check`; run a full
offline SQLite integrity check first when index corruption is suspected.

```bash
opencode-db prune [--db /absolute/path/opencode.db] \
  ([--project-id ID] \
    (--oldest N|TIME | --keep-newest N|TIME | --target-size SIZE) \
    [--estimate-size] [--vacuum] | --vacuum-only) \
  [--timeout-seconds SECONDS] [--work-dir /absolute/private/local-work] [--yes]
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

### Prune failure diagnostics

Prune failures include their execution **phase**, a more specific **step** when
available, and the underlying numeric SQLite or OS error code. For example, a
vacuum failure can now distinguish `SQLITE_BUSY` (lock contention),
`SQLITE_READONLY`, `SQLITE_CANTOPEN`, `SQLITE_FULL`, and extended I/O codes such
as `SQLITE_IOERR_FSYNC`. Failed integrity/foreign-key checks retain their safe
check reason rather than collapsing into just "vacuum failed". Deletion errors
can identify the fixed known table being processed, without SQL or row values.

Diagnostics also show the canonical selected database, configured SQLite
temporary directory, the configured phase timeout, and a targeted next-step
hint. Initial SQLite writer-lock acquisition has a separate **10-second busy
wait**; increasing `--timeout-seconds` does not extend that initial wait. A COMMIT
lock wait consumes only the remaining application budget and is classified as
a timeout even when SQLite's whole-millisecond wait expires just before the
wall-clock deadline.

Storage exhaustion reports byte/inode availability and quota uncertainty at
the selected locations. I/O/open/permission failures also show current
capacity observations where available, without assuming that every I/O failure
means the disk is full. These are observations at selected/configured locations;
SQLite does not always identify the exact file that failed.

If deletions already committed, the committed count and **do not repeat the
prune request; use `--vacuum-only`** warning remain intact. A secondary cleanup
failure is reported separately without masking the original reason. Raw SQLite
messages, OS exception filenames, SQL, session IDs, and row contents are not
printed. If no structured cause is available, the diagnostic explicitly says
so instead of guessing. Additional detail is automatic on stderr and bounded
to 8192 characters; success output and exit classes are unchanged.

### Staged prune and update

`--staged` makes a consistent SQLite backup and performs selection, deletion and
optional vacuum on a **private copy**. Keep using OpenCode on the original while
this long phase runs. It requires an existing mode-0700 **local** `--work-dir`
large enough for the image, sync state and SQLite work. The source must already
use WAL; the tool never changes its journal policy or adds source tables/triggers.
Run it on the same machine/context as the source users and resume on the creating
host; it does not coordinate writers across cluster nodes.

Example retaining the newest 1000 sessions (replace that selector with your
existing policy):

```bash
mkdir -m 700 /absolute/large-local/opencode-db-work
opencode-db prune --db /absolute/path/opencode.db \
  --keep-newest 1000 --vacuum --staged \
  --work-dir /absolute/large-local/opencode-db-work --yes --progress
```

The command prints `operation: prune-…` before the long copy and reports phases
and elapsed time with `--progress` (also automatic on a human terminal). Each
staged phase defaults to a 24-hour finite budget; `--timeout-seconds` can shorten
it. A copied snapshot and completed prune/vacuum stages are retained on failure.
An interrupted individual snapshot, planning scan or vacuum may need repeating;
an already committed deletion is recognized using the exact saved ID selection,
so resumption does not prune another oldest-N batch.

After `state: ready`, catch up new sessions and changed existing sessions while
continuing to use OpenCode:

```bash
opencode-db prune update --db /absolute/path/opencode.db \
  --work-dir /absolute/large-local/opencode-db-work \
  --operation prune-YYYYMMDDTHHMMSSZ-HEX --progress
```

Repeat update as needed. Each update reads session IDs/activity stamps, then
copies complete row families for new sessions or sessions whose
`session.time_updated` **differs** from the last synchronized value (including
clock regression). It propagates source session deletions and synchronizes
non-session metadata. Native sequence-row markers select changed non-session
event histories without rereading every immutable event payload. It does not
reevaluate retention or run another vacuum.

Changed sessions are restored even if the original prune selected them for
deletion. Unchanged pruned sessions stay deleted; necessary existing parent
dependencies of new/restored sessions are also restored. Consequently, catch-up
may exceed the original keep-count or size target. Repeated updates replace
owned rows rather than duplicating messages, parts, events or todos.

**Accepted limitation: timestamp-only sync can miss message/tool/metadata edits
or deletions within a session when its parent activity timestamp is unchanged.**
This is intentionally not a byte-perfect merge or a new change-tracking system.
Source IDs/stamps, schema/migration identity and global metadata bind the staged
source generation; they do not prove equality of every source payload byte.

For final cutover, **stop OpenCode and other source clients**, then run:

```bash
opencode-db prune update --db /absolute/path/opencode.db \
  --work-dir /absolute/large-local/opencode-db-work \
  --operation prune-YYYYMMDDTHHMMSSZ-HEX --install --progress
```

This performs a final timestamp catch-up, verifies/seals the pruned image, stages
it beside the source, rechecks the source generation, retains the original
main/WAL/SHM/journal set privately, and promotes the new image. Only this explicit
`--install` replaces the original. Start OpenCode separately after success.
The tool does not inspect processes, request a shutdown confirmation or coordinate
service lifecycle. Other cooperative staged readers/cutovers are serialized.

The long prune is moved out of the stopped window; final downtime still depends
on changed-session volume, validation and the pruned image's copy/hash time.
Snapshot/update reads consume I/O and can retain source WAL frames until their
read snapshot closes. Keep capacity on the source filesystem too. Installation
needs room for the pruned image there, and keeps original bytes until explicit
retirement; it does not make an extra full-size copy of the original.

Use the same source/work-root selection and printed opaque ID for recovery:

```bash
opencode-db prune status --db /absolute/path/opencode.db \
  --work-dir /absolute/large-local/opencode-db-work --operation prune-YYYYMMDDTHHMMSSZ-HEX
opencode-db prune rollback --db /absolute/path/opencode.db \
  --work-dir /absolute/large-local/opencode-db-work --operation prune-YYYYMMDDTHHMMSSZ-HEX
opencode-db prune discard --db /absolute/path/opencode.db \
  --work-dir /absolute/large-local/opencode-db-work --operation prune-YYYYMMDDTHHMMSSZ-HEX
```

`update` resumes incomplete preparation (`--yes` bypasses only any pending
private-copy authorization). Once cutover starts, repeat `update --install` to
finish it, or explicitly `rollback` with clients stopped. Rollback refuses to
overwrite changed active state or new sidecars. There is no automatic rollback.
`discard` removes only this job's exact artifacts/backups; incomplete cutover or
unlisted files refuse. Retain the work directory until installation/recovery and
backup retirement are finished. These are separate staged job IDs, not cleanup
snapshot/candidate IDs.

Supported staged sources have the native transfer-known session tables and
normal non-session tables, without triggers/virtual tables. Schema or known
migration-state changes require restarting preparation. Private cursor/policy
checksums, candidate hashes, target/host binding and closed versioned state
refuse changed or unknown evidence. Staged metadata is bounded to one million
source sessions, 16 KiB per ID and 128 MiB of session-key evidence; non-session
metadata is bounded to 64 MiB (immutable event payloads are streamed separately).
The native selector's existing scope/evidence limits still apply.

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

Preview and preview-resume default to four hours; installation and installation
recovery default to 30 minutes. `--deadline-seconds` accepts finite whole seconds
from 1 through 86400. SQLite busy waits are finite. The tool requires
private retained storage, admitted local scratch storage, and sufficient space
and inodes before sensitive writes. Recovery can temporarily multiply disk use:
plan capacity for the original source set, retained snapshot, scratch copy,
candidate, staging, and rollback files. Set `--work-dir` or `--scratch-dir` as
described below when the defaults do not have sufficient capacity.

### Work location and capacity diagnostics

All commands accept `--work-dir /absolute/private/directory`. The directory must
already exist with mode `0700`; the tool never silently falls back to another
location. Cleanup uses it for durable recovery evidence, so **keep it until you
have finished installation/recovery and explicitly pruned the retained backup**.

| Storage | Default | With `--work-dir` |
| --- | --- | --- |
| Cleanup snapshots, candidates, reports, catalog, install intent | `<database-parent>/.opencode-db/<target-id>/` | `<work-dir>/.opencode-db/<target-id>/` |
| Cleanup SQLite working copies and verification scratch | `$TMPDIR/opencode-db` (or `/tmp/opencode-db`) | `<work-dir>/scratch` |
| SQLite temporary files for other commands, including prune/vacuum | SQLite's normal environment defaults | `<work-dir>` |
| Final install staging, quarantined originals, rollback staging | Beside the database in its `.opencode-db` tree | Still beside the database |
| Active database and SQLite WAL/journal | Selected database location | Same selected database location |
| Export archive | `--export-dir` | Same `--export-dir` |

Use the **same `--work-dir` on every later cleanup command** (`install`, `status`,
`abort`, `resume`, `rollback`, `prune-backup`). Generated follow-up commands
preserve it, along with an explicit `--scratch-dir` for installation. Artifact
selection uses only the specified store; it does not search or migrate previous
stores. Existing adjacent artifacts remain usable by omitting `--work-dir`.

`--scratch-dir` remains available for cleanup preview/install and overrides only
their local scratch location. For example, retained evidence may live on a large
network filesystem while SQLite work uses a local disk:

```bash
opencode-db cleanup preview \
  --work-dir /absolute/private/retained-evidence \
  --scratch-dir /absolute/local/private/scratch
```

Writable SQLite scratch must be on an admitted local filesystem. For other
commands, `--work-dir` itself must be local; for cleanup, only its selected
scratch must be local. The CLI selects SQLite temporary storage through
`SQLITE_TMPDIR` and `TMPDIR` before command execution. This is a per-invocation
CLI setting; Python API callers manage their own SQLite temporary environment.

For compaction without deleting sessions:

```bash
opencode-db prune --vacuum-only --work-dir /absolute/private/local-work
```

Relocating work reduces additional copies on a full database filesystem, but
cannot eliminate its write-space requirement. Installation stages a complete
candidate there before replacing files, retains quarantined originals there,
and rollback stages original copies there. SQLite's active WAL/journal writes
also stay with the database. Free enough capacity on that filesystem before
installing or compacting; these commands do not move the active database.

Capacity preflight failures identify **disk space**, **inodes**, or **both**,
the checked location, and required/available byte and inode counts. Cleanup
human and JSON diagnostics carry the same information. An unmeasurable
filesystem is reported as a measurement failure rather than as a proven shortage.
Runtime `ENOSPC`, quota, and SQLite `SQLITE_FULL` failures report relevant paths
and current capacity. If current counts cannot establish the cause, the tool
explicitly reports an undetermined resource (quota or temporary exhaustion may
be responsible); it does not guess that bytes or inodes were exhausted.

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
