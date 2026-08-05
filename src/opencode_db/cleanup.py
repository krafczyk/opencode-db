"""Create sidecar-free candidates through normal SQLite recovery on copies.

This module consumes only accepted retained snapshots. SQLite never opens an
active or retained source path: it opens a fresh local scratch copy whose SHM
file is intentionally absent, allowing SQLite to rebuild its WAL index.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Callable

from . import __version__
from .artifacts import (
    CaptureError,
    load_accepted_snapshot,
    new_artifact_id,
    persist_candidate,
    snapshot_source_path,
)
from .model import AcceptedSnapshot, Check, CleanupOutcome, Validation
from .target import (
    TargetEnvironment,
    TargetError,
    admit_scratch,
    ensure_private_directory,
)

WAL_HEADER_BYTES = 32
"""Fixed byte length of an SQLite WAL header."""

WAL_FRAME_HEADER_BYTES = 24
"""Fixed byte length preceding each SQLite WAL frame page."""


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
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
    hooks: CleanupHooks | None = None,
) -> CleanupOutcome:
    """Recover, validate, and retain one candidate from an accepted snapshot.

    Parameters: ``snapshot_dir`` names an exact retained source group,
    ``scratch_dir`` is an admitted local writable root, ``deadline_seconds`` is
    a finite overall bound, ``environment`` provides testable scratch probes,
    and ``hooks`` supplies narrow scratch-only interruption seams. Returns a
    :class:`CleanupOutcome` classified ``complete``, ``uncertain``, or invalid.
    Raises no SQLite error to callers: malformed inputs and recovery failures
    become non-installable outcomes. The function rehashes retained inputs,
    writes SQLite only in scratch, and persists a private candidate/report only
    after all validation and digest checks pass.
    """
    started = time.monotonic()
    validation = Validation()
    snapshot_id = "unknown"
    target = "unknown"
    workdir: Path | None = None
    try:
        if deadline_seconds < 1:
            raise CleanupError("deadline_exceeded")
        snapshot = load_accepted_snapshot(
            snapshot_dir, started=started, deadline_seconds=deadline_seconds
        )
        snapshot_id = snapshot.snapshot_id
        target = snapshot.manifest.target
        _check_deadline(started, deadline_seconds)
        _refuse_journal(snapshot)
        evidence = _inspect_wal(snapshot)
        if evidence.malformed:
            raise CleanupError("cleanup_invalid")
        workdir = _create_scratch(
            Path(scratch_dir), snapshot, environment, started, deadline_seconds
        )
        scratch_main = workdir / "source.sqlite"
        _copy_inputs(snapshot, scratch_main, started, deadline_seconds)
        checkpoint = Check.NOT_RUN
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
            checkpoint = Check.PASS
            candidate = workdir / "candidate.sqlite"
            if hooks and hooks.before_backup:
                hooks.before_backup(candidate)
            _backup(source, candidate, started, deadline_seconds)
        finally:
            source.close()
        validation = _validate_candidate(
            candidate, started, deadline_seconds, hooks, checkpoint
        )
        if validation != Validation(
            integrity=Check.PASS,
            foreign_keys=Check.PASS,
            checkpoint=Check.PASS,
            clean_reopen=Check.PASS,
            sidecars_absent=Check.PASS,
        ):
            return _invalid(snapshot_id, target, validation, "cleanup_invalid")
        _check_deadline(started, deadline_seconds)
        # Catch retained-input tampering between initial staging and publication.
        snapshot = load_accepted_snapshot(
            snapshot.snapshot_dir,
            started=started,
            deadline_seconds=deadline_seconds,
        )
        completeness = _classify(snapshot, evidence)
        candidate_id = new_artifact_id("candidate")
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
            ),
            started=started,
            deadline_seconds=deadline_seconds,
        )
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
        )
    except (CaptureError, CleanupError, TargetError, OSError, sqlite3.Error) as error:
        code = (
            error.code
            if isinstance(error, (CaptureError, CleanupError, TargetError))
            else "cleanup_invalid"
        )
        return _invalid(snapshot_id, target, validation, code)
    except Exception:
        return _invalid(snapshot_id, target, validation, "cleanup_invalid")
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def _create_scratch(
    root: Path,
    snapshot: AcceptedSnapshot,
    environment: TargetEnvironment | None,
    started: float,
    deadline_seconds: int,
) -> Path:
    """Create one private admitted scratch workspace for a copied source set."""
    required = sum(item.size or 0 for item in snapshot.manifest.files) * 3 + 65536
    admitted = admit_scratch(root, required, 8, environment)
    ensure_private_directory(admitted)
    _check_deadline(started, deadline_seconds)
    workdir = admitted / f"cleanup-{secrets.token_hex(12)}"
    ensure_private_directory(workdir)
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
) -> Validation:
    """Run full SQLite checks, clean reopen, and sidecar absence validation."""
    integrity = Check.FAIL
    foreign_keys = Check.FAIL
    clean_reopen = Check.FAIL
    sidecars = Check.FAIL
    try:
        connection = _connect_rw(candidate)
        try:
            _set_deadline_handler(connection, started, deadline_seconds)
            _check_deadline(started, deadline_seconds)
            integrity = (
                Check.PASS
                if connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                else Check.FAIL
            )
            foreign_keys = (
                Check.PASS
                if connection.execute("PRAGMA foreign_key_check").fetchall() == []
                else Check.FAIL
            )
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
    except (CleanupError, sqlite3.Error, OSError):
        _check_deadline(started, deadline_seconds)
    return Validation(integrity, foreign_keys, checkpoint, clean_reopen, sidecars)


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
) -> dict[str, object]:
    """Build a bounded immutable report without domain rows or SQLite messages."""
    return {
        "schema_version": 1,
        "tool_version": __version__,
        "snapshot_id": snapshot.snapshot_id,
        "target": snapshot.manifest.target,
        "target_id": snapshot.manifest.target_id,
        "completeness": completeness,
        "completeness_scope": "captured_source_set",
        "historical_completeness": "not_proven",
        "validation": validation.to_dict(),
        "passive_checkpoint": list(passive_checkpoint),
        "truncate_checkpoint": list(truncate_checkpoint),
        "wal_evidence": evidence.to_dict(),
        "sqlite_version": sqlite3.sqlite_version,
    }


def _invalid(
    snapshot_id: str, target: str, validation: Validation, code: str
) -> CleanupOutcome:
    """Return one non-installable outcome without persisting a candidate."""
    return CleanupOutcome(
        snapshot_id=snapshot_id,
        target=target,
        status="invalid",
        completeness="invalid",
        validation=validation,
        installable=False,
        diagnostic_code=code,
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
