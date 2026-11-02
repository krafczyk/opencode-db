"""Explicit recoverable cutover for timestamp-synchronized staged prune images.

Source generation is the accepted timestamp/global-metadata profile, not a full
source byte comparison. The operator stops all source clients before cutover.
Original files are renamed into private same-filesystem storage, never deleted
automatically. Legacy cleanup installation and its byte proofs are unchanged.
"""

from collections.abc import Callable
import os
from pathlib import Path
import re
import shutil

from .artifacts import _fsync_directory, _read_canonical_json, _write_json
from .prune import PruneError
from .staged_prune import Budget, StageJob, _private, read_meta, seal_job, source_digest, source_lock, write_meta
from .target import ensure_private_directory, require_capacity, resolve_recorded_target

_FILES = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}
_SAVED = {"main": "original.db", "wal": "original.db-wal", "shm": "captured.shm", "journal": "original.db-journal"}
_KEYS = {"schema_version", "operation_id", "target", "target_id", "source_digest", "candidate_sha256", "phase", "index", "files"}
_PHASES = {"retaining", "promoting", "installed", "rolling_back", "rolled_back"}


def cutover_directory(job: StageJob) -> Path:
    """Derive private same-filesystem staging, independent of the work-store root.

    ``job`` is validated metadata. Returns an adjacent path without creating it
    or reading source bytes; no persisted arbitrary output path is accepted.
    """
    control = resolve_recorded_target(str(job.target)).control_dir
    return control / "staged-prunes" / job.operation_id / "cutover"


def install_job(job: StageJob, timeout_seconds: float, *, progress: Callable[[str], None], after_move: Callable[[int], None] | None = None) -> dict[str, object]:
    """Prepare or resume an explicitly requested stopped-source cutover.

    Caller holds the job lock and arranges source shutdown. Recheck the accepted
    logical source generation before moving files, retain the complete original
    set, verify it again after retention, then promote sealed candidate bytes.
    ``after_move`` is a recovery-test seam. Returns durable installed metadata;
    ambiguity/source changes raise PruneError and preserve every file for exact
    resume or explicit rollback. Never inspects or signals processes.
    """
    with source_lock(job.target, exclusive=True):
        return _install_job(job, timeout_seconds, progress=progress, after_move=after_move)


def _install_job(job, timeout_seconds, *, progress, after_move):
    """Apply one exact cutover while holding both job and source authority."""
    path = job.directory / "cutover.json"
    if not path.exists() and not path.is_symlink():
        meta = seal_job(job, timeout_seconds, progress=progress)
        if meta["phase"] != "ready":
            raise PruneError("staged image is not ready for installation")
        progress("install: checking the final timestamp source generation")
        if source_digest(job.target, timeout_seconds) != meta["source_digest"]:
            raise PruneError("source changed since update; run prune update again before installation")
        directory = cutover_directory(job)
        require_capacity(job.target.parent, job.candidate.stat().st_size + 65536, 8)
        _make_directories(job, directory)
        staged = directory / "install.sqlite"
        if staged.exists() or staged.is_symlink():
            _private(staged, 0o600)
            staged.unlink()
        progress("install: staging the pruned image beside the source")
        _copy_image(job.candidate, staged, timeout_seconds, meta["candidate_sha256"])
        if source_digest(job.target, timeout_seconds) != meta["source_digest"]:
            raise PruneError("source changed during staging; run prune update again")
        files = []
        for name, suffix in _FILES.items():
            current = Path(str(job.target) + suffix)
            if current.is_symlink() or current.exists() and not current.is_file():
                raise PruneError("source file set is unsafe for staged cutover")
            if current.exists():
                stat = current.stat()
                files.append({"name": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        if not files or files[0]["name"] != "main":
            raise PruneError("source main file disappeared before cutover")
        intent = {
            "schema_version": 1, "operation_id": job.operation_id,
            "target": str(job.target), "target_id": job.target_id,
            "source_digest": meta["source_digest"], "candidate_sha256": meta["candidate_sha256"],
            "phase": "retaining", "index": 0, "files": files,
        }
        _write_json(path, intent)
    intent = read_intent(job)
    if intent["phase"] == "installed":
        _mark(job, "installed")
        return read_meta(job)
    if intent["phase"] in {"rolling_back", "rolled_back"}:
        raise PruneError("staged rollback has started; finish rollback instead of installation")
    directory = cutover_directory(job)
    for index, item in enumerate(intent["files"]):
        if index < intent["index"]:
            saved = directory / "original" / _SAVED[item["name"]]
            if not saved.is_file() or saved.is_symlink() or (saved.stat().st_size, saved.stat().st_mtime_ns) != (item["size"], item["mtime_ns"]):
                raise PruneError("retained original evidence changed; files were preserved")
            continue
        progress("install: retaining original file set")
        source = Path(str(job.target) + _FILES[item["name"]])
        destination = directory / "original" / _SAVED[item["name"]]
        _retain(source, destination, item)
        intent["index"] = index + 1
        _write_json(path, intent)
        if after_move:
            after_move(index + 1)
    if intent["phase"] == "retaining":
        progress("install: verifying retained source generation")
        if _saved_generation(job, timeout_seconds) != intent["source_digest"]:
            raise PruneError("source changed at cutover; originals were preserved; use prune rollback")
        intent["phase"] = "promoting"
        _write_json(path, intent)
    staged = directory / "install.sqlite"
    if staged.is_symlink():
        raise PruneError("staged promotion path is unsafe")
    if staged.exists() and not job.target.exists():
        if any(Path(str(job.target) + suffix).exists() or Path(str(job.target) + suffix).is_symlink() for suffix in _FILES.values()):
            raise PruneError("source files or sidecars appeared during cutover; originals were preserved")
        _matches_image(staged, intent["candidate_sha256"], timeout_seconds)
        os.replace(staged, job.target)
        _fsync_directory(job.target.parent)
        _fsync_directory(directory)
        if after_move:
            after_move(len(intent["files"]) + 1)
    elif not staged.exists() and job.target.exists():
        _matches_image(job.target, intent["candidate_sha256"], timeout_seconds)
        if any(Path(str(job.target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise PruneError("the promoted source has new sidecars; preserve it and inspect cutover state")
    else:
        raise PruneError("staged promotion is ambiguous; files were preserved")
    intent["phase"] = "installed"
    _write_json(path, intent)
    _mark(job, "installed")
    progress("install: completed; original files remain in the private backup")
    return read_meta(job)


def rollback_job(job: StageJob, timeout_seconds: float, *, progress: Callable[[str], None], after_move: Callable[[int], None] | None = None) -> dict[str, object]:
    """Explicitly restore exact retained original paths without discarding new work.

    Caller holds job_lock and stops clients. A promoted image must still match
    sealed bytes with no new sidecars before it can be retained as a retired
    candidate. Interrupted retention/rollback is reconciled by exact path and
    original metadata, followed by source-generation verification. Returns
    rolled-back metadata; ambiguity refuses without overwriting either side.
    """
    with source_lock(job.target, exclusive=True):
        return _rollback_job(job, timeout_seconds, progress=progress, after_move=after_move)


def _rollback_job(job, timeout_seconds, *, progress, after_move):
    """Restore originals while excluding other staged source readers and movers."""
    intent = read_intent(job)
    if intent["phase"] == "rolled_back":
        _mark(job, "rolled_back")
        return read_meta(job)
    directory = cutover_directory(job)
    original = directory / "original" / "original.db"
    retired = directory / "retired.sqlite"
    _preflight_rollback(job, intent)
    if job.target.exists() and original.exists():
        if intent["phase"] == "retaining":
            raise PruneError("both original main paths exist; rollback is ambiguous")
        _matches_image(job.target, intent["candidate_sha256"], timeout_seconds)
        if any(Path(str(job.target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise PruneError("active state changed after installation; rollback refused")
        if retired.exists() or retired.is_symlink():
            raise PruneError("retired candidate path is occupied")
        intent["phase"] = "rolling_back"
        _write_json(job.directory / "cutover.json", intent)
        os.replace(job.target, retired)
        _fsync_directory(job.target.parent)
        _fsync_directory(directory)
    intent["phase"] = "rolling_back"
    _write_json(job.directory / "cutover.json", intent)
    for index, item in enumerate(intent["files"]):
        source = directory / "original" / _SAVED[item["name"]]
        destination = Path(str(job.target) + _FILES[item["name"]])
        progress("rollback: restoring the original file set")
        _retain(source, destination, item)
        if after_move:
            after_move(index + 1)
    if source_digest(job.target, timeout_seconds) != intent["source_digest"]:
        raise PruneError("restored source generation differs; files were preserved for inspection")
    intent["phase"] = "rolled_back"
    _write_json(job.directory / "cutover.json", intent)
    _mark(job, "rolled_back")
    return read_meta(job)


def read_intent(job: StageJob) -> dict[str, object]:
    """Read a closed, target/job-bound cutover log; never inspect source payloads.

    Returns validated metadata or raises PruneError/CaptureError for future,
    malformed or unsafe state. All move paths are derived, never read from JSON.
    """
    path = job.directory / "cutover.json"
    if path.stat().st_size > 65536:
        raise PruneError("staged cutover metadata exceeds supported bounds")
    value, _ = _read_canonical_json(path)
    meta = read_meta(job)
    if not isinstance(value, dict) or set(value) != _KEYS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PruneError("staged cutover schema is unsupported")
    if (value["operation_id"], value["target"], value["target_id"], value["source_digest"], value["candidate_sha256"]) != (
        job.operation_id, str(job.target), job.target_id, meta["source_digest"], meta["candidate_sha256"],
    ):
        raise PruneError("staged cutover binding changed")
    if not isinstance(value["phase"], str) or value["phase"] not in _PHASES or not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= 4 or type(value["index"]) is not int or not 0 <= value["index"] <= len(value["files"]):
        raise PruneError("staged cutover state is malformed")
    names = []
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {"name", "size", "mtime_ns"} or not isinstance(item["name"], str) or item["name"] not in _FILES or type(item["size"]) is not int or item["size"] < 0 or type(item["mtime_ns"]) is not int:
            raise PruneError("staged original file evidence is malformed")
        names.append(item["name"])
    if names[0] != "main" or len(set(names)) != len(names):
        raise PruneError("staged original file set is malformed")
    if value["phase"] in {"promoting", "installed"} and value["index"] != len(value["files"]):
        raise PruneError("staged cutover progress is contradictory")
    return value


def discard_job(job: StageJob) -> None:
    """Explicitly remove exact private job artifacts and eligible original backups.

    Caller holds job_lock. Incomplete cutover/rollback and unlisted files refuse.
    This never removes the active database or another job. Installed backups are
    retired only by this explicit opaque-ID request; there is no expiry/cleanup.
    """
    cutover = job.directory / "cutover.json"
    if cutover.exists() or cutover.is_symlink():
        intent = read_intent(job)
        if intent["phase"] not in {"installed", "rolled_back"}:
            raise PruneError("finish staged installation or rollback before discarding its recovery files")
    adjacent = cutover_directory(job).parent
    allowed = {"job.json", "state.sqlite", "state.sqlite-journal", "candidate.sqlite", "candidate.sqlite-journal", "candidate.sqlite-wal", "candidate.sqlite-shm", ".lock", "cutover.json", "cutover"}
    for entry in job.directory.iterdir():
        if entry.is_symlink() or entry.name not in allowed and re.fullmatch(r"candidate\.sqlite-mj[0-9A-Za-z]{1,64}", entry.name) is None:
            raise PruneError("unlisted staged artifacts prevent discard")
        if entry.is_dir() and entry != cutover_directory(job) or not entry.is_dir() and not entry.is_file():
            raise PruneError("unexpected staged artifact type prevents discard")
    directory = cutover_directory(job)
    if adjacent != job.directory and adjacent.exists() and {entry.name for entry in adjacent.iterdir()} - {"cutover"}:
        raise PruneError("unlisted adjacent staged files prevent discard")
    if directory.exists():
        _private(directory, 0o700)
        for entry in directory.iterdir():
            if entry.is_symlink() or entry.name not in {"install.sqlite", "retired.sqlite", "original"}:
                raise PruneError("unlisted cutover artifacts prevent discard")
            if entry.name != "original" and not entry.is_file():
                raise PruneError("unexpected cutover artifact type prevents discard")
            if entry.name == "original":
                _private(entry, 0o700)
                if any(child.is_symlink() or not child.is_file() or child.name not in set(_SAVED.values()) | {"original.db-shm"} for child in entry.iterdir()):
                    raise PruneError("unlisted original backup files prevent discard")
        shutil.rmtree(directory)
    if adjacent != job.directory and adjacent.exists():
        if list(adjacent.iterdir()):
            raise PruneError("unlisted adjacent staged files prevent discard")
        adjacent.rmdir()
    shutil.rmtree(job.directory)
    _fsync_directory(job.directory.parent)


def _make_directories(job, directory):
    control = resolve_recorded_target(str(job.target)).control_dir
    for path in (control.parent, control, control / "staged-prunes", directory.parent, directory, directory / "original"):
        ensure_private_directory(path)
    if directory.stat().st_dev != job.target.parent.stat().st_dev:
        raise PruneError("staged cutover must stay on the source filesystem")


def _copy_image(source, destination, timeout, expected):
    budget = Budget(timeout)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            while block := reader.read(1024 * 1024):
                budget.check()
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        _matches_image(destination, expected, timeout)
        _fsync_directory(destination.parent)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _matches_image(path, expected, timeout):
    _private(path, 0o600)
    budget = Budget(timeout)
    if budget.digest(path) != expected:
        raise PruneError("staged image changed; refusing replacement or rollback")


def _retain(source, destination, evidence):
    if source.is_symlink() or destination.is_symlink() or source.exists() == destination.exists():
        raise PruneError("staged file movement is ambiguous; files were preserved")
    existing = source if source.exists() else destination
    if not existing.is_file() or (existing.stat().st_size, existing.stat().st_mtime_ns) != (evidence["size"], evidence["mtime_ns"]):
        raise PruneError("original file metadata changed during staged movement")
    if source.exists():
        os.replace(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)


def _preflight_rollback(job, intent):
    """Validate the entire original/active set before restoring even its main file."""
    records = {item["name"]: item for item in intent["files"]}
    directory = cutover_directory(job) / "original"
    for name, suffix in _FILES.items():
        active = Path(str(job.target) + suffix)
        saved = directory / _SAVED[name]
        if active.is_symlink() or saved.is_symlink():
            raise PruneError("staged rollback paths are unsafe; files were preserved")
        if name not in records:
            if active.exists() or saved.exists():
                raise PruneError("new sidecars prevent staged rollback; files were preserved")
            continue
        if active.exists() and saved.exists():
            if name != "main" or intent["phase"] == "retaining":
                raise PruneError("staged rollback file movement is ambiguous; files were preserved")
            existing = saved
        elif active.exists() != saved.exists():
            existing = saved if saved.exists() else active
        else:
            raise PruneError("staged rollback original file is missing; files were preserved")
        evidence = records[name]
        if not existing.is_file() or (existing.stat().st_size, existing.stat().st_mtime_ns) != (evidence["size"], evidence["mtime_ns"]):
            raise PruneError("original file metadata changed before staged rollback")


def _saved_generation(job, timeout):
    directory = cutover_directory(job) / "original"
    cache = directory / "original.db-shm"
    if cache.is_symlink() or cache.exists() and not cache.is_file():
        raise PruneError("temporary retained-source cache is unsafe")
    cache.unlink(missing_ok=True)
    try:
        return source_digest(directory / "original.db", timeout)
    finally:
        cache.unlink(missing_ok=True)


def _mark(job, phase):
    meta = read_meta(job)
    write_meta(job, {**meta, "phase": phase})
