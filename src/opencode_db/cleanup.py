"""Create sidecar-free candidates through normal SQLite recovery on copies.

This module consumes only accepted retained snapshots. SQLite never opens an
active or retained source path: it opens a fresh local scratch copy whose SHM
file is intentionally absent, allowing SQLite to rebuild its WAL index.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .artifacts import (
    CaptureError,
    bind_preview_scratch,
    finish_preview_operation,
    load_accepted_snapshot,
    load_preview_candidate,
    persist_candidate,
    register_preview_scratch,
    preview_operation_lock,
    validate_preview_scratch,
    snapshot_source_path,
)
from .model import AcceptedSnapshot, Check, CleanupOutcome, Validation
from .preview_state import checkpoint_files, open_checkpoint
from .target import (
    TargetEnvironment,
    TargetError,
    admit_scratch,
    ensure_private_directory,
    storage_failure,
)

WAL_HEADER_BYTES = 32
"""Fixed byte length of an SQLite WAL header."""

WAL_FRAME_HEADER_BYTES = 24
"""Fixed byte length preceding each SQLite WAL frame page."""

MAX_PREVIEW_ROWS = 4
"""Maximum number of recent session records exposed in a preview."""

MAX_PREVIEW_TEXT = 512
"""Maximum UTF-8 character count exposed for one recognizable field."""

DEFAULT_PREVIEW_DEADLINE_SECONDS = 14_400
"""Four-hour default preview/resume budget; explicit deadlines remain bounded."""


class CleanupError(RuntimeError):
    """Report one bounded normal-recovery failure without raw SQLite details.

    Parameters: ``code`` is a stable safe classification string. The exception
    is caught by :func:`clean_snapshot`; it does not write source or retained
    input files.
    """

    def __init__(self, code: str) -> None:
        """Store the safe failure ``code`` without arbitrary database content."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CleanupHooks:
    """Provide narrow deterministic interruption seams for cleanup tests.

    ``checkpoint`` receives only a scratch SQLite connection and checkpoint mode;
    ``before_backup`` and ``before_reopen`` receive only scratch candidate paths.
    Hooks may raise an exception to simulate interruption. Production callers use
    the default empty hooks. The hooks never receive a source or retained path.
    """

    checkpoint: Callable[[sqlite3.Connection, str], tuple[int, int, int]] | None = None
    before_backup: Callable[[Path], None] | None = None
    before_reopen: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class _WalEvidence:
    """Hold bounded read-only WAL layout evidence used for classification."""

    present: bool
    malformed: bool
    frame_count: int
    trailing_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Return a bounded report representation without reading any files."""
        return {
            "present": self.present,
            "malformed": self.malformed,
            "frame_count": self.frame_count,
            "trailing_bytes": self.trailing_bytes,
        }


def clean_snapshot(
    snapshot_dir: Path,
    *,
    scratch_dir: str,
    deadline_seconds: int = DEFAULT_PREVIEW_DEADLINE_SECONDS,
    environment: TargetEnvironment | None = None,
    hooks: CleanupHooks | None = None,
    progress: Callable[[str], None] | None = None,
) -> CleanupOutcome:
    """Recover, validate, and retain one candidate from an accepted snapshot.

    Parameters: ``snapshot_dir`` names an exact retained source group,
    ``scratch_dir`` is an admitted local writable root, ``deadline_seconds`` is
    a finite overall bound, ``environment`` provides testable scratch probes,
    and ``hooks`` supplies narrow scratch-only interruption seams. ``progress``
    receives bounded stage names, including completed checkpoint notifications.
    Returns a
    :class:`CleanupOutcome` classified ``complete``, ``uncertain``, or invalid.
    Raises no SQLite error to callers: malformed inputs and recovery failures
    become non-installable outcomes. The function rehashes retained inputs,
    writes SQLite only in scratch, and persists a private candidate/report only
    after all validation and digest checks pass. Completed stages are fsynced in
    registered scratch and rehashed before resumption. Timeout, interruption and
    recoverable I/O failures retain scratch and return a resumable operational
    outcome. Invalid evidence is refused; simultaneous resume/abort is excluded
    by a nonblocking operation lock. Active source paths are never opened.
    """
    try:
        with preview_operation_lock(snapshot_dir):
            return _clean_snapshot(
                snapshot_dir, scratch_dir=scratch_dir, deadline_seconds=deadline_seconds,
                environment=environment, hooks=hooks, progress=progress,
            )
    except CaptureError as error:
        return _invalid(
            snapshot_dir.name, "unknown", Validation(),
            error.code,
        )
    except OSError as error:
        return replace(
            _invalid(
                snapshot_dir.name, "unknown", Validation(), "preview_failed",
                diagnostic=storage_failure(error, snapshot_dir, Path(scratch_dir)),
            ),
            status="operational_failure", resumable=True,
        )


def _clean_snapshot(
    snapshot_dir: Path, *, scratch_dir: str, deadline_seconds: int,
    environment: TargetEnvironment | None, hooks: CleanupHooks | None,
    progress: Callable[[str], None] | None,
) -> CleanupOutcome:
    """Run or resume one preview while holding its durable operation lock."""
    started = time.monotonic()
    validation = Validation()
    snapshot_id = "unknown"
    target = "unknown"
    snapshot: AcceptedSnapshot | None = None
    workdir: Path | None = None
    operation_finished = False
    retain_work = False

    def announce(stage: str) -> None:
        if progress is not None:
            progress(stage)

    try:
        announce("verifying snapshot")
        if deadline_seconds < 1:
            raise CleanupError("deadline_exceeded")
        snapshot = load_accepted_snapshot(
            snapshot_dir, started=started, deadline_seconds=deadline_seconds
        )
        snapshot_id = snapshot.snapshot_id
        target = snapshot.manifest.target
        _check_deadline(started, deadline_seconds)
        existing = load_preview_candidate(snapshot, started=started, deadline_seconds=deadline_seconds)
        if existing is not None:
            retained, report_path, report, report_digest = existing
            validation = Validation(**{key: Check(value) for key, value in report["validation"].items()})
            if validation != Validation(Check.PASS, Check.PASS, Check.PASS, Check.PASS, Check.PASS):
                raise CleanupError("preview_checkpoint_invalid")
            completeness = str(report["completeness"])
            operation_dir = Path(scratch_dir) / snapshot.operation_id
            if operation_dir.exists():
                validate_preview_scratch(snapshot, operation_dir)
                shutil.rmtree(operation_dir)
            finish_preview_operation(snapshot, completeness)
            operation_finished = True
            return CleanupOutcome(
                snapshot.snapshot_id, snapshot.manifest.target, completeness, completeness,
                validation, True, candidate_id=str(report["candidate_id"]),
                candidate_path=retained, report_path=report_path, report_sha256=report_digest,
                preview=report["preview"],
            )
        _refuse_journal(snapshot)
        evidence = _inspect_wal(snapshot)
        if evidence.malformed:
            raise CleanupError("cleanup_invalid")
        workdir = _create_scratch(
            Path(scratch_dir), snapshot, environment, started, deadline_seconds
        )
        scratch_main = workdir / "source.sqlite"
        checkpoint_state = open_checkpoint(snapshot, workdir)
        announce("verifying saved work")
        checkpoint_state.verify(started, deadline_seconds)
        if checkpoint_state.stage in {"captured", "recovering"}:
            announce("copying snapshot to scratch")
            _remove_scratch_files(workdir, "source.sqlite", "candidate.sqlite")
            _copy_inputs(snapshot, scratch_main, started, deadline_seconds)
            copied = [scratch_main]
            if Path(f"{scratch_main}-wal").exists():
                copied.append(Path(f"{scratch_main}-wal"))
            checkpoint_state.save(
                "copied", files=checkpoint_files(copied, started, deadline_seconds),
                passive_checkpoint=None, truncate_checkpoint=None,
            )
            announce("saved copied")
        if not checkpoint_state.reached("recovered"):
            checkpoint_state.save("recovering", files={})
            announce("recovering WAL")
            source = _connect_rw(scratch_main)
            try:
                _set_deadline_handler(source, started, deadline_seconds)
                _check_deadline(started, deadline_seconds)
                passive_checkpoint = _checkpoint(source, "PASSIVE", hooks)
                _check_deadline(started, deadline_seconds)
                truncate_checkpoint = _checkpoint(source, "TRUNCATE", hooks)
                if truncate_checkpoint not in ((0, 0, 0), (0, -1, -1)):
                    validation = Validation(checkpoint=Check.FAIL)
                    raise CleanupError("checkpoint_incomplete")
            finally:
                source.close()
            _sync_file(scratch_main)
            checkpoint_state.save(
                "recovered", files=checkpoint_files([scratch_main], started, deadline_seconds),
                passive_checkpoint=list(passive_checkpoint), truncate_checkpoint=list(truncate_checkpoint),
            )
            announce("saved recovered")
        passive_checkpoint = tuple(checkpoint_state.value["passive_checkpoint"])
        truncate_checkpoint = tuple(checkpoint_state.value["truncate_checkpoint"])
        candidate = workdir / "candidate.sqlite"
        if not checkpoint_state.reached("candidate"):
            announce("creating candidate")
            _remove_scratch_files(workdir, "candidate.sqlite")
            source = sqlite3.connect(f"{scratch_main.as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                if hooks and hooks.before_backup:
                    hooks.before_backup(candidate)
                _backup(source, candidate, started, deadline_seconds)
            finally:
                source.close()
            _sync_file(candidate)
            checkpoint_state.save("candidate", files=checkpoint_files([candidate], started, deadline_seconds))
            announce("saved candidate")
        completed = Validation(
            integrity=Check.PASS if checkpoint_state.reached("integrity") else Check.NOT_RUN,
            foreign_keys=Check.PASS if checkpoint_state.reached("foreign_keys") else Check.NOT_RUN,
            checkpoint=Check.PASS,
        )

        def checked(stage: str) -> None:
            checkpoint_state.save(stage)
            announce(f"saved {stage}")

        if not checkpoint_state.reached("validated"):
            validation, preview = _validate_candidate(
                candidate, started, deadline_seconds, hooks, Check.PASS,
                completed=completed, checked=checked, progress=announce,
            )
        else:
            validation = Validation(Check.PASS, Check.PASS, Check.PASS, Check.PASS, Check.PASS)
            preview = checkpoint_state.value["preview"]
        if validation != Validation(
            integrity=Check.PASS,
            foreign_keys=Check.PASS,
            checkpoint=Check.PASS,
            clean_reopen=Check.PASS,
            sidecars_absent=Check.PASS,
        ):
            return _invalid(snapshot_id, target, validation, "cleanup_invalid", preview)
        if not checkpoint_state.reached("validated"):
            checkpoint_state.save("validated", preview=preview)
            announce("saved validated")
        _check_deadline(started, deadline_seconds)
        completeness = _classify(snapshot, evidence)
        candidate_id = checkpoint_state.value["candidate_id"]
        announce("publishing candidate")
        retained, report_path, report_digest = persist_candidate(
            snapshot,
            candidate,
            candidate_id,
            _report(
                snapshot,
                completeness,
                validation,
                evidence,
                passive_checkpoint,
                truncate_checkpoint,
                preview,
            ),
            started=started,
            deadline_seconds=deadline_seconds,
            retry=True,
        )
        checkpoint_state.save("published")
        announce("saved published")
        finish_preview_operation(snapshot, completeness)
        operation_finished = True
        shutil.rmtree(workdir)
        workdir = None
        return CleanupOutcome(
            snapshot_id=snapshot.snapshot_id,
            target=snapshot.manifest.target,
            status=completeness,
            completeness=completeness,
            validation=validation,
            installable=True,
            candidate_id=candidate_id,
            candidate_path=retained,
            report_path=report_path,
            report_sha256=report_digest,
            preview=preview,
        )
    except (CaptureError, CleanupError, TargetError, OSError, sqlite3.Error, KeyboardInterrupt) as error:
        code = (
            error.code
            if isinstance(error, (CaptureError, CleanupError, TargetError))
            else "preview_interrupted" if isinstance(error, (InterruptedError, KeyboardInterrupt))
            else "cleanup_invalid"
        )
        if time.monotonic() - started >= deadline_seconds:
            code = "deadline_exceeded"
        retain_work = code in {"deadline_exceeded", "preview_interrupted"} or isinstance(error, OSError) or storage_failure(error, Path(scratch_dir), snapshot_dir) is not None
        # Invalid or foreign checkpoint evidence is preserved for inspection,
        # never treated as a reusable validation receipt or deleted automatically.
        preserve_invalid = code in {
            "preview_checkpoint_invalid", "snapshot_invalid", "preview_not_resumable",
            "candidate_changed", "report_changed", "artifact_schema_unsupported", "artifact_not_private",
        }
        retain_work |= preserve_invalid
        outcome = _invalid(
            snapshot_id, target, validation, code,
            diagnostic=storage_failure(error, Path(scratch_dir), snapshot_dir),
        )
        if retain_work and not preserve_invalid:
            return replace(outcome, status="operational_failure", resumable=True)
        return outcome
    except Exception:
        retain_work = True
        return replace(
            _invalid(snapshot_id, target, validation, "preview_failed"),
            status="operational_failure", resumable=True,
        )
    finally:
        if workdir is not None and not retain_work:
            shutil.rmtree(workdir, ignore_errors=True)
        if snapshot is not None and not operation_finished and not retain_work:
            try:
                finish_preview_operation(snapshot, "invalid")
            except CaptureError:
                pass


def _remove_scratch_files(directory: Path, *names: str) -> None:
    """Remove only fixed partial SQLite outputs inside marker-authorized scratch."""
    for name in names:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = directory / f"{name}{suffix}"
            if path.is_symlink() or path.exists() and not path.is_file():
                raise CleanupError("preview_checkpoint_invalid")
            path.unlink(missing_ok=True)


def _sync_file(path: Path) -> None:
    """Make a closed SQLite stage durable before writing its checkpoint receipt."""
    with path.open("rb") as reader:
        os.fsync(reader.fileno())


def _create_scratch(
    root: Path,
    snapshot: AcceptedSnapshot,
    environment: TargetEnvironment | None,
    started: float,
    deadline_seconds: int,
) -> Path:
    """Create one private admitted scratch workspace for a copied source set."""
    required = sum(item.size or 0 for item in snapshot.manifest.files) * 3 + 65536
    workdir = root.resolve() / snapshot.operation_id
    if workdir.exists() or workdir.is_symlink():
        validate_preview_scratch(snapshot, workdir)
        admit_scratch(root, 0, 0, environment)
        return workdir
    admitted = admit_scratch(root, required, 8, environment)
    ensure_private_directory(admitted)
    _check_deadline(started, deadline_seconds)
    workdir = admitted / snapshot.operation_id
    register_preview_scratch(snapshot, workdir)
    ensure_private_directory(workdir)
    bind_preview_scratch(snapshot, workdir)
    return workdir


def _copy_inputs(
    snapshot: AcceptedSnapshot,
    scratch_main: Path,
    started: float,
    deadline_seconds: int,
) -> None:
    """Copy only retained main/WAL bytes into SQLite's fresh scratch namespace."""
    names = (("main", scratch_main), ("wal", Path(f"{scratch_main}-wal")))
    files = {item.name: item for item in snapshot.manifest.files}
    for name, destination in names:
        source_file = files[name]
        if not source_file.present:
            continue
        _copy_verified(
            snapshot_source_path(snapshot.snapshot_dir, snapshot.manifest, name),
            destination,
            source_file.size,
            source_file.sha256,
            started,
            deadline_seconds,
        )


def _copy_verified(
    source: Path,
    destination: Path,
    expected_size: int | None,
    expected_digest: str | None,
    started: float,
    deadline_seconds: int,
) -> None:
    """Stream one retained input into scratch and require its expected digest."""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        os.chmod(destination, 0o600)
        while chunk := reader.read(1024 * 1024):
            _check_deadline(started, deadline_seconds)
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if (size, digest.hexdigest()) != (expected_size, expected_digest):
        raise CleanupError("snapshot_invalid")


def _inspect_wal(snapshot: AcceptedSnapshot) -> _WalEvidence:
    """Inspect bounded WAL header and frame layout evidence without decoding frames."""
    wal = next(item for item in snapshot.manifest.files if item.name == "wal")
    if not wal.present:
        return _WalEvidence(False, False, 0, 0)
    try:
        with snapshot_source_path(snapshot.snapshot_dir, snapshot.manifest, "wal").open(
            "rb"
        ) as reader:
            header = reader.read(WAL_HEADER_BYTES)
    except OSError as error:
        raise CleanupError("snapshot_invalid") from error
    if len(header) != WAL_HEADER_BYTES:
        return _WalEvidence(True, True, 0, 0)
    magic = int.from_bytes(header[0:4], "big")
    page_size = int.from_bytes(header[8:12], "big")
    if magic not in (0x377F0682, 0x377F0683) or page_size == 1:
        page_size = 65536 if page_size == 1 else page_size
    if magic not in (0x377F0682, 0x377F0683) or page_size not in {
        512,
        1024,
        2048,
        4096,
        8192,
        16384,
        32768,
        65536,
    }:
        return _WalEvidence(True, True, 0, 0)
    payload = (wal.size or 0) - WAL_HEADER_BYTES
    frame_bytes = WAL_FRAME_HEADER_BYTES + page_size
    return _WalEvidence(True, False, payload // frame_bytes, payload % frame_bytes)


def _connect_rw(path: Path) -> sqlite3.Connection:
    """Open one existing scratch database with SQLite URI create protection."""
    return sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=1.0)


def _checkpoint(
    connection: sqlite3.Connection, mode: str, hooks: CleanupHooks | None
) -> tuple[int, int, int]:
    """Run one checkpoint and return its bounded busy/logged/checkpointed row."""
    row = (
        hooks.checkpoint(connection, mode)
        if hooks and hooks.checkpoint
        else _checkpoint_row(connection, mode)
    )
    if (
        not isinstance(row, tuple)
        or len(row) != 3
        or any(type(value) is not int for value in row)
        or row[0] < 0
        or row[1] < -1
        or row[2] < -1
        or (row[1] == -1) != (row[2] == -1)
    ):
        raise CleanupError("checkpoint_incomplete")
    return row


def _checkpoint_row(connection: sqlite3.Connection, mode: str) -> object:
    """Return SQLite's checkpoint row while normalizing database failures."""
    try:
        return connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    except sqlite3.Error as error:
        raise CleanupError("checkpoint_incomplete") from error


def _backup(
    source: sqlite3.Connection,
    candidate: Path,
    started: float,
    deadline_seconds: int,
) -> None:
    """Materialize SQLite's recovered view using its backup API into scratch."""
    destination = sqlite3.connect(candidate)
    try:
        source.backup(
            destination,
            pages=64,
            progress=lambda _status, _remaining, _total: _check_deadline(
                started, deadline_seconds
            ),
        )
    except sqlite3.Error as error:
        _check_deadline(started, deadline_seconds)
        raise CleanupError("backup_failed") from error
    finally:
        destination.close()
    os.chmod(candidate, 0o600)


def _validate_candidate(
    candidate: Path,
    started: float,
    deadline_seconds: int,
    hooks: CleanupHooks | None,
    checkpoint: Check,
    *,
    completed: Validation | None = None,
    checked: Callable[[str], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[Validation, dict[str, object] | None]:
    """Run validation and collect only the bounded recognizable preview fields.

    Parameters are a scratch ``candidate``, shared operation deadline, optional
    scratch-only hooks, and the prior checkpoint outcome. Returns validation plus
    a preview for readable databases, or ``None`` when SQLite cannot read the
    candidate. The function opens only the scratch candidate and never returns
    arbitrary table rows, SQL text, or SQLite integrity messages. ``completed``
    supplies hash-bound saved gates; ``checked`` durably records each newly
    passed expensive gate, and ``progress`` announces a gate before running it.
    """
    integrity = Check.FAIL
    foreign_keys = Check.FAIL
    clean_reopen = Check.FAIL
    sidecars = Check.FAIL
    preview: dict[str, object] | None = None
    try:
        connection = _connect_rw(candidate)
        try:
            _set_deadline_handler(connection, started, deadline_seconds)
            _check_deadline(started, deadline_seconds)
            if progress:
                progress("reusing integrity check" if completed and completed.integrity == Check.PASS else "checking integrity")
            integrity_rows = [("ok",)] if completed and completed.integrity == Check.PASS else connection.execute("PRAGMA integrity_check").fetchall()
            integrity = Check.PASS if integrity_rows == [("ok",)] else Check.FAIL
            if integrity == Check.PASS and checked and not (completed and completed.integrity == Check.PASS):
                checked("integrity")
            if progress:
                progress("reusing foreign-key check" if completed and completed.foreign_keys == Check.PASS else "checking foreign keys")
            foreign_key_rows = [] if completed and completed.foreign_keys == Check.PASS else connection.execute("PRAGMA foreign_key_check").fetchall()
            foreign_keys = Check.PASS if foreign_key_rows == [] else Check.FAIL
            if integrity == Check.PASS and foreign_keys == Check.PASS and checked and not (completed and completed.foreign_keys == Check.PASS):
                checked("foreign_keys")
            preview = _preview(connection, integrity_rows, foreign_key_rows)
        finally:
            connection.close()
        if hooks and hooks.before_reopen:
            hooks.before_reopen(candidate)
        _check_deadline(started, deadline_seconds)
        connection = _connect_rw(candidate)
        try:
            _set_deadline_handler(connection, started, deadline_seconds)
            connection.execute("PRAGMA schema_version").fetchone()
            clean_reopen = Check.PASS
        finally:
            connection.close()
        sidecars = (
            Check.PASS
            if not any(
                Path(f"{candidate}{suffix}").exists()
                for suffix in ("-wal", "-shm", "-journal")
            )
            else Check.FAIL
        )
    except (CleanupError, sqlite3.Error, OSError) as error:
        if isinstance(error, OSError) or isinstance(error, CleanupError) and error.code == "deadline_exceeded" or storage_failure(error, candidate.parent):
            raise
        _check_deadline(started, deadline_seconds)
    return (
        Validation(integrity, foreign_keys, checkpoint, clean_reopen, sidecars),
        preview,
    )


def _preview(
    connection: sqlite3.Connection,
    integrity_rows: list[tuple[object, ...]],
    foreign_key_rows: list[tuple[object, ...]],
) -> dict[str, object]:
    """Build a fixed-size preview from known OpenCode fields and check outcomes.

    Parameters are an open scratch connection and structured SQLite pragma rows.
    Returns only project identity, up to four session identity records, and
    counted issue summaries. Missing expected tables or columns make only their
    respective domain summary ``"unavailable"``. This function never parses
    integrity message text or exposes data from arbitrary application tables.
    """
    table_counts: dict[str, int] = {}
    for row in foreign_key_rows:
        if row and isinstance(row[0], str) and _safe_table_name(row[0]):
            table_counts[row[0]] = table_counts.get(row[0], 0) + 1
    database_issue_count = 0
    if integrity_rows != [("ok",)]:
        database_issue_count = len(integrity_rows)
        for table in _schema_tables(connection):
            quoted = table.replace('"', '""')
            rows = connection.execute(f'PRAGMA integrity_check("{quoted}")').fetchall()
            issues = sum(row != ("ok",) for row in rows)
            if issues:
                table_counts[table] = table_counts.get(table, 0) + issues
    return {
        "projects": _projects(connection),
        "recent_sessions": _recent_sessions(connection),
        "table_issue_counts": dict(sorted(table_counts.items())),
        "database_issue_count": database_issue_count,
    }


def _projects(connection: sqlite3.Connection) -> list[dict[str, object]] | str:
    """Return bounded OpenCode project identity records or ``"unavailable"``.

    Parameters: ``connection`` is an already-open scratch candidate. Returns
    stable project fields only. Missing project schema elements are not generic
    validation failures; SQLite query errors return ``"unavailable"``.
    """
    if not _has_columns(connection, "project", {"id", "name", "worktree"}):
        return "unavailable"
    try:
        rows = connection.execute(
            "SELECT id, name, worktree FROM project ORDER BY id LIMIT 128"
        ).fetchall()
    except sqlite3.Error:
        return "unavailable"
    projects: list[dict[str, object]] = []
    for project_id, name, worktree in rows:
        if not isinstance(project_id, str) or not isinstance(worktree, str):
            continue
        projects.append(
            {
                "id": _bounded_text(project_id),
                "name": _bounded_text(name) if isinstance(name, str) else None,
                "worktree": _bounded_text(worktree),
                "origin": _origin(worktree),
            }
        )
    return projects


def _recent_sessions(connection: sqlite3.Connection) -> list[dict[str, object]] | str:
    """Return the four latest OpenCode session identities or ``"unavailable"``.

    Parameters: ``connection`` is an open scratch candidate. Returns the four
    greatest ``time_updated`` non-archived sessions when ``time_archived`` is
    present; older compatible schemas fall back to the four greatest sessions.
    Query failures and missing required columns return ``"unavailable"`` only
    for the session summary.
    """
    required = {"id", "title", "project_id", "time_updated"}
    if not _has_columns(connection, "session", required):
        return "unavailable"
    columns = _columns(connection, "session")
    query = "SELECT id, title, project_id, time_updated FROM session"
    if "time_archived" in columns:
        query += " WHERE time_archived IS NULL"
    query += " ORDER BY time_updated DESC, id ASC LIMIT 4"
    try:
        rows = connection.execute(query).fetchall()
    except sqlite3.Error:
        return "unavailable"
    sessions: list[dict[str, object]] = []
    for session_id, title, project_id, updated in rows:
        if (
            not isinstance(session_id, str)
            or not isinstance(title, str)
            or not isinstance(project_id, str)
            or type(updated) is not int
        ):
            continue
        sessions.append(
            {
                "id": _bounded_text(session_id),
                "title": _bounded_text(title),
                "project_id": _bounded_text(project_id),
                "time_updated": updated,
            }
        )
    return sessions


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return recognized columns for one safe schema table without row access."""
    try:
        return {
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            if len(row) > 1 and isinstance(row[1], str)
        }
    except sqlite3.Error:
        return set()


def _has_columns(
    connection: sqlite3.Connection, table: str, required: set[str]
) -> bool:
    """Return whether one expected table exposes all required stable columns."""
    return required <= _columns(connection, table)


def _schema_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Return a bounded deterministic list of safe non-system schema table names."""
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name LIMIT 128"
    ).fetchall()
    return tuple(
        row[0] for row in rows if isinstance(row[0], str) and _safe_table_name(row[0])
    )


def _safe_table_name(value: str) -> bool:
    """Return whether a schema table name is safe for the limited pragma query."""
    return re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,127}", value
    ) is not None and not value.startswith("sqlite_")


def _origin(worktree: str) -> str:
    """Return a credential-redacted origin URL or ``"unknown"`` without errors.

    Parameters: ``worktree`` is a recorded project location. Returns a bounded
    read-only Git result; missing Git, invalid worktrees, nonzero exits, timeouts,
    and malformed URLs all produce ``"unknown"``. The subprocess has no shell,
    no stdin, a finite timeout, and no surfaced stderr.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", worktree, "remote", "get-url", "origin"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return _redact_origin(completed.stdout.strip())


def _redact_origin(origin: str) -> str:
    """Strip URL credentials, query data, and fragments from one Git origin value."""
    if not origin or len(origin) > MAX_PREVIEW_TEXT * 4:
        return "unknown"
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return "unknown"
    if parsed.scheme and parsed.netloc:
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return "unknown"
        if host is None:
            return "unknown"
        netloc = host if port is None else f"{host}:{port}"
        return _bounded_text(urlunsplit((parsed.scheme, netloc, parsed.path, "", "")))
    if "@" in origin and ":" in origin and not any(char.isspace() for char in origin):
        return _bounded_text(origin.split("@", 1)[1].split("?", 1)[0].split("#", 1)[0])
    return "unknown"


def _bounded_text(value: str) -> str:
    """Return one bounded single-line preview string without control characters."""
    return "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in value[:MAX_PREVIEW_TEXT]
    )


def _refuse_journal(snapshot: AcceptedSnapshot) -> None:
    """Reject rollback-journal evidence before any SQLite connection is opened."""
    if next(item for item in snapshot.manifest.files if item.name == "journal").present:
        raise CleanupError("unsupported_journal")


def _classify(snapshot: AcceptedSnapshot, evidence: _WalEvidence) -> str:
    """Classify a valid candidate from captured sidecar and WAL layout evidence."""
    shm_present = next(
        item for item in snapshot.manifest.files if item.name == "shm"
    ).present
    if (not evidence.present and shm_present) or evidence.trailing_bytes:
        return "uncertain"
    return "complete"


def _report(
    snapshot: AcceptedSnapshot,
    completeness: str,
    validation: Validation,
    evidence: _WalEvidence,
    passive_checkpoint: tuple[int, int, int],
    truncate_checkpoint: tuple[int, int, int],
    preview: dict[str, object] | None,
) -> dict[str, object]:
    """Build a bounded immutable report without domain rows or SQLite messages."""
    return {
        "schema_version": 1,
        "tool_version": __version__,
        "snapshot_id": snapshot.snapshot_id,
        "target": snapshot.manifest.target,
        "target_id": snapshot.manifest.target_id,
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(
                snapshot.manifest.to_dict(),
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest(),
        "completeness": completeness,
        "completeness_scope": "captured_source_set",
        "historical_completeness": "not_proven",
        "validation": validation.to_dict(),
        "passive_checkpoint": list(passive_checkpoint),
        "truncate_checkpoint": list(truncate_checkpoint),
        "wal_evidence": evidence.to_dict(),
        "sqlite_version": sqlite3.sqlite_version,
        "preview": preview,
    }


def _invalid(
    snapshot_id: str,
    target: str,
    validation: Validation,
    code: str,
    preview: dict[str, object] | None = None,
    diagnostic: str | None = None,
) -> CleanupOutcome:
    """Return one non-installable outcome with preview only when SQLite read it."""
    return CleanupOutcome(
        snapshot_id=snapshot_id,
        target=target,
        status="invalid",
        completeness="invalid",
        validation=validation,
        installable=False,
        preview=preview,
        diagnostic_code=code,
        diagnostic_message=diagnostic,
    )


def _check_deadline(started: float, deadline_seconds: int) -> None:
    """Raise a bounded cleanup error once the operation deadline expires."""
    if time.monotonic() - started >= deadline_seconds:
        raise CleanupError("deadline_exceeded")


def _set_deadline_handler(
    connection: sqlite3.Connection, started: float, deadline_seconds: int
) -> None:
    """Interrupt long-running SQLite virtual-machine work after the deadline."""
    connection.set_progress_handler(
        lambda: int(time.monotonic() - started >= deadline_seconds), 1_000
    )
