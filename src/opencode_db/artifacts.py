"""Create and validate private immutable SQLite cleanup evidence.

The functions here read operator-selected source bytes with ordinary file I/O.
They never import SQLite, invoke OpenCode, or writable-open active or retained
database files. Later units consume only accepted snapshots and rehash retained
candidates/reports before allowing a selected action to proceed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar, cast

from . import __version__
from .model import (
    AcceptedSnapshot,
    CandidateEvidence,
    CaptureOutcome,
    OperationEvidence,
    SourceFile,
    SourceManifest,
)
from .target import (
    TargetEnvironment,
    TargetError,
    admit_scratch,
    ensure_private_directory,
    resolve_recorded_target,
    resolve_target,
    storage_space,
)

COPY_CHUNK_BYTES = 1024 * 1024
"""Bounded streaming-copy chunk size for source and retained artifact files."""

MANIFEST_SCHEMA_VERSION = 1
"""Schema version for target catalogs and retained snapshot manifests."""

CATALOG_SCHEMA_VERSION = 2
"""Current schema version for target-scoped catalog coordination state."""

_P = ParamSpec("_P")
_R = TypeVar("_R")


class CaptureError(RuntimeError):
    """Report a bounded copy or artifact failure without propagating OS details.

    Parameters: ``code`` is the stable public diagnostic code. The exception
    leaves source paths untouched; callers preserve any diagnostic-only artifact
    group already created.
    """

    def __init__(self, code: str) -> None:
        """Store the safe ``code`` without retaining arbitrary exception text."""
        super().__init__(code)
        self.code = code


def _private_umask(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run one artifact-writing call with ``077`` and restore its caller state."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        previous = os.umask(0o077)
        try:
            return function(*args, **kwargs)
        finally:
            os.umask(previous)

    return wrapped


@_private_umask
def capture_source_set(
    database: str,
    *,
    scratch_dir: str | None = None,
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
    after_copy: Callable[[Path], None] | None = None,
    after_chunk: Callable[[Path, int], None] | None = None,
) -> CaptureOutcome:
    """Capture a stable exact source set into a private target-scoped snapshot.

    Parameters: ``database`` is one explicit path, ``scratch_dir`` optionally
    selects a local scratch root to preflight, ``deadline_seconds`` is a finite
    positive operation limit, ``environment`` supplies testable system probes,
    and ``after_copy`` / ``after_chunk`` are narrow test seams called after each
    copied source file or stream chunk. Returns a :class:`CaptureOutcome`; only
    ``accepted=True`` is eligible for future recovery. Invalid targets and
    storage preconditions return a refusal without artifact capture; source
    mutation, interruption, I/O, and rollback-journal evidence retain
    diagnostic-only snapshots. The function temporarily sets process umask
    ``077`` while writing private artifacts, restores the caller's umask, never
    opens SQLite, and never changes source files.
    """
    if deadline_seconds < 1:
        return _outcome(None, None, None, None, False, "deadline_exceeded")
    started = time.monotonic()
    try:
        target = resolve_target(database)
        _check_deadline(started, deadline_seconds)
        pre_manifest = _source_manifest(
            target.path, target.target_id, started, deadline_seconds
        )
        _preflight(target.control_dir, pre_manifest, scratch_dir, environment)
        catalog = _read_catalog(target.control_dir, target.path, target.target_id)
    except TargetError as error:
        return _outcome(database, None, None, None, False, error.code)
    except CaptureError as error:
        return _outcome(
            str(target.path), target.target_id, None, None, False, error.code
        )

    snapshot_id = new_artifact_id("snapshot")
    operation_id = new_artifact_id("operation")
    snapshot_dir: Path | None = None
    try:
        snapshot_dir = _create_snapshot_directory(target.control_dir, snapshot_id)
        _write_catalog(
            target.control_dir,
            target.path,
            target.target_id,
            snapshot_id,
            operation_id,
            catalog,
        )
        _write_json(snapshot_dir / "pre-manifest.json", pre_manifest.to_dict())
        copied_manifest = _copy_manifest(
            target.path,
            target.target_id,
            pre_manifest,
            snapshot_dir,
            started,
            deadline_seconds,
            after_copy,
            after_chunk,
        )
        post_manifest = _source_manifest(
            target.path, target.target_id, started, deadline_seconds
        )
        if pre_manifest != post_manifest or pre_manifest != copied_manifest:
            _write_snapshot_manifest(
                snapshot_dir,
                target.path,
                target.target_id,
                snapshot_id,
                operation_id,
                pre_manifest,
                copied_manifest,
                post_manifest,
                "diagnostic_only",
                "source_changed",
            )
            return _outcome(
                str(target.path),
                target.target_id,
                snapshot_id,
                operation_id,
                False,
                "source_changed",
                snapshot_dir,
            )
        if _has_journal(pre_manifest):
            _write_snapshot_manifest(
                snapshot_dir,
                target.path,
                target.target_id,
                snapshot_id,
                operation_id,
                pre_manifest,
                copied_manifest,
                post_manifest,
                "diagnostic_only",
                "unsupported_journal",
            )
            return _outcome(
                str(target.path),
                target.target_id,
                snapshot_id,
                operation_id,
                False,
                "unsupported_journal",
                snapshot_dir,
            )
        _write_snapshot_manifest(
            snapshot_dir,
            target.path,
            target.target_id,
            snapshot_id,
            operation_id,
            pre_manifest,
            copied_manifest,
            post_manifest,
            "accepted",
            None,
        )
        return _outcome(
            str(target.path),
            target.target_id,
            snapshot_id,
            operation_id,
            True,
            "captured",
            snapshot_dir,
        )
    except (CaptureError, InterruptedError, OSError) as error:
        reason = (
            error.code if isinstance(error, CaptureError) else "operational_failure"
        )
        if snapshot_dir is not None:
            _best_effort_diagnostic_manifest(
                snapshot_dir,
                target.path,
                target.target_id,
                snapshot_id,
                operation_id,
                pre_manifest,
                reason,
            )
        return _outcome(
            str(target.path),
            target.target_id,
            snapshot_id,
            operation_id,
            False,
            reason,
            snapshot_dir,
        )


def load_accepted_snapshot(
    snapshot_dir: Path,
    *,
    started: float | None = None,
    deadline_seconds: int = 1_800,
) -> AcceptedSnapshot:
    """Load and rehash one accepted snapshot without opening SQLite.

    Parameters: ``snapshot_dir`` is the exact private retained snapshot
    directory. Returns an :class:`AcceptedSnapshot` only when its closed
    manifest is accepted, its path matches its target-scoped IDs, all three
    capture manifests agree, and every retained source file has its recorded
    digest. ``started`` and ``deadline_seconds`` optionally preserve a caller's
    finite operation budget. Raises :class:`CaptureError` for a changed,
    malformed, non-private, misplaced, expired, or diagnostic-only snapshot.
    The function uses read-only ordinary file I/O and never modifies retained
    evidence.
    """
    operation_started = time.monotonic() if started is None else started
    try:
        if (
            snapshot_dir.is_symlink()
            or not snapshot_dir.is_dir()
            or snapshot_dir.stat().st_mode & 0o777 != 0o700
        ):
            raise CaptureError("snapshot_invalid")
        manifest_path = snapshot_dir / "manifest.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_mode & 0o777 != 0o600:
            raise CaptureError("snapshot_invalid")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureError("snapshot_invalid") from error
    required = {
        "schema_version",
        "tool_version",
        "target",
        "target_id",
        "snapshot_id",
        "operation_id",
        "state",
        "reason",
        "pre_copy_manifest",
        "copied_manifest",
        "post_copy_manifest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CaptureError("snapshot_invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != MANIFEST_SCHEMA_VERSION
        or value["state"] != "accepted"
        or value["reason"] is not None
        or not all(
            isinstance(value[name], str)
            for name in (
                "tool_version",
                "target",
                "target_id",
                "snapshot_id",
                "operation_id",
            )
        )
    ):
        raise CaptureError("snapshot_invalid")
    if (
        snapshot_dir.name != value["snapshot_id"]
        or snapshot_dir.parent.name != "snapshots"
        or snapshot_dir.parent.parent.name != value["target_id"]
        or snapshot_dir.parent.parent.parent.name != ".opencode-db"
    ):
        raise CaptureError("snapshot_invalid")
    try:
        manifests = tuple(
            SourceManifest.from_dict(value[name])
            for name in ("pre_copy_manifest", "copied_manifest", "post_copy_manifest")
        )
    except ValueError as error:
        raise CaptureError("snapshot_invalid") from error
    manifest = manifests[0]
    if (
        manifests[1:] != (manifest, manifest)
        or manifest.target != value["target"]
        or manifest.target_id != value["target_id"]
        or hashlib.sha256(os.fsencode(manifest.target)).hexdigest()[:32]
        != manifest.target_id
        or re.fullmatch(r"snapshot-\d{8}T\d{6}Z-[0-9a-f]{24}", value["snapshot_id"])
        is None
        or re.fullmatch(r"operation-\d{8}T\d{6}Z-[0-9a-f]{24}", value["operation_id"])
        is None
    ):
        raise CaptureError("snapshot_invalid")
    for source_file in manifest.files:
        path = _snapshot_source_path(snapshot_dir, manifest, source_file.name)
        if source_file.present:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_mode & 0o777 != 0o600
            ):
                raise CaptureError("snapshot_invalid")
            try:
                size, digest = _hash_file(
                    path,
                    operation_started,
                    deadline_seconds,
                )
            except CaptureError as error:
                raise CaptureError("snapshot_invalid") from error
            if size != source_file.size or digest != source_file.sha256:
                raise CaptureError("snapshot_invalid")
        elif path.exists() or path.is_symlink():
            raise CaptureError("snapshot_invalid")
    return AcceptedSnapshot(
        snapshot_dir=snapshot_dir,
        snapshot_id=value["snapshot_id"],
        operation_id=value["operation_id"],
        manifest=manifest,
    )


def snapshot_source_path(
    snapshot_dir: Path, manifest: SourceManifest, source_name: str
) -> Path:
    """Return the retained filename for one named source file without opening it.

    Parameters: ``snapshot_dir`` is a retained group, ``manifest`` supplies the
    original main basename, and ``source_name`` is one supported logical source
    name. Returns the captured filename. Raises :class:`CaptureError` for an
    unsupported name. The function has no filesystem side effects.
    """
    return _snapshot_source_path(snapshot_dir, manifest, source_name)


def _snapshot_source_path(
    snapshot_dir: Path, manifest: SourceManifest, source_name: str
) -> Path:
    """Map one fixed logical source name to capture's preserved basename."""
    main = Path(manifest.target).name
    suffix = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}.get(
        source_name
    )
    if suffix is None:
        raise CaptureError("snapshot_invalid")
    return snapshot_dir / f"{main}{suffix}"


@_private_umask
def persist_candidate(
    snapshot: AcceptedSnapshot,
    candidate: Path,
    candidate_id: str,
    report: dict[str, object],
    *,
    started: float,
    deadline_seconds: int,
) -> tuple[Path, Path, str]:
    """Persist one closed, validated candidate and immutable report privately.

    Parameters: ``snapshot`` is a freshly revalidated accepted source,
    ``candidate`` is a closed scratch database, ``candidate_id`` is its opaque
    identifier, and ``report`` is a canonical safe report mapping. ``started``
    and ``deadline_seconds`` preserve the caller's finite operation budget.
    Returns the retained candidate path, report path, and report SHA-256 digest.
    Raises :class:`CaptureError` if source revalidation, private writes, digest
    verification, or the deadline fails. It writes only inside the bound
    snapshot directory and never writable-opens any retained input.
    """
    snapshot = load_accepted_snapshot(
        snapshot.snapshot_dir, started=started, deadline_seconds=deadline_seconds
    )
    if not re.fullmatch(r"candidate-[A-Za-z0-9-]{1,80}", candidate_id):
        raise CaptureError("candidate_persist_failed")
    if candidate.is_symlink() or not candidate.is_file():
        raise CaptureError("candidate_persist_failed")
    expected_size, expected_digest = _hash_file(candidate, started, deadline_seconds)
    destination = snapshot.snapshot_dir / f"{candidate_id}.sqlite"
    report_path = snapshot.snapshot_dir / f"{candidate_id}.report.json"
    if destination.exists() or report_path.exists():
        raise CaptureError("candidate_persist_failed")
    try:
        _copy_file(candidate, destination, started, deadline_seconds, None)
        size, digest = _hash_file(destination, started, deadline_seconds)
        if (size, digest) != (expected_size, expected_digest):
            raise CaptureError("candidate_persist_failed")
        value = dict(report)
        value["candidate_id"] = candidate_id
        value["candidate_sha256"] = digest
        value["candidate_size"] = size
        _write_json(report_path, value)
        _, report_digest = _hash_file(report_path, started, deadline_seconds)
        _register_candidate(
            snapshot,
            candidate_id,
            size,
            digest,
            report_digest,
        )
    except (CaptureError, OSError) as error:
        destination.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        if isinstance(error, CaptureError):
            raise
        raise CaptureError("candidate_persist_failed") from error
    return destination, report_path, report_digest


def register_preview_scratch(
    snapshot: AcceptedSnapshot, scratch_path: Path
) -> OperationEvidence:
    """Record exact host-local preview scratch before the directory is created.

    Parameters: ``snapshot`` is freshly accepted retained source evidence and
    ``scratch_path`` is the planned absolute operation-specific workspace.
    Returns the persisted :class:`OperationEvidence`. Raises :class:`CaptureError`
    for target/catalog mismatches or unsafe paths. The function updates only the
    private target catalog; it neither creates scratch nor opens SQLite.
    """
    if not scratch_path.is_absolute() or scratch_path.name != snapshot.operation_id:
        raise CaptureError("snapshot_invalid")
    snapshots, candidates, operations = _read_catalog(
        snapshot.snapshot_dir.parent.parent,
        Path(snapshot.manifest.target),
        snapshot.manifest.target_id,
    )
    if not any(
        entry["snapshot_id"] == snapshot.snapshot_id
        and entry["operation_id"] == snapshot.operation_id
        for entry in snapshots
    ):
        raise CaptureError("snapshot_invalid")
    evidence = OperationEvidence(
        operation_id=snapshot.operation_id,
        snapshot_id=snapshot.snapshot_id,
        state="previewing",
        host=socket.gethostname(),
        scratch_path=str(scratch_path),
    )
    operations = [
        entry for entry in operations if entry["operation_id"] != snapshot.operation_id
    ]
    operations.append(_operation_dict(evidence))
    _write_catalog_state(
        snapshot.snapshot_dir.parent.parent,
        Path(snapshot.manifest.target),
        snapshot.manifest.target_id,
        snapshots,
        candidates,
        operations,
    )
    return evidence


def bind_preview_scratch(snapshot: AcceptedSnapshot, scratch_path: Path) -> None:
    """Write exact tool-owned authority inside a newly created scratch directory.

    Parameters bind an accepted ``snapshot`` to its registered ``scratch_path``.
    Returns ``None`` after a private durable marker is written. Raises
    :class:`CaptureError` unless the path is the exact private operation
    directory. It never opens SQLite or changes source/retained evidence.
    """
    if (
        not scratch_path.is_absolute()
        or scratch_path.name != snapshot.operation_id
        or scratch_path.is_symlink()
        or not scratch_path.is_dir()
        or scratch_path.stat().st_mode & 0o777 != 0o700
    ):
        raise CaptureError("snapshot_invalid")
    try:
        _write_json(
            scratch_path / ".opencode-db-operation.json",
            {
                "schema_version": 1,
                "operation_id": snapshot.operation_id,
                "snapshot_id": snapshot.snapshot_id,
                "target_id": snapshot.manifest.target_id,
                "host": socket.gethostname(),
            },
        )
    except OSError as error:
        raise CaptureError("copy_failed") from error


def finish_preview_operation(snapshot: AcceptedSnapshot, state: str) -> None:
    """Mark one registered preview operation terminal after scratch cleanup.

    Parameters: ``snapshot`` identifies a retained target-scoped operation and
    ``state`` is one of ``complete``, ``uncertain``, or ``invalid``. Returns
    ``None`` after updating only private catalog metadata. Raises
    :class:`CaptureError` for malformed catalog state; it never opens SQLite or
    changes source, candidate, report, or scratch files.
    """
    if state not in {"complete", "uncertain", "invalid"}:
        raise CaptureError("snapshot_invalid")
    root = snapshot.snapshot_dir.parent.parent
    snapshots, candidates, operations = _read_catalog(
        root, Path(snapshot.manifest.target), snapshot.manifest.target_id
    )
    replaced = False
    updated: list[dict[str, str | None]] = []
    for entry in operations:
        if entry["operation_id"] == snapshot.operation_id:
            updated.append({**entry, "state": state})
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        raise CaptureError("snapshot_invalid")
    _write_catalog_state(
        root,
        Path(snapshot.manifest.target),
        snapshot.manifest.target_id,
        snapshots,
        candidates,
        updated,
    )


def operation_status(database: str, operation_id: str) -> OperationEvidence:
    """Read one exact target-scoped operation without touching SQLite or processes.

    Parameters: ``database`` is an exact previously recorded target path and
    ``operation_id`` is an exact opaque ID. Returns the recorded
    :class:`OperationEvidence`. Raises :class:`CaptureError` when the target,
    catalog, or operation reference is unavailable or foreign. This read-only
    operation has no source, scratch, process, or SQLite side effects.
    """
    try:
        target = resolve_recorded_target(database)
        _, _, operations = _read_catalog(
            target.control_dir, target.path, target.target_id
        )
    except TargetError as error:
        raise CaptureError(error.code) from error
    for entry in operations:
        if entry["operation_id"] == operation_id:
            return OperationEvidence(
                operation_id=cast(str, entry["operation_id"]),
                snapshot_id=cast(str, entry["snapshot_id"]),
                state=cast(str, entry["state"]),
                host=entry["host"],
                scratch_path=entry["scratch_path"],
            )
    raise CaptureError("candidate_changed")


def register_install_operation(
    database: str, candidate: CandidateEvidence, operation_id: str
) -> AcceptedSnapshot:
    """Register one exact candidate installation in its target catalog.

    Parameters bind the explicit ``database``, rehashed target-scoped
    ``candidate``, and newly allocated ``operation_id``.  Returns the accepted
    snapshot only after it and the candidate record are revalidated.  Raises
    :class:`CaptureError` for cross-target, changed, or incompatible evidence.
    The function changes only the private catalog; it never opens SQLite or an
    active source file.
    """
    try:
        target = resolve_target(database)
        snapshots, candidates, operations = _read_catalog(
            target.control_dir, target.path, target.target_id
        )
    except TargetError as error:
        raise CaptureError(error.code) from error
    if not re.fullmatch(r"install-\d{8}T\d{6}Z-[0-9a-f]{24}", operation_id) or any(
        item["operation_id"] == operation_id for item in operations
    ):
        raise CaptureError("candidate_changed")
    record = next(
        (item for item in candidates if item["candidate_id"] == candidate.candidate_id),
        None,
    )
    if (
        record is None
        or record["snapshot_id"] != candidate.snapshot_id
        or record["candidate_sha256"] != candidate.candidate_sha256
        or record["report_sha256"] != candidate.report_sha256
        or not any(item["snapshot_id"] == candidate.snapshot_id for item in snapshots)
    ):
        raise CaptureError("candidate_changed")
    snapshot = load_accepted_snapshot(
        target.control_dir / "snapshots" / candidate.snapshot_id
    )
    operations.append(
        {
            "operation_id": operation_id,
            "snapshot_id": candidate.snapshot_id,
            "state": "prepared",
            "host": None,
            "scratch_path": None,
        }
    )
    _write_catalog_state(
        target.control_dir,
        target.path,
        target.target_id,
        snapshots,
        candidates,
        operations,
    )
    return snapshot


def load_install_operation(database: str, operation_id: str) -> AcceptedSnapshot:
    """Load the accepted source snapshot bound to one installation operation.

    Parameters select an explicit recorded target and exact ``operation_id``.
    Returns the immutable accepted snapshot.  Raises :class:`CaptureError` for
    foreign, malformed, future, or changed evidence without modifying state or
    opening SQLite.
    """
    evidence = operation_status(database, operation_id)
    if not operation_id.startswith("install-"):
        raise CaptureError("candidate_changed")
    try:
        target = resolve_recorded_target(database)
    except TargetError as error:
        raise CaptureError(error.code) from error
    return load_accepted_snapshot(
        target.control_dir / "snapshots" / evidence.snapshot_id
    )


def set_install_operation_state(database: str, operation_id: str, state: str) -> None:
    """Persist one allowed installation state in the target catalog.

    ``database`` and ``operation_id`` select an existing installation record;
    ``state`` is a closed install lifecycle value.  Raises :class:`CaptureError`
    for incompatible catalog data.  This updates only catalog metadata and never
    opens SQLite or mutates active database files.
    """
    allowed = {
        "prepared",
        "quarantining",
        "promoting",
        "validating",
        "installed",
        "install_incomplete",
        "rolling_back",
        "rolled_back",
        "manual_recovery_required",
    }
    if state not in allowed:
        raise CaptureError("artifact_schema_unsupported")
    try:
        target = resolve_recorded_target(database)
        snapshots, candidates, operations = _read_catalog(
            target.control_dir, target.path, target.target_id
        )
    except TargetError as error:
        raise CaptureError(error.code) from error
    changed = False
    updated: list[dict[str, str | None]] = []
    for entry in operations:
        if entry["operation_id"] == operation_id and entry["operation_id"].startswith(
            "install-"
        ):
            updated.append({**entry, "state": state})
            changed = True
        else:
            updated.append(entry)
    if not changed:
        raise CaptureError("candidate_changed")
    _write_catalog_state(
        target.control_dir,
        target.path,
        target.target_id,
        snapshots,
        candidates,
        updated,
    )


def abort_preview_operation(database: str, operation_id: str) -> OperationEvidence:
    """Remove only same-host registered preview scratch and then record abort.

    Parameters: ``database`` and ``operation_id`` select exact catalog evidence.
    Returns the post-operation evidence. Raises :class:`CaptureError` if the
    operation is not a nonterminal preview or if exact safe scratch removal
    fails. A foreign-host operation returns ``scratch_cleanup_required`` without
    deletion. The function never inspects processes or alters SQLite/source data.
    """
    evidence = operation_status(database, operation_id)
    try:
        target = resolve_recorded_target(database)
    except TargetError as error:
        raise CaptureError(error.code) from error
    if evidence.state != "previewing" or evidence.scratch_path is None:
        raise CaptureError("candidate_changed")
    if evidence.host != socket.gethostname():
        return OperationEvidence(
            evidence.operation_id,
            evidence.snapshot_id,
            "scratch_cleanup_required",
            evidence.host,
            evidence.scratch_path,
        )
    scratch = Path(evidence.scratch_path)
    if not scratch.is_absolute() or scratch.name != evidence.operation_id:
        raise CaptureError("candidate_changed")
    try:
        if scratch.is_symlink():
            raise CaptureError("candidate_changed")
        if scratch.exists():
            _validate_scratch_marker(scratch, evidence, target.target_id)
            shutil.rmtree(scratch)
    except OSError as error:
        raise CaptureError("copy_failed") from error
    snapshots, candidates, operations = _read_catalog(
        target.control_dir, target.path, target.target_id
    )
    updated = [
        {**entry, "state": "aborted"}
        if entry["operation_id"] == operation_id
        else entry
        for entry in operations
    ]
    _write_catalog_state(
        target.control_dir,
        target.path,
        target.target_id,
        snapshots,
        candidates,
        updated,
    )
    return OperationEvidence(
        evidence.operation_id,
        evidence.snapshot_id,
        "aborted",
        evidence.host,
        evidence.scratch_path,
    )


def _validate_scratch_marker(
    scratch: Path, evidence: OperationEvidence, target_id: str
) -> None:
    """Require exact private marker authority before recursive scratch deletion."""
    marker = scratch / ".opencode-db-operation.json"
    try:
        if (
            scratch.stat().st_mode & 0o777 != 0o700
            or marker.is_symlink()
            or not marker.is_file()
            or marker.stat().st_mode & 0o777 != 0o600
        ):
            raise CaptureError("candidate_changed")
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureError("candidate_changed") from error
    if value != {
        "schema_version": 1,
        "operation_id": evidence.operation_id,
        "snapshot_id": evidence.snapshot_id,
        "target_id": target_id,
        "host": evidence.host,
    }:
        raise CaptureError("candidate_changed")


def select_candidate(
    database: str, candidate_id: str, approve_uncertain_report: str | None = None
) -> CandidateEvidence:
    """Rehash and validate one exact candidate/report pair before later install work.

    Parameters: ``database`` is the explicit target, ``candidate_id`` is an exact
    catalog ID, and ``approve_uncertain_report`` optionally supplies the required
    report SHA-256 for uncertain evidence. Returns verified
    :class:`CandidateEvidence`. Raises :class:`CaptureError` with
    ``candidate_changed`` or ``report_changed`` for altered or cross-target
    evidence, and ``approval_required`` when uncertainty lacks its exact report
    digest. It is read-only and performs no active-database mutation.
    """
    try:
        target = resolve_target(database)
        snapshots, candidates, _ = _read_catalog(
            target.control_dir, target.path, target.target_id
        )
    except TargetError as error:
        raise CaptureError(error.code) from error
    record = next(
        (item for item in candidates if item["candidate_id"] == candidate_id), None
    )
    if record is None:
        raise CaptureError("candidate_changed")
    snapshot_id = cast(str, record["snapshot_id"])
    if not any(item["snapshot_id"] == snapshot_id for item in snapshots):
        raise CaptureError("candidate_changed")
    snapshot_dir = target.control_dir / "snapshots" / snapshot_id
    candidate_path = snapshot_dir / f"{candidate_id}.sqlite"
    report_path = snapshot_dir / f"{candidate_id}.report.json"
    try:
        snapshot = load_accepted_snapshot(snapshot_dir)
    except CaptureError as error:
        raise CaptureError("candidate_changed") from error
    if snapshot.snapshot_id != snapshot_id:
        raise CaptureError("candidate_changed")
    try:
        candidate_size, candidate_digest = _hash_file(
            candidate_path, time.monotonic(), 1_800
        )
    except CaptureError as error:
        raise CaptureError("candidate_changed") from error
    if (candidate_size, candidate_digest) != (
        record["candidate_size"],
        record["candidate_sha256"],
    ):
        raise CaptureError("candidate_changed")
    try:
        _, report_digest = _hash_file(report_path, time.monotonic(), 1_800)
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (CaptureError, OSError, json.JSONDecodeError) as error:
        raise CaptureError("report_changed") from error
    if report_digest != record["report_sha256"] or not _valid_report(
        report,
        target.path,
        target.target_id,
        record,
        snapshot.manifest,
    ):
        raise CaptureError("report_changed")
    completeness = cast(str, report["completeness"])
    if completeness == "uncertain" and approve_uncertain_report != report_digest:
        raise CaptureError("approval_required")
    return CandidateEvidence(
        snapshot_id=snapshot_id,
        candidate_id=candidate_id,
        candidate_path=candidate_path,
        report_path=report_path,
        candidate_sha256=candidate_digest,
        report_sha256=report_digest,
        completeness=completeness,
    )


def _preflight(
    control_dir: Path,
    manifest: SourceManifest,
    scratch_dir: str | None,
    environment: TargetEnvironment | None,
) -> None:
    """Verify privacy, retained capacity, and admitted scratch before copying."""
    required_bytes = sum(item.size or 0 for item in manifest.files) + 64 * 1024
    required_inodes = len(manifest.files) + 8
    retained = storage_space(control_dir.parent, environment)
    if (
        retained.available_bytes < required_bytes
        or retained.available_inodes < required_inodes
    ):
        raise TargetError("storage_capacity")
    scratch_root = Path(
        scratch_dir
        if scratch_dir is not None
        else os.path.join(os.environ.get("TMPDIR", "/tmp"), "opencode-db")
    )
    admit_scratch(
        scratch_root,
        required_bytes=max(required_bytes * 2, COPY_CHUNK_BYTES),
        required_inodes=required_inodes,
        environment=environment,
    )
    ensure_private_directory(control_dir.parent)
    ensure_private_directory(control_dir)


def _source_manifest(
    main: Path,
    target_id: str,
    started: float,
    deadline_seconds: int,
) -> SourceManifest:
    """Hash exactly the main and standard sidecar source names without SQLite."""
    paths = (
        ("main", main),
        ("wal", Path(f"{main}-wal")),
        ("shm", Path(f"{main}-shm")),
        ("journal", Path(f"{main}-journal")),
    )
    files: list[SourceFile] = []
    for name, path in paths:
        _check_deadline(started, deadline_seconds)
        if not path.exists():
            files.append(SourceFile(name, False, None, None))
            continue
        if path.is_symlink() or not path.is_file():
            raise CaptureError("source_changed")
        files.append(
            SourceFile(name, True, *_hash_file(path, started, deadline_seconds))
        )
    return SourceManifest(target=str(main), target_id=target_id, files=tuple(files))


def _copy_manifest(
    main: Path,
    target_id: str,
    manifest: SourceManifest,
    snapshot_dir: Path,
    started: float,
    deadline_seconds: int,
    after_copy: Callable[[Path], None] | None,
    after_chunk: Callable[[Path, int], None] | None,
) -> SourceManifest:
    """Stream-copy all pre-manifest present files and return their exact hashes."""
    copied: list[SourceFile] = []
    for source_file, (_, source) in zip(
        manifest.files,
        (
            ("main", main),
            ("wal", Path(f"{main}-wal")),
            ("shm", Path(f"{main}-shm")),
            ("journal", Path(f"{main}-journal")),
        ),
        strict=True,
    ):
        if not source_file.present:
            copied.append(source_file)
            continue
        if not source.exists() or source.is_symlink() or not source.is_file():
            raise CaptureError("source_changed")
        destination = snapshot_dir / source.name
        _copy_file(source, destination, started, deadline_seconds, after_chunk)
        copied.append(
            SourceFile(
                source_file.name,
                True,
                *_hash_file(destination, started, deadline_seconds),
            )
        )
        if after_copy is not None:
            after_copy(source)
    return SourceManifest(target=str(main), target_id=target_id, files=tuple(copied))


def _copy_file(
    source: Path,
    destination: Path,
    started: float,
    deadline_seconds: int,
    after_chunk: Callable[[Path, int], None] | None,
) -> None:
    """Copy one source stream privately, fsync it, and enforce the deadline."""
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            os.chmod(destination, 0o600)
            chunk_number = 0
            while chunk := reader.read(COPY_CHUNK_BYTES):
                _check_deadline(started, deadline_seconds)
                writer.write(chunk)
                chunk_number += 1
                if after_chunk is not None:
                    after_chunk(source, chunk_number)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as error:
        raise CaptureError("copy_failed") from error


def _hash_file(path: Path, started: float, deadline_seconds: int) -> tuple[int, str]:
    """Return a streamed byte count and SHA-256 digest for one regular file."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as reader:
            while chunk := reader.read(COPY_CHUNK_BYTES):
                _check_deadline(started, deadline_seconds)
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise CaptureError("copy_failed") from error
    return size, digest.hexdigest()


def _create_snapshot_directory(control_dir: Path, snapshot_id: str) -> Path:
    """Create one collision-resistant private snapshot directory under control."""
    snapshots = control_dir / "snapshots"
    ensure_private_directory(snapshots)
    directory = snapshots / snapshot_id
    ensure_private_directory(directory)
    return directory


def _write_catalog(
    control_dir: Path,
    target: Path,
    target_id: str,
    snapshot_id: str,
    operation_id: str,
    catalog: tuple[
        list[dict[str, str]], list[dict[str, object]], list[dict[str, str | None]]
    ],
) -> None:
    """Persist a new snapshot and its initial nonterminal operation record."""
    snapshots, candidates, operations = catalog
    snapshots = [*snapshots, {"snapshot_id": snapshot_id, "operation_id": operation_id}]
    operations = [
        *operations,
        {
            "operation_id": operation_id,
            "snapshot_id": snapshot_id,
            "state": "capturing",
            "host": None,
            "scratch_path": None,
        },
    ]
    _write_catalog_state(
        control_dir, target, target_id, snapshots, candidates, operations
    )


def _read_catalog(
    control_dir: Path, target: Path, target_id: str
) -> tuple[list[dict[str, str]], list[dict[str, object]], list[dict[str, str | None]]]:
    """Read one compatible catalog before allocating a snapshot directory.

    Returns snapshot, candidate, and operation records. Raises :class:`CaptureError`
    for malformed, foreign-target, or future catalog state without creating a
    snapshot or copying source bytes. Catalog reads do not open SQLite.
    """
    path = control_dir / "catalog.json"
    try:
        if path.is_symlink():
            raise CaptureError("artifact_schema_unsupported")
        if not path.exists():
            return [], [], []
        if path.stat().st_mode & 0o777 != 0o600:
            raise CaptureError("artifact_not_private")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureError("artifact_schema_unsupported") from error
    if (
        not isinstance(value, dict)
        or value.get("target") != str(target)
        or value.get("target_id") != target_id
        or not isinstance(value.get("tool_version"), str)
    ):
        raise CaptureError("artifact_schema_unsupported")
    if value.get("schema_version") == MANIFEST_SCHEMA_VERSION and set(value) == {
        "schema_version",
        "tool_version",
        "target",
        "target_id",
        "snapshots",
    }:
        entries = value["snapshots"]
        candidates: list[dict[str, object]] = []
        operations = [
            {
                "operation_id": item["operation_id"],
                "snapshot_id": item["snapshot_id"],
                "state": "captured",
                "host": None,
                "scratch_path": None,
            }
            for item in entries
            if isinstance(item, dict)
            and set(item) == {"snapshot_id", "operation_id"}
            and all(isinstance(part, str) for part in item.values())
        ]
    elif value.get("schema_version") == CATALOG_SCHEMA_VERSION and set(value) == {
        "schema_version",
        "tool_version",
        "target",
        "target_id",
        "snapshots",
        "candidates",
        "operations",
    }:
        entries = value["snapshots"]
        candidates = value["candidates"]
        operations = value["operations"]
    else:
        raise CaptureError("artifact_schema_unsupported")
    if not isinstance(entries, list) or any(
        not isinstance(item, dict)
        or set(item) != {"snapshot_id", "operation_id"}
        or not all(isinstance(part, str) for part in item.values())
        for item in entries
    ):
        raise CaptureError("artifact_schema_unsupported")
    if not isinstance(candidates, list) or any(
        not isinstance(item, dict)
        or set(item)
        != {
            "snapshot_id",
            "candidate_id",
            "candidate_size",
            "candidate_sha256",
            "report_sha256",
        }
        or not isinstance(item["snapshot_id"], str)
        or not isinstance(item["candidate_id"], str)
        or type(item["candidate_size"]) is not int
        or item["candidate_size"] < 0
        or any(
            not _valid_digest(item[name])
            for name in ("candidate_sha256", "report_sha256")
        )
        for item in candidates
    ):
        raise CaptureError("artifact_schema_unsupported")
    if not isinstance(operations, list) or any(
        not isinstance(item, dict)
        or set(item) != {"operation_id", "snapshot_id", "state", "host", "scratch_path"}
        or not all(
            isinstance(item[name], str)
            for name in ("operation_id", "snapshot_id", "state")
        )
        or any(
            item[name] is not None and not isinstance(item[name], str)
            for name in ("host", "scratch_path")
        )
        for item in operations
    ):
        raise CaptureError("artifact_schema_unsupported")
    return (
        [dict(item) for item in entries],
        [dict(item) for item in candidates],
        [dict(item) for item in operations],
    )


def _valid_digest(value: object) -> bool:
    """Return whether one persisted value is a lowercase SHA-256 digest."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _write_catalog_state(
    control_dir: Path,
    target: Path,
    target_id: str,
    snapshots: list[dict[str, str]],
    candidates: list[dict[str, object]],
    operations: list[dict[str, str | None]],
) -> None:
    """Write one complete versioned target catalog with private atomic replacement."""
    _write_json(
        control_dir / "catalog.json",
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "tool_version": __version__,
            "target": str(target),
            "target_id": target_id,
            "snapshots": snapshots,
            "candidates": candidates,
            "operations": operations,
        },
    )


def _operation_dict(evidence: OperationEvidence) -> dict[str, str | None]:
    """Return one closed catalog record for read-only operation status."""
    return {
        "operation_id": evidence.operation_id,
        "snapshot_id": evidence.snapshot_id,
        "state": evidence.state,
        "host": evidence.host,
        "scratch_path": evidence.scratch_path,
    }


def _register_candidate(
    snapshot: AcceptedSnapshot,
    candidate_id: str,
    candidate_size: int,
    candidate_sha256: str,
    report_sha256: str,
) -> None:
    """Add one immutable candidate/report identity to its exact target catalog."""
    root = snapshot.snapshot_dir.parent.parent
    snapshots, candidates, operations = _read_catalog(
        root, Path(snapshot.manifest.target), snapshot.manifest.target_id
    )
    if not any(item["snapshot_id"] == snapshot.snapshot_id for item in snapshots):
        raise CaptureError("candidate_persist_failed")
    candidates.append(
        {
            "snapshot_id": snapshot.snapshot_id,
            "candidate_id": candidate_id,
            "candidate_size": candidate_size,
            "candidate_sha256": candidate_sha256,
            "report_sha256": report_sha256,
        }
    )
    _write_catalog_state(
        root,
        Path(snapshot.manifest.target),
        snapshot.manifest.target_id,
        snapshots,
        candidates,
        operations,
    )


def _valid_report(
    report: object,
    target: Path,
    target_id: str,
    record: dict[str, object],
    manifest: SourceManifest,
) -> bool:
    """Check report references without exposing or repairing retained evidence."""
    required = {
        "schema_version",
        "tool_version",
        "snapshot_id",
        "target",
        "target_id",
        "source_manifest_sha256",
        "completeness",
        "completeness_scope",
        "historical_completeness",
        "validation",
        "passive_checkpoint",
        "truncate_checkpoint",
        "wal_evidence",
        "sqlite_version",
        "preview",
        "candidate_id",
        "candidate_sha256",
        "candidate_size",
    }
    return (
        isinstance(report, dict)
        and set(report) == required
        and report.get("schema_version") == 1
        and isinstance(report.get("tool_version"), str)
        and report.get("snapshot_id") == record["snapshot_id"]
        and report.get("target") == str(target)
        and report.get("target_id") == target_id
        and isinstance(report.get("source_manifest_sha256"), str)
        and report.get("source_manifest_sha256")
        == hashlib.sha256(
            json.dumps(
                manifest.to_dict(),
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        and report.get("candidate_id") == record["candidate_id"]
        and report.get("candidate_sha256") == record["candidate_sha256"]
        and report.get("candidate_size") == record["candidate_size"]
        and report.get("completeness") in {"complete", "uncertain"}
    )


def _write_snapshot_manifest(
    directory: Path,
    target: Path,
    target_id: str,
    snapshot_id: str,
    operation_id: str,
    pre: SourceManifest,
    copied: SourceManifest,
    post: SourceManifest,
    state: str,
    reason: str | None,
) -> None:
    """Write the versioned final snapshot state after a bounded capture attempt."""
    _write_json(
        directory / "manifest.json",
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tool_version": __version__,
            "target": str(target),
            "target_id": target_id,
            "snapshot_id": snapshot_id,
            "operation_id": operation_id,
            "state": state,
            "reason": reason,
            "pre_copy_manifest": pre.to_dict(),
            "copied_manifest": copied.to_dict(),
            "post_copy_manifest": post.to_dict(),
        },
    )


def _best_effort_diagnostic_manifest(
    directory: Path,
    target: Path,
    target_id: str,
    snapshot_id: str,
    operation_id: str,
    pre: SourceManifest,
    reason: str,
) -> None:
    """Persist safe incomplete-capture state without replacing source evidence."""
    try:
        _write_json(
            directory / "manifest.json",
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "tool_version": __version__,
                "target": str(target),
                "target_id": target_id,
                "snapshot_id": snapshot_id,
                "operation_id": operation_id,
                "state": "diagnostic_only",
                "reason": reason,
                "pre_copy_manifest": pre.to_dict(),
            },
        )
    except OSError:
        pass


def _write_json(path: Path, value: dict[str, object]) -> None:
    """Atomically write one private canonical JSON artifact and fsync its parent."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    encoded = (
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("ascii")
    try:
        with temporary.open("xb") as writer:
            os.chmod(temporary, 0o600)
            writer.write(encoded)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    """Sync one artifact directory after metadata replacement without SQLite."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_deadline(started: float, deadline_seconds: int) -> None:
    """Raise a bounded error when the finite operation deadline has elapsed."""
    if time.monotonic() - started >= deadline_seconds:
        raise CaptureError("deadline_exceeded")


def _has_journal(manifest: SourceManifest) -> bool:
    """Return whether preserved rollback-journal evidence makes recovery unsupported."""
    return manifest.files[-1].present


def new_artifact_id(prefix: str) -> str:
    """Return a UTC-sortable random ID for an artifact bound by catalog path.

    Parameters: ``prefix`` is a trusted lowercase artifact kind. Returns a
    timestamp-plus-random identifier. Raises :class:`ValueError` for an invalid
    prefix and has no filesystem or database side effects.
    """
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", prefix) is None:
        raise ValueError("invalid artifact ID prefix")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}-{secrets.token_hex(12)}"


def _outcome(
    target: str | None,
    target_id: str | None,
    snapshot_id: str | None,
    operation_id: str | None,
    accepted: bool,
    status: str,
    snapshot_dir: Path | None = None,
) -> CaptureOutcome:
    """Construct a capture result without evaluating filesystem or source state."""
    return CaptureOutcome(
        target=target,
        target_id=target_id,
        snapshot_id=snapshot_id,
        operation_id=operation_id,
        snapshot_dir=snapshot_dir,
        accepted=accepted,
        status=status,
    )
