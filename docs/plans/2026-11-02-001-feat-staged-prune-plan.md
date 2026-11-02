# Staged pruning with timestamp catch-up

## Settled requirements

- Retain existing in-place pruning. Add an opt-in `prune --staged` workflow.
- Create a consistent SQLite backup of the explicitly selected WAL database;
  prune/compact only that private copy while OpenCode continues using the source.
- `prune update` copies new sessions and replaces complete row families for
  sessions whose `time_updated` differs from the last synchronized snapshot.
- Restore changed sessions even if the initial prune deleted them. Keep
  unchanged pruned sessions deleted. Propagate source session deletions and
  preserve parent dependencies needed by restored/new sessions.
- Synchronize non-session metadata as well. Never append duplicate child rows
  or reapply the original retention policy during catch-up.
- No source tracking objects, triggers, journal-policy changes, dependencies,
  runtime changes, process inspection, or lifecycle coordination.
- The user explicitly accepts timestamp-only detection: edits that do not
  advance/change the parent session's activity timestamp can be missed. This
  limitation applies to the staged synchronization contract, not legacy cleanup
  provenance or the existing in-place prune transaction.
- `update --install` is explicit. The operator stops OpenCode for the final
  catch-up/cutover. Preserve the original file set and durable rename intent;
  support explicit recovery/rollback without overwriting newer active state.
- Use the existing `--work-dir` private local filesystem admission. Print an
  operation ID early and provide phase timing/progress and exact next commands.

## Implementation boundaries

Private job state records schema/target/host identity and source session stamps.
Candidate and synchronization state commit together using SQLite's supported
rollback-journal multi-database transaction. Do not install staging metadata
into the user's database. Validate candidate schema, quick integrity and foreign
keys and seal candidate bytes before reuse or installation.

Global metadata is copied consistently with the same source read transaction.
Non-session event aggregates are preserved; session-owned payloads are selected
only for new/changed/restored sessions. Unknown schemas, triggers, virtual
tables, invalid ownership, changed candidates and ambiguous recovery refuse.

Cutover uses this mode's explicitly accepted logical source generation
(schema, session IDs/timestamps and non-session metadata), not a claim that all
source bytes were compared. Originals are retained without a full-size extra
copy. The existing cleanup snapshot/report/install protocol remains unchanged.

## Verification

Generated WAL fixtures cover concurrent source writes, changed/deleted/new
sessions, unchanged pruned sessions, restoration, clock regression/equality,
parent closure, global metadata, repeat updates and rollback. Verify source
rows/schema remain untouched, unknown/changed evidence refuses, update failure
is atomic, and every cutover rename boundary is recoverable. Finish with the
child full suite, compile checks and installed-copy smoke tests.

## Implementation notes

- Two explicit command stages are `prune --staged` and `prune update`; the final
  update optionally includes `--install`. Status, rollback and exact discard
  select the same private job by canonical source, work root and opaque ID.
- Source WAL is a prerequisite, not a policy the tool enables. SQLite backup
  reads a single consistent transaction; the long prune/vacuum holds no source
  transaction. Updates release their source read snapshot after copying rows,
  before local validation/sealing.
- Staged phases use the existing finite 86400-second maximum by default. Phase
  timings and update counts are observable without session contents/identifiers.
- Private cursor/selection checksums and candidate hashes protect resumption.
  Attached rollback-journal commits keep rows and sync acknowledgement together.
- Non-session immutable event histories follow native sequence-row markers;
  metadata scans do not read all historical event payloads. Known migration
  receipts and application/user version join schema identity to reject data-only
  migrations that timestamp-only catch-up cannot safely apply.
- Shared source locks protect cooperating staged snapshots/updates; exclusive
  source locks serialize cutover/rollback across jobs. They do not coordinate
  native OpenCode, so the operator owns final shutdown.
- Cutover records a closed rename intent, retains the original file set without
  a full-size extra original copy, rechecks the accepted source generation,
  rejects newly appearing sidecars and promotes sealed candidate bytes. Captured
  original SHM is kept separately from a disposable retained-source read cache.
- Explicit rollback refuses new active work, and discard refuses unfinished
  recovery or unlisted artifacts. The accepted timestamp limitation remains;
  neither these checks nor backups are described as fixing it.

## Verification results

- All 252 repository tests passed, including 32 staged-prune tests using
  generated WAL databases and fresh-process interruption/recovery checks.
- Rollback preflights every original/active file before moving the main database;
  regression coverage verifies that unexpected WAL/journal files preserve both
  the retained original and the unexpected sidecar without restoring the main.
- Ruff, Python compile checks and `git diff --check` passed.
- Installed-command smoke verification found a staged subcommand help-routing
  defect; the fix and all four staged help routes now have regression coverage.
- The full suite emits existing Python 3.14 unclosed-SQLite `ResourceWarning`s;
  the focused staged suite passes without those warnings when run independently.
