# OpenCode Database Cleanup Tool

## Environment

`opencode-db` is an independent Python 3.11+ child repository in the MkChad
development workspace. It inherits the parent workspace policy in
`../AGENTS.md`. Its development short name is `opencode-db-v1`; disposable
build products, test artifacts, downloaded development tools, and temporary
files belong beneath `/tmp/opencode-db-v1`.

Use only the Python standard library at runtime. Keep the package in `src/`,
run focused standard-library tests from this child, and do not install
undeclared dependencies merely to make an optional build gate available.

## Protected State And Actors

Protected state includes operator-selected OpenCode SQLite databases, every
standard database sidecar, retained source snapshots, candidates, reports,
installation intent logs, target catalogs, credentials, prompts, session data,
and unrelated worktree contents. The operator and explicitly invoked local
tooling are trusted. Other agents sharing the worktree are cooperative but can
change files concurrently. Other local users, remote services, package
registries, external input, and data returned by OpenCode are untrusted.

The tool accepts an explicit absolute database path or the documented bounded
XDG/HOME default. It must not discover a target through OpenCode, inspect
processes, request a shutdown confirmation, or automatically run at OpenCode
startup or shutdown. Explicit values remain absolute and non-memory; default
selection does not probe paths before command-specific validation.

## Filesystem And Concurrency Assumptions

Supported hosts are Linux HPC systems, including NFS, Lustre, and GPFS. Mount
semantics, inode identity, ownership presentation, and atomicity can differ
from local ext4. Treat active databases, retained artifacts, and local scratch
as separate trust boundaries. The operator arranges service shutdown and
concurrent-use safety; this repository neither verifies nor coordinates them.
Commands must fail closed on malformed input, unknown persisted schemas,
ambiguous selection, unavailable prerequisites, and source changes at a
mutation boundary. Do not create or open a replacement empty or in-memory
database as fallback.

## Scope And Exclusions

In scope are explicit target selection, bounded diagnostics, private artifact
handling, normal SQLite recovery on copies, validation, explicit installation,
bounded recovery, and exact retained-backup pruning as those units are
implemented. General SQLite salvage, OpenCode runtime changes, journal-policy
changes, process inspection, cross-node leases, automatic cleanup, automatic
rollback, retention expiry, and active-database `prune` behavior are excluded.

Do not inspect live MkChad state, user credentials, OpenCode session data, or
private database fixtures unless the current task explicitly authorizes it.

## Verification And Git

Run `python3 -m unittest discover -s tests -v` and compile checks for changes
to this repository. Run one resource-intensive suite at a time. This child has
independent Git history; inspect its status before any commit and do not modify
the parent gitlink until the coordinating agent directs that integration.
