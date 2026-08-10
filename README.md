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
- Every command requires an exact absolute `--database` path. The tool never
  discovers an OpenCode database and never accepts a relative or in-memory
  target.
- The operator owns shutdown, restart, and concurrent-use safety. Commands run
  immediately: they do not prompt for confirmation, inspect processes, or
  coordinate other users.
- The tool never starts OpenCode. After a successful install, start OpenCode
  separately using its unchanged normal command and configuration.

## Session transfer

Session transfer is an explicit, project-scoped workflow for moving complete
current OpenCode session state between projects known to separate databases. It
does not discover a database, project, or directory. Arrange database shutdown
and concurrent-use safety before starting.

Export from an existing absolute database and existing source project directory:

```bash
opencode-db export --db /absolute/path/opencode.db \
  --project-dir /absolute/source/project \
  --export-dir /absolute/private/exports
```

The command prints the canonical export directory, resolved source project ID,
and one import-file path, never session content. It creates the export directory
as mode `0700` and publishes one SQLite import file as mode `0600` atomically.
The archive contains all rows and columns from `session`, `message`, `part`,
`todo`, `session_message`, `session_input`, `session_context_epoch`,
`session_share`, `event_sequence`, and `event` for the selected project. Global
sessions are included only when their recorded directory matches the selected
project directory.

Import that exact file into an existing target database and project directory:

```bash
opencode-db import --target-project-dir /absolute/target/project \
  --db /absolute/path/target-opencode.db \
  --import /absolute/private/exports/opencode-session-YYYYMMDDTHHMMSSZ-HEX.sqlite
```

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
COMPONENT="${XDG_DATA_HOME:-$HOME/.local/share}/mkchad/components/opencode-db"
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
pinned checkout from `XDG_DATA_HOME`, sets `PYTHONPATH` to only that checkout's
`src`, and executes `python3 -m opencode_db` with unchanged arguments.

## Operator workflow

First arrange OpenCode shutdown if it is needed. Then preview a known absolute
database path. Preview immediately captures the main file and any present
`-wal`, `-shm`, and `-journal` sidecars, works only on a separate scratch copy,
and retains the source snapshot privately beside the target.

```bash
opencode-db cleanup preview --database /absolute/path/opencode.db --json
```

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
opencode-db cleanup install --database /absolute/path/opencode.db \
  --candidate candidate-YYYYMMDDTHHMMSSZ-HEX
```

For an uncertain candidate, copy the exact `report_sha256` from that same
preview result. This is approval of that candidate/report pair, not a broad
override:

```bash
opencode-db cleanup install --database /absolute/path/opencode.db \
  --candidate candidate-YYYYMMDDTHHMMSSZ-HEX \
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
opencode-db cleanup status --database /absolute/path/opencode.db \
  --operation install-YYYYMMDDTHHMMSSZ-HEX --json
opencode-db cleanup resume --database /absolute/path/opencode.db \
  --operation install-YYYYMMDDTHHMMSSZ-HEX
opencode-db cleanup rollback --database /absolute/path/opencode.db \
  --operation install-YYYYMMDDTHHMMSSZ-HEX
```

`status` is read-only. `resume` accepts only the durable recorded before/after
states. `rollback` is always explicit and restores only hash-verified retained
source bytes. For an incomplete preview scratch operation, use its exact
operation ID with `cleanup abort`; cross-host scratch is reported as requiring
manual cleanup rather than removed remotely.

```bash
opencode-db cleanup abort --database /absolute/path/opencode.db \
  --operation operation-YYYYMMDDTHHMMSSZ-HEX
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
opencode-db cleanup prune-backup --database /absolute/path/opencode.db \
  --snapshot snapshot-YYYYMMDDTHHMMSSZ-HEX --json
```

`cleanup prune` is deliberately not implemented. The shorter name is reserved
for a future active-database retention command and does not currently reclaim
space in an OpenCode database.

## Results, deadlines, and storage

Cleanup commands accept `--json` for exactly one newline-terminated
schema-version-1 JSON object on stdout. Top-level `export` and `import` do not
accept `--json`; their separate human-only success output is written to stdout
and their bounded diagnostics to stderr. Exit classes are `0` success, `2`
syntax/input shape, `3` uncertain decision required, `4` safety-precondition
refusal, `5` operational or validation failure, and `6` manual recovery
required.

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
