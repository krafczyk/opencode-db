"""Create private, immutable, copy-only SQLite source snapshots.

The functions here read operator-selected source bytes with ordinary file I/O.
They never import SQLite, invoke OpenCode, or writable-open active or retained
database files. Later units may consume only accepted snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from . import __version__
from .model import AcceptedSnapshot, CaptureOutcome, SourceFile, SourceManifest
from .target import (
    TargetEnvironment,
    TargetError,
    admit_scratch,
    ensure_private_directory,
    resolve_target,
    storage_space,
)

COPY_CHUNK_BYTES = 1024 * 1024
"""Bounded streaming-copy chunk size for source and retained artifact files."""

MANIFEST_SCHEMA_VERSION = 1
"""Schema version for target catalogs and retained snapshot manifests."""

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
        catalog_entries = _read_catalog(
            target.control_dir, target.path, target.target_id
        )
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
            catalog_entries,
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
    except (CaptureError, OSError) as error:
        destination.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        if isinstance(error, CaptureError):
            raise
        raise CaptureError("candidate_persist_failed") from error
    return destination, report_path, report_digest


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
    existing_entries: list[dict[str, str]],
) -> None:
    """Persist the minimal private target catalog without opening source data."""
    path = control_dir / "catalog.json"
    entries = [*existing_entries]
    entries.append({"snapshot_id": snapshot_id, "operation_id": operation_id})
    _write_json(
        path,
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tool_version": __version__,
            "target": str(target),
            "target_id": target_id,
            "snapshots": entries,
        },
    )


def _read_catalog(
    control_dir: Path, target: Path, target_id: str
) -> list[dict[str, str]]:
    """Read one compatible catalog before allocating a snapshot directory.

    Returns prior exact snapshot/operation ID pairs. Raises :class:`CaptureError`
    for malformed, foreign-target, or future catalog state without creating a
    snapshot or copying source bytes. Catalog reads do not open SQLite.
    """
    path = control_dir / "catalog.json"
    try:
        if path.is_symlink():
            raise CaptureError("artifact_schema_unsupported")
        if not path.exists():
            return []
        if path.stat().st_mode & 0o777 != 0o600:
            raise CaptureError("artifact_not_private")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureError("artifact_schema_unsupported") from error
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "tool_version", "target", "target_id", "snapshots"}
        or value["schema_version"] != MANIFEST_SCHEMA_VERSION
        or value["target"] != str(target)
        or value["target_id"] != target_id
        or not isinstance(value["tool_version"], str)
        or not isinstance(value["snapshots"], list)
    ):
        raise CaptureError("artifact_schema_unsupported")
    entries = value["snapshots"]
    if any(
        not isinstance(item, dict)
        or set(item) != {"snapshot_id", "operation_id"}
        or not all(isinstance(part, str) for part in item.values())
        for item in entries
    ):
        raise CaptureError("artifact_schema_unsupported")
    return [dict(item) for item in entries]


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
