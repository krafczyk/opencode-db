"""Render bounded prune failure reasons without raw database/exception content.

Diagnostics retain the tool-owned top-level message, especially post-commit
retry warnings, and classify numeric SQLite/OS codes through wrapped causes.
Paths come only from operator-selected locations, never from error messages.
"""

import errno
import math
import os
from pathlib import Path
import sqlite3
from collections.abc import Iterator

from .prune import CHILD_SESSION_TABLES, MAX_PRUNE_TIMEOUT_SECONDS, PruneError, PruneOperationalError, _BUSY_TIMEOUT_MS
from .target import TargetError, _display_path, storage_failure, storage_space

_PHASES = {
    "path": "database path inspection",
    "planning_open": "planning / opening database read-only",
    "planning": "planning / validation and selection",
    "planning_cleanup": "planning / connection cleanup",
    "application_open": "application / opening database for writing",
    "writer_lock": "application / acquiring writer lock",
    "revalidation": "application / revalidating reviewed selection",
    "selection": "application / preparing deletion selection",
    "deleting_children": "application / deleting session-owned rows",
    "deleting_sessions": "application / deleting session rows",
    "post_delete_validation": "application / validating deletions",
    "size_estimation": "application / estimating remaining logical size",
    "commit": "application / committing deletions",
    "empty_selection": "application / finishing empty selection",
    "application_cleanup": "application / connection cleanup",
    "vacuum": "vacuum / physical compaction",
    "preview_output": "planning / writing preview",
    "confirmation_output": "planning / writing confirmation prompt",
}
_STEPS = {
    "quick_check": "SQLite quick integrity check",
    "foreign_key_check": "SQLite foreign-key check",
    "vacuum_rebuild": "SQLite VACUUM rebuild",
    "vacuum_validation": "post-vacuum validation",
    "vacuum_geometry": "reading compacted database page geometry",
    **{f"delete:{table}": f"deleting known table {table}" for table in CHILD_SESSION_TABLES},
}
_SQLITE_REASONS = {
    sqlite3.SQLITE_ERROR: ("SQLite rejected the operation", "Check the reported phase; the numeric SQLite code is retained for diagnosis."),
    sqlite3.SQLITE_BUSY: ("a conflicting database lock could not be acquired", "Release conflicting database transactions before retrying; this command does not stop other clients."),
    sqlite3.SQLITE_LOCKED: ("a table or shared-cache lock blocked the operation", "Release conflicting database transactions before retrying."),
    sqlite3.SQLITE_READONLY: ("SQLite could not write to read-only storage", "Check write access to the database and its directory, including WAL/journal files, and the temporary directory."),
    sqlite3.SQLITE_CANTOPEN: ("SQLite could not open a required database or temporary file", "Check directory existence, access permissions and filesystem availability at the locations below."),
    sqlite3.SQLITE_IOERR: ("a filesystem I/O operation failed", "Check database and temporary filesystem availability, quota and filesystem errors."),
    sqlite3.SQLITE_FULL: ("SQLite exhausted storage or a storage limit", "Check free bytes, inodes and quota. --work-dir moves temporary files, not the active database or its WAL/journal."),
    sqlite3.SQLITE_CORRUPT: ("SQLite detected database corruption", "Validate/recover the database before attempting further pruning."),
    sqlite3.SQLITE_NOTADB: ("the selected file is not a readable SQLite database", "Check the selected --db path and database validity before pruning."),
    sqlite3.SQLITE_CONSTRAINT: ("a database constraint prevented the operation", "Check database relationships and schema compatibility before retrying."),
    sqlite3.SQLITE_INTERRUPT: ("SQLite execution was interrupted", "Check the phase timeout or cancellation; completed deletions remain committed if reported above."),
    sqlite3.SQLITE_NOMEM: ("SQLite could not allocate memory", "Check process/job memory limits and available memory."),
    sqlite3.SQLITE_PERM: ("SQLite denied access", "Check access to the database, its directory and temporary storage."),
}
_OS_REASONS = {
    errno.EACCES: ("filesystem access was denied", "Check database, database-directory and temporary-directory permissions."),
    errno.EPERM: ("the filesystem operation was not permitted", "Check filesystem permissions and mount policy."),
    errno.EROFS: ("a required filesystem is read-only", "Check the database and temporary filesystem mounts."),
    errno.EIO: ("a filesystem I/O operation failed", "Check database and temporary filesystem availability and filesystem errors."),
    errno.ENOENT: ("a required path disappeared or does not exist", "Check the database and temporary directory locations."),
    errno.ENOSPC: ("storage space or inodes were exhausted", "Check the capacity observations below and applicable quotas."),
    errno.EDQUOT: ("a filesystem quota was exceeded", "Check your byte and inode quotas on the relevant filesystems."),
    errno.EMFILE: ("the process open-file limit was reached", "Check the process/job open-file limit."),
    errno.ENFILE: ("the system open-file limit was reached", "Check host filesystem resource limits."),
    errno.ENOMEM: ("memory allocation failed", "Check process/job memory limits and available memory."),
    errno.ESTALE: ("a network filesystem handle became stale", "Check the mount and database path before retrying."),
    errno.ETIMEDOUT: ("a filesystem operation timed out", "Check filesystem or mount availability."),
    errno.EPIPE: ("the output pipe was closed", "Keep the output destination open; a prune preview must be delivered before deletion."),
}
_PUBLIC_REASONS = {
    "database integrity check failed": "The database failed SQLite's quick integrity check; validate/recover it before pruning.",
    "database foreign key check failed": "The database failed its foreign-key check; resolve inconsistent relationships before pruning.",
    "prune candidate evidence exceeds supported bounds": "The reviewed selection exceeds the supported candidate/evidence limits; use a narrower project scope.",
    "database session schema is incompatible": "The session schema is incompatible with the supported pruning table map.",
    "database session schema is incomplete": "The database is missing required session schema elements.",
    "database session schema is invalid": "The session schema failed supported shape checks.",
    "unknown session-linked table": "An unsupported session-linked table exists; pruning cannot safely delete its state.",
}
_TIMEOUTS = {
    "prune operation timed out", "prune planning timed out", "prune application timed out",
    "database vacuum timed out",
    "sessions were pruned but vacuum timed out; do not repeat the prune request; rerun with --vacuum-only",
}


def describe_prune_failure(error: PruneOperationalError, database: str | None, work_dir: str | None = None) -> str:
    """Explain one prune failure with safe phase, cause, locations and next steps.

    ``error`` is a tool-owned operational/committed error; ``database`` and
    ``work_dir`` are selected paths (``None`` retains environment defaults).
    Returns at most 8192 characters, preserving the original message first.
    Numeric codes are resolved against standard-library constants, never raw
    SQLite names/messages or OS filenames. Reads statvfs only for storage
    exhaustion and filesystem/access failures; never opens databases, inspects
    processes or mutates state.
    """
    chain = list(_causes(error))
    database = next((item.database_path for item in chain if isinstance(item, PruneOperationalError) and item.database_path is not None), database)
    lines = [str(error)]
    phase = next((item.phase for item in chain if isinstance(item, PruneOperationalError) and item.phase in _PHASES), None)
    step = next((item.step for item in reversed(chain) if isinstance(item, PruneOperationalError) and item.step in _STEPS), None)
    if phase is not None:
        lines.append("  phase: " + _PHASES[phase])
    if step is not None:
        lines.append("  step: " + _STEPS[step])
    budget = next((item.timeout_seconds for item in chain if isinstance(item, PruneOperationalError) and _valid_budget(item.timeout_seconds)), None)
    if budget is not None:
        lines.append(f"  configured phase timeout: {budget:g}s (--timeout-seconds)")
    if phase == "writer_lock":
        lines.append(f"  configured SQLite busy-wait limit: {_BUSY_TIMEOUT_MS / 1000:g}s")
    cause = _coded_cause(chain)
    if cause:
        reason, hint = cause
        lines.append("  reason: " + reason)
        if phase not in {"preview_output", "confirmation_output"}:
            lines.append("  hint: " + hint)
    if phase in {"preview_output", "confirmation_output"}:
        lines.append("  hint: Check the output destination (pipe, log or terminal); preview/prompt delivery must finish before deletion can start.")
    for item in reversed(chain):
        if isinstance(item, PruneError) and str(item) in _PUBLIC_REASONS:
            lines.append("  check: " + _PUBLIC_REASONS[str(item)])
            break
    if any(isinstance(item, PruneOperationalError) and str(item) in _TIMEOUTS for item in chain):
        lines.append("  timeout: the operation's time budget expired; increase --timeout-seconds for long work, or resolve lock contention if reported.")
    temporary = Path(work_dir or os.environ.get("SQLITE_TMPDIR", os.environ.get("TMPDIR", "/tmp")))
    paths = [Path(database).parent] if database else []
    if database:
        lines.append("  database: " + _display_path(Path(database)))
    lines.append("  SQLite temporary directory: " + _display_path(temporary) + " (configured)")
    paths.append(temporary)
    capacity = storage_failure(error, *paths)
    if capacity:
        lines.append("  storage: " + capacity)
    elif _filesystem_failure(chain):
        for location in dict.fromkeys(paths):
            try:
                space = storage_space(location)
                lines.append(
                    f"  capacity now at {_display_path(location)}: "
                    f"bytes available={space.available_bytes}; inodes available={space.available_inodes}"
                )
            except TargetError:
                lines.append(f"  capacity unavailable at {_display_path(location)}")
    additional = next((item.cleanup_error for item in chain if isinstance(item, PruneOperationalError) and item.cleanup_error is not None), None)
    if additional is not None:
        extra = _coded_cause(list(_causes(additional)))
        lines.append("  additional cleanup failure: " + (extra[0] if extra else "SQLite connection cleanup failed; raw details withheld"))
        extra_capacity = storage_failure(additional, *paths)
        if extra_capacity and extra_capacity != capacity:
            lines.append("  cleanup storage: " + extra_capacity)
    if cause is None and not any(str(item) in _PUBLIC_REASONS or str(item) in _TIMEOUTS for item in chain if isinstance(item, PruneError)):
        lines.append("  reason: no structured SQLite/OS cause was supplied; raw exception text is withheld")
    return "\n".join(lines)[:8192]


def _valid_budget(value: object) -> bool:
    """Accept only finite tool-supported timeout numbers for rendering."""
    return type(value) in (int, float) and 0 < value <= MAX_PRUNE_TIMEOUT_SECONDS and math.isfinite(value)


def _filesystem_failure(chain: list[BaseException]) -> bool:
    """Select capacity observations without guessing that an I/O error means full storage."""
    for error in chain:
        if isinstance(error, OSError) and error.errno in {
            errno.EIO, errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ESTALE,
            errno.ETIMEDOUT, errno.ENOSPC, errno.EDQUOT,
        }:
            return True
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(error, sqlite3.Error) and type(code) is int and code & 255 in {
            sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM,
        }:
            return True
    return False


def _causes(error: BaseException) -> Iterator[BaseException]:
    """Walk a bounded explicit-cause chain without trusting arbitrary messages."""
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        yield current
        current = current.__cause__


def _coded_cause(chain: list[BaseException]) -> tuple[str, str] | None:
    """Prefer the deepest numeric SQLite/OS cause, never provider-supplied names."""
    for item in reversed(chain):
        code = getattr(item, "sqlite_errorcode", None)
        if isinstance(item, sqlite3.Error) and type(code) is int and 0 < code < 2**31:
            primary = code & 255
            base_name = next((name for name in (
                "SQLITE_ERROR", "SQLITE_INTERNAL", "SQLITE_PERM", "SQLITE_ABORT", "SQLITE_BUSY", "SQLITE_LOCKED",
                "SQLITE_NOMEM", "SQLITE_READONLY", "SQLITE_INTERRUPT", "SQLITE_IOERR", "SQLITE_CORRUPT", "SQLITE_NOTFOUND",
                "SQLITE_FULL", "SQLITE_CANTOPEN", "SQLITE_PROTOCOL", "SQLITE_EMPTY", "SQLITE_SCHEMA", "SQLITE_TOOBIG",
                "SQLITE_CONSTRAINT", "SQLITE_MISMATCH", "SQLITE_MISUSE", "SQLITE_NOLFS", "SQLITE_AUTH", "SQLITE_FORMAT", "SQLITE_RANGE", "SQLITE_NOTADB",
            ) if getattr(sqlite3, name, None) == primary), None)
            extended = next((name for name, value in vars(sqlite3).items() if base_name and name.startswith(base_name + "_") and value == code), None) if code > 255 else None
            name = extended or base_name or "SQLite error"
            reason, hint = _SQLITE_REASONS.get(primary, ("SQLite could not complete the operation", "Use the reported phase and numeric SQLite code to diagnose the failure."))
            return f"{name} ({code}): {reason}", hint
        if isinstance(item, OSError) and type(item.errno) is int and 0 < item.errno < 65536:
            reason, hint = _OS_REASONS.get(item.errno, ("an operating-system operation failed", "Check filesystem availability and process/job limits at the selected locations."))
            return f"{errno.errorcode.get(item.errno, 'OS error')} ({item.errno}): {reason}", hint
    return None
