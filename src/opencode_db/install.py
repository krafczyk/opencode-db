"""Install reviewed candidates through a durable, per-file recovery protocol.

The active SQLite main file and sidecars are deliberately not treated as one
atomic object.  This module records and syncs an exact move intent before every
active-path mutation, so a fresh process can reconcile only the recorded before
or after bytes.  It never discovers, starts, or coordinates OpenCode.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Callable, cast

from . import __version__
from .artifacts import (
    CaptureError,
    load_install_operation,
    new_artifact_id,
    register_install_operation,
    select_candidate,
    set_install_operation_state,
    snapshot_source_path,
)
from .model import (
    AcceptedSnapshot,
    CandidateEvidence,
    Check,
    InstallOutcome,
    SourceFile,
    Validation,
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

_SUFFIXES = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}
_INSTALL_STATES = {
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


class InstallError(RuntimeError):
    """Report a bounded install or recovery error without filesystem detail.

    ``code`` is a stable diagnostic identifier.  Callers convert it to public
    output without exposing arbitrary path content or SQLite error messages.
    """

    def __init__(self, code: str) -> None:
        """Store the public-safe failure ``code``."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InstallHooks:
    """Provide deterministic interruption seams at durable mutation boundaries.

    Each optional callback receives the install ``operation_id`` after the named
    state has been synced.  Production callers use no hooks; tests may raise or
    terminate a process to model abrupt loss without mocking filesystem moves.
    """

    after_original_retained: Callable[[str], None] | None = None
    after_candidate_promoted: Callable[[str], None] | None = None
    during_rollback: Callable[[str], None] | None = None


def install_candidate(
    database: str,
    candidate: CandidateEvidence,
    *,
    approve_uncertain_report: str | None = None,
    scratch_dir: str,
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
    hooks: InstallHooks | None = None,
) -> InstallOutcome:
    """Install one revalidated exact candidate after a final source check.

    ``database`` and ``candidate`` must have been selected by
    :func:`opencode_db.artifacts.select_candidate`; ``scratch_dir`` is an
    admitted local root, ``approve_uncertain_report`` must equal the report
    digest for uncertain evidence, and the deadline is finite. Returns a
    terminal or incomplete :class:`InstallOutcome`. Before active mutation it
    refuses on changed evidence; after mutation it leaves durable recovery
    state. It opens SQLite only for non-migrating candidate and active
    validation and never invokes OpenCode or inspects processes.
    """
    started = time.monotonic()
    try:
        _deadline(started, deadline_seconds)
        selected = select_candidate(
            database,
            candidate.candidate_id,
            approve_uncertain_report,
        )
        if selected != candidate:
            raise InstallError("candidate_changed")
        target = resolve_target(database)
        snapshot = _load_snapshot_for_candidate(database, selected)
        _require_manifest_matches(
            target.path, snapshot.manifest.files, started, deadline_seconds
        )
        staged = _stage_candidate(
            target.path,
            snapshot.snapshot_dir,
            selected,
            scratch_dir,
            deadline_seconds,
            environment,
            started,
        )
        # This is deliberately the last source observation before intent/moves.
        _require_manifest_matches(
            target.path, snapshot.manifest.files, started, deadline_seconds
        )
        _fsync_directory(target.path.parent)
        operation_id = new_artifact_id("install")
        snapshot = register_install_operation(database, selected, operation_id)
        state = _new_state(
            target.path, target.target_id, snapshot, selected, operation_id, staged
        )
        _write_state(_state_path(snapshot.snapshot_dir, operation_id), state)
        return _advance_install(
            database, state, started, deadline_seconds, hooks, environment
        )
    except (CaptureError, TargetError, InstallError) as error:
        code = error.code
        return _failure_outcome("unknown", "unknown", "unknown", code)
    except (OSError, sqlite3.Error):
        return _failure_outcome("unknown", "unknown", "unknown", "install_failed")


def install_from_selection(
    database: str,
    candidate_id: str,
    approve_uncertain_report: str | None,
    *,
    scratch_dir: str,
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
) -> InstallOutcome:
    """Select and install one exact candidate through the public CLI boundary.

    Parameters are explicit operator values; uncertain evidence requires its
    exact report digest.  Returns the same outcomes as :func:`install_candidate`
    and raises no raw filesystem or SQLite errors.  This helper does not inspect
    processes or invoke OpenCode.
    """
    try:
        candidate = select_candidate(database, candidate_id, approve_uncertain_report)
    except CaptureError as error:
        return _failure_outcome("unknown", "unknown", candidate_id, error.code)
    return install_candidate(
        database,
        candidate,
        approve_uncertain_report=approve_uncertain_report,
        scratch_dir=scratch_dir,
        deadline_seconds=deadline_seconds,
        environment=environment,
    )


def install_status(database: str, operation_id: str) -> InstallOutcome:
    """Read one installation intent even when the active main path is absent.

    Returns the exact persisted phase and identifiers without changing files or
    opening SQLite.  Raises :class:`InstallError` for malformed, foreign, or
    future state, which callers must treat as a read-only recovery refusal.
    """
    snapshot = load_install_operation(database, operation_id)
    state = _read_state(
        _state_path(snapshot.snapshot_dir, operation_id), database, operation_id
    )
    return _outcome_from_state(state)


def resume_install(
    database: str,
    operation_id: str,
    *,
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
    hooks: InstallHooks | None = None,
) -> InstallOutcome:
    """Continue one unambiguous persisted installation mutation.

    The function accepts only exact recorded before/after bytes.  Ambiguous,
    changed, or missing artifacts become ``manual_recovery_required`` without
    deletion.  It uses the original staged candidate and retained snapshot, and
    never starts OpenCode or prompts for service state.
    """
    started = time.monotonic()
    try:
        snapshot = load_install_operation(database, operation_id)
        state = _read_state(
            _state_path(snapshot.snapshot_dir, operation_id), database, operation_id
        )
        if state["state"] in {"installed", "rolled_back", "manual_recovery_required"}:
            return _outcome_from_state(state)
        return _advance_install(
            database, state, started, deadline_seconds, hooks, environment
        )
    except (CaptureError, TargetError, InstallError) as error:
        return _manual_or_failure(database, operation_id, error.code)
    except (OSError, sqlite3.Error):
        return _manual_or_failure(database, operation_id, "install_failed")


def rollback_install(
    database: str,
    operation_id: str,
    *,
    deadline_seconds: int = 1_800,
    environment: TargetEnvironment | None = None,
    hooks: InstallHooks | None = None,
) -> InstallOutcome:
    """Restore immutable accepted source bytes for one incomplete installation.

    The explicit rollback first stages copies of the immutable snapshot and then
    records per-file replacement intents.  Quarantined originals are accepted
    only when hash-identical to the snapshot.  Unknown active bytes produce
    ``manual_recovery_required`` and leave every unmatched artifact intact.
    """
    started = time.monotonic()
    try:
        snapshot = load_install_operation(database, operation_id)
        path = _state_path(snapshot.snapshot_dir, operation_id)
        state = _read_state(path, database, operation_id)
        if state["state"] == "rolled_back":
            return _outcome_from_state(state)
        if state["state"] in {"installed", "manual_recovery_required"}:
            raise InstallError("manual_recovery_required")
        _deadline(started, deadline_seconds)
        _prepare_rollback(snapshot.snapshot_dir, state, started, deadline_seconds)
        state["state"] = "rolling_back"
        state["rollback"] = True
        state["index"] = 0
        state["actions"] = _rollback_actions(snapshot.snapshot_dir, state)
        _write_state(path, state)
        set_install_operation_state(database, operation_id, "rolling_back")
        return _advance_install(
            database, state, started, deadline_seconds, hooks, environment
        )
    except (CaptureError, TargetError, InstallError) as error:
        return _manual_or_failure(database, operation_id, error.code)
    except (OSError, sqlite3.Error):
        return _manual_or_failure(database, operation_id, "install_failed")


def _advance_install(
    database: str,
    state: dict[str, object],
    started: float,
    deadline_seconds: int,
    hooks: InstallHooks | None,
    environment: TargetEnvironment | None,
) -> InstallOutcome:
    """Reconcile and perform the sole recorded next mutation until terminal."""
    _ = environment
    operation_id = _string(state, "operation_id")
    snapshot_dir = Path(_string(state, "snapshot_dir"))
    path = _state_path(snapshot_dir, operation_id)
    active_mutated = _int(state, "index") > 0
    try:
        while _int(state, "index") < len(_actions(state)):
            _deadline(started, deadline_seconds)
            index = _int(state, "index")
            action = _actions(state)[index]
            reconciled = _reconcile_action(action)
            if reconciled == "after":
                active_mutated = True
                state["index"] = index + 1
                _write_state(path, state)
                continue
            if reconciled != "before":
                raise InstallError("manual_recovery_required")
            phase = (
                "rolling_back"
                if state["state"] == "rolling_back"
                else _action_string(action, "phase")
            )
            state["state"] = phase
            _write_state(path, state)  # durable intent before the active move
            set_install_operation_state(database, operation_id, phase)
            # From this point recovery must assume replacement may have occurred,
            # including when a post-replace directory fsync raises.
            active_mutated = True
            _move(
                Path(_action_string(action, "source")),
                Path(_action_string(action, "destination")),
            )
            _require_after(action)
            state["index"] = index + 1
            _write_state(path, state)
            if (
                _action_string(action, "kind") == "retain"
                and hooks
                and hooks.after_original_retained
            ):
                hooks.after_original_retained(operation_id)
            if (
                _action_string(action, "kind") == "promote"
                and hooks
                and hooks.after_candidate_promoted
            ):
                hooks.after_candidate_promoted(operation_id)
            if (
                _action_string(action, "kind") == "restore"
                and hooks
                and hooks.during_rollback
            ):
                hooks.during_rollback(operation_id)
        if state["rollback"] is True:
            _require_manifest_matches(
                Path(_string(state, "target")),
                _snapshot_files(snapshot_dir),
                started,
                deadline_seconds,
            )
            state["state"] = "rolled_back"
            _write_state(path, state)
            set_install_operation_state(database, operation_id, "rolled_back")
            return _outcome_from_state(state)
        state["state"] = "validating"
        _write_state(path, state)
        set_install_operation_state(database, operation_id, "validating")
        validation = _validate_active(
            Path(_string(state, "target")), started, deadline_seconds
        )
        if validation != _passing_validation():
            raise InstallError("validation_failed")
        state["state"] = "installed"
        _write_state(path, state)
        set_install_operation_state(database, operation_id, "installed")
        return _outcome_from_state(state, validation)
    except (OSError, sqlite3.Error, InstallError) as error:
        code = error.code if isinstance(error, InstallError) else "install_failed"
        if active_mutated:
            state["state"] = (
                "manual_recovery_required"
                if code == "manual_recovery_required"
                else "install_incomplete"
            )
            _write_state(path, state)
            set_install_operation_state(database, operation_id, _string(state, "state"))
            return _outcome_from_state(state)
        raise InstallError(code) from error


def _stage_candidate(
    target: Path,
    snapshot_dir: Path,
    candidate: CandidateEvidence,
    scratch_dir: str,
    deadline_seconds: int,
    environment: TargetEnvironment | None,
    started: float,
) -> Path:
    """Revalidate a fresh local candidate copy and stage it beside the target."""
    candidate_size = candidate.candidate_path.stat().st_size
    scratch = admit_scratch(
        Path(scratch_dir), candidate_size * 2 + 65536, 8, environment
    )
    ensure_private_directory(scratch)
    local = scratch / f"install-verify-{secrets.token_hex(12)}"
    ensure_private_directory(local)
    copied = local / "candidate.sqlite"
    try:
        _copy_checked(
            candidate.candidate_path,
            copied,
            candidate.candidate_sha256,
            started,
            deadline_seconds,
        )
        if _validate_active(copied, started, deadline_seconds) != _passing_validation():
            raise InstallError("validation_failed")
    finally:
        shutil.rmtree(local, ignore_errors=True)
    stage_dir = snapshot_dir / "staging"
    ensure_private_directory(stage_dir)
    required = candidate_size + 65536
    capacity = storage_space(target.parent, environment)
    if capacity.available_bytes < required or capacity.available_inodes < 3:
        raise InstallError("storage_capacity")
    if stage_dir.stat().st_dev != target.parent.stat().st_dev:
        raise InstallError("staging_not_local")
    staged = stage_dir / f"{candidate.candidate_id}.sqlite"
    if staged.exists():
        if _digest(staged, started, deadline_seconds) != candidate.candidate_sha256:
            raise InstallError("candidate_changed")
    else:
        _copy_checked(
            candidate.candidate_path,
            staged,
            candidate.candidate_sha256,
            started,
            deadline_seconds,
        )
    return staged


def _load_snapshot_for_candidate(database: str, candidate: CandidateEvidence):
    """Load the exact snapshot bound to a candidate without accepting path replay."""
    target = resolve_target(database)
    from .artifacts import load_accepted_snapshot

    return load_accepted_snapshot(
        target.control_dir / "snapshots" / candidate.snapshot_id
    )


def _new_state(
    target: Path,
    target_id: str,
    snapshot: AcceptedSnapshot,
    candidate: CandidateEvidence,
    operation_id: str,
    staged: Path,
) -> dict[str, object]:
    """Create a closed durable state with one initial intent per active file."""
    snapshot_dir = snapshot.snapshot_dir
    manifest = snapshot.manifest
    quarantine = snapshot_dir / "quarantine" / operation_id
    ensure_private_directory(snapshot_dir / "quarantine")
    ensure_private_directory(quarantine)
    actions: list[dict[str, str | None]] = []
    for source in manifest.files:
        if source.present:
            active = Path(f"{target}{_SUFFIXES[source.name]}")
            actions.append(
                _action(
                    "retain",
                    "quarantining",
                    active,
                    quarantine / source.name,
                    source.sha256,
                )
            )
    actions.append(
        _action("promote", "promoting", staged, target, candidate.candidate_sha256)
    )
    return {
        "schema_version": 1,
        "tool_version": __version__,
        "operation_id": operation_id,
        "snapshot_id": snapshot.snapshot_id,
        "candidate_id": candidate.candidate_id,
        "candidate_sha256": candidate.candidate_sha256,
        "report_sha256": candidate.report_sha256,
        "target": str(target),
        "target_id": target_id,
        "snapshot_dir": str(snapshot_dir),
        "state": "prepared",
        "rollback": False,
        "index": 0,
        "actions": actions,
    }


def _action(
    kind: str,
    phase: str,
    source: Path,
    destination: Path,
    digest: str | None,
    destination_before: str | None = None,
) -> dict[str, str | None]:
    """Build one closed per-file intent record before its future move."""
    if digest is None:
        raise InstallError("snapshot_invalid")
    return {
        "kind": kind,
        "phase": phase,
        "source": str(source),
        "destination": str(destination),
        "source_before_sha256": digest,
        "destination_before_sha256": destination_before,
        "source_after_sha256": None,
        "destination_after_sha256": digest,
    }


def _prepare_rollback(
    snapshot_dir: Path, state: dict[str, object], started: float, deadline_seconds: int
) -> None:
    """Stage immutable originals for rollback without changing any active path."""
    target = Path(_string(state, "target"))
    snapshot = load_install_operation(str(target), _string(state, "operation_id"))
    rollback = snapshot_dir / "rollback" / _string(state, "operation_id")
    ensure_private_directory(snapshot_dir / "rollback")
    ensure_private_directory(rollback)
    for source in snapshot.manifest.files:
        active = Path(f"{target}{_SUFFIXES[source.name]}")
        if not source.present:
            if active.exists():
                raise InstallError("manual_recovery_required")
            continue
        assert source.sha256 is not None
        staged = rollback / source.name
        if staged.exists():
            if _digest(staged, started, deadline_seconds) != source.sha256:
                raise InstallError("manual_recovery_required")
        else:
            _copy_checked(
                snapshot_source_path(
                    snapshot.snapshot_dir, snapshot.manifest, source.name
                ),
                staged,
                source.sha256,
                started,
                deadline_seconds,
            )


def _rollback_actions(
    snapshot_dir: Path, state: dict[str, object]
) -> list[dict[str, str | None]]:
    """Return restoration intents from staged immutable bytes to active names."""
    snapshot = load_install_operation(
        _string(state, "target"), _string(state, "operation_id")
    )
    rollback = snapshot_dir / "rollback" / _string(state, "operation_id")
    actions: list[dict[str, str | None]] = []
    for source in snapshot.manifest.files:
        if not source.present:
            continue
        assert source.sha256 is not None
        destination = Path(f"{_string(state, 'target')}{_SUFFIXES[source.name]}")
        observed = _digest_if_regular(destination)
        if observed is not None and observed != _string(state, "candidate_sha256"):
            raise InstallError("manual_recovery_required")
        actions.append(
            _action(
                "restore",
                "rolling_back",
                rollback / source.name,
                destination,
                source.sha256,
                observed,
            )
        )
    return actions


def _reconcile_action(action: dict[str, str | None]) -> str:
    """Classify an intent only as exact recorded before, after, or unknown."""
    source = Path(_action_string(action, "source"))
    destination = Path(_action_string(action, "destination"))
    source_digest = _digest_if_regular(source)
    destination_digest = _digest_if_regular(destination)
    if (
        source_digest == action["source_before_sha256"]
        and destination_digest == action["destination_before_sha256"]
    ):
        return "before"
    if (
        source_digest == action["source_after_sha256"]
        and destination_digest == action["destination_after_sha256"]
    ):
        return "after"
    return "unknown"


def _require_after(action: dict[str, str | None]) -> None:
    """Require the exact logged after-state following a same-filesystem move."""
    if _reconcile_action(action) != "after":
        raise InstallError("manual_recovery_required")


def _move(source: Path, destination: Path) -> None:
    """Move one staged regular file and sync both metadata directories."""
    if source.parent.stat().st_dev != destination.parent.stat().st_dev:
        raise InstallError("staging_not_local")
    os.replace(source, destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _validate_active(path: Path, started: float, deadline_seconds: int) -> Validation:
    """Run non-migrating integrity, FK, clean reopen, and sidecar checks."""
    integrity = foreign_keys = clean_reopen = sidecars = Check.FAIL
    try:
        _deadline(started, deadline_seconds)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=1.0)
        try:
            connection.set_progress_handler(
                lambda: int(time.monotonic() - started >= deadline_seconds), 1000
            )
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
        _deadline(started, deadline_seconds)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=1.0)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
            clean_reopen = Check.PASS
        finally:
            connection.close()
        sidecars = (
            Check.PASS
            if not any(
                Path(f"{path}{suffix}").exists()
                for suffix in ("-wal", "-shm", "-journal")
            )
            else Check.FAIL
        )
    except (OSError, sqlite3.Error, InstallError):
        pass
    return Validation(integrity, foreign_keys, Check.PASS, clean_reopen, sidecars)


def _require_manifest_matches(
    target: Path, files: tuple[SourceFile, ...], started: float, deadline_seconds: int
) -> None:
    """Require exact expected source presence, size, and digest before mutation."""
    for source in files:
        _deadline(started, deadline_seconds)
        path = Path(f"{target}{_SUFFIXES[source.name]}")
        actual = _digest_if_regular(path, started, deadline_seconds)
        if source.present:
            if actual != source.sha256 or path.stat().st_size != source.size:
                raise InstallError("source_changed")
        elif actual is not None or path.exists() or path.is_symlink():
            raise InstallError("source_changed")


def _snapshot_files(snapshot_dir: Path) -> tuple[SourceFile, ...]:
    """Load source file identities from the operation's immutable snapshot."""
    state_files = load_install_operation
    _ = state_files
    # The caller already has a state directory; use its manifest through a path-independent read.
    from .artifacts import load_accepted_snapshot

    return load_accepted_snapshot(snapshot_dir).manifest.files


def _copy_checked(
    source: Path, destination: Path, digest: str, started: float, deadline_seconds: int
) -> None:
    """Copy one regular file privately, fsync it, and require its exact digest."""
    observed = hashlib.sha256()
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            os.chmod(destination, 0o600)
            while chunk := reader.read(1024 * 1024):
                _deadline(started, deadline_seconds)
                writer.write(chunk)
                observed.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as error:
        raise InstallError("install_failed") from error
    if observed.hexdigest() != digest:
        raise InstallError("candidate_changed")


def _write_state(path: Path, state: dict[str, object]) -> None:
    """Atomically publish one closed intent log and fsync its directory."""
    _validate_state(state)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    data = (
        json.dumps(state, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("ascii")
    try:
        with temporary.open("xb") as writer:
            os.chmod(temporary, 0o600)
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise InstallError("state_write_failed") from error


def _read_state(path: Path, database: str, operation_id: str) -> dict[str, object]:
    """Read one closed intent log without inferring recovery authority from paths."""
    try:
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise InstallError("manual_recovery_required")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("manual_recovery_required") from error
    if not isinstance(value, dict):
        raise InstallError("manual_recovery_required")
    _validate_state(value)
    target = resolve_recorded_target(database)
    if (
        value["operation_id"] != operation_id
        or value["target"] != str(target.path)
        or value["target_id"] != target.target_id
    ):
        raise InstallError("manual_recovery_required")
    return value


def _validate_state(value: dict[str, object]) -> None:
    """Reject unknown or malformed persisted install protocol versions read-only."""
    fields = {
        "schema_version",
        "tool_version",
        "operation_id",
        "snapshot_id",
        "candidate_id",
        "candidate_sha256",
        "report_sha256",
        "target",
        "target_id",
        "snapshot_dir",
        "state",
        "rollback",
        "index",
        "actions",
    }
    if (
        set(value) != fields
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("state") not in _INSTALL_STATES
    ):
        raise InstallError("manual_recovery_required")
    if (
        not all(
            isinstance(value.get(name), str)
            for name in fields
            - {"schema_version", "rollback", "index", "actions", "state"}
        )
        or type(value.get("rollback")) is not bool
        or type(value.get("index")) is not int
        or not isinstance(value.get("actions"), list)
    ):
        raise InstallError("manual_recovery_required")
    actions = value["actions"]
    assert isinstance(actions, list)
    for action in actions:
        if (
            not isinstance(action, dict)
            or set(action)
            != {
                "kind",
                "phase",
                "source",
                "destination",
                "source_before_sha256",
                "destination_before_sha256",
                "source_after_sha256",
                "destination_after_sha256",
            }
            or not all(
                isinstance(action[name], str)
                for name in ("kind", "phase", "source", "destination")
            )
            or not all(
                action[name] is None or _valid_digest(action[name])
                for name in (
                    "source_before_sha256",
                    "destination_before_sha256",
                    "source_after_sha256",
                    "destination_after_sha256",
                )
            )
            or action["kind"] not in {"retain", "promote", "restore"}
            or action["phase"] not in {"quarantining", "promoting", "rolling_back"}
        ):
            raise InstallError("manual_recovery_required")
    index = value["index"]
    assert type(index) is int
    if index < 0 or index > len(actions):
        raise InstallError("manual_recovery_required")
    _validate_action_paths(value, actions)


def _validate_action_paths(state: dict[str, object], actions: list[object]) -> None:
    """Confine every persisted move to the exact target and snapshot namespaces."""
    target = Path(_string(state, "target"))
    snapshot_id = _string(state, "snapshot_id")
    target_id = _string(state, "target_id")
    operation_id = _string(state, "operation_id")
    candidate_id = _string(state, "candidate_id")
    snapshot_dir = Path(_string(state, "snapshot_dir"))
    expected_snapshot = (
        target.parent / ".opencode-db" / target_id / "snapshots" / snapshot_id
    )
    if (
        not target.is_absolute()
        or not snapshot_dir.is_absolute()
        or snapshot_dir != expected_snapshot
    ):
        raise InstallError("manual_recovery_required")
    active = {
        str(Path(f"{target}{suffix}")): name for name, suffix in _SUFFIXES.items()
    }
    for raw in actions:
        if not isinstance(raw, dict):
            raise InstallError("manual_recovery_required")
        action = cast(dict[str, str | None], raw)
        kind = _action_string(action, "kind")
        source = Path(_action_string(action, "source"))
        destination = Path(_action_string(action, "destination"))
        if kind == "retain":
            name = active.get(str(source))
            expected = snapshot_dir / "quarantine" / operation_id / str(name)
            valid = name is not None and destination == expected
        elif kind == "promote":
            valid = (
                source == snapshot_dir / "staging" / f"{candidate_id}.sqlite"
                and destination == target
            )
        else:
            name = active.get(str(destination))
            expected = snapshot_dir / "rollback" / operation_id / str(name)
            valid = name is not None and source == expected
        if not valid:
            raise InstallError("manual_recovery_required")


def _valid_digest(value: object) -> bool:
    """Return whether one intent value is a lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _state_path(snapshot_dir: Path, operation_id: str) -> Path:
    """Return the exact state filename for one operation without creating it."""
    return snapshot_dir / f"{operation_id}.install.json"


def _outcome_from_state(
    state: dict[str, object], validation: Validation | None = None
) -> InstallOutcome:
    """Convert closed persisted state to a public typed recovery observation."""
    return InstallOutcome(
        _string(state, "operation_id"),
        _string(state, "snapshot_id"),
        _string(state, "candidate_id"),
        _string(state, "state"),
        validation or Validation(),
    )


def _failure_outcome(
    operation: str, snapshot: str, candidate: str, code: str
) -> InstallOutcome:
    """Return a bounded non-mutating failure observation for CLI conversion."""
    return InstallOutcome(
        operation, snapshot, candidate, "install_incomplete", Validation(), code
    )


def _manual_or_failure(database: str, operation_id: str, code: str) -> InstallOutcome:
    """Persist manual recovery only when the exact state file remains locatable."""
    try:
        snapshot = load_install_operation(database, operation_id)
        state_path = _state_path(snapshot.snapshot_dir, operation_id)
        state = _read_state(state_path, database, operation_id)
        state["state"] = (
            "manual_recovery_required"
            if code
            in {"manual_recovery_required", "candidate_changed", "snapshot_invalid"}
            else "install_incomplete"
        )
        _write_state(state_path, state)
        set_install_operation_state(database, operation_id, _string(state, "state"))
        return _outcome_from_state(state)
    except (CaptureError, InstallError, TargetError):
        return InstallOutcome(
            operation_id, "unknown", "unknown", "manual_recovery_required"
        )


def _actions(state: dict[str, object]) -> list[dict[str, str | None]]:
    """Return validated intent records with their concrete string value types."""
    actions = state["actions"]
    if not isinstance(actions, list):
        raise InstallError("manual_recovery_required")
    return [cast(dict[str, str | None], item) for item in actions]


def _action_string(action: dict[str, str | None], name: str) -> str:
    """Return one required string member from a validated per-file intent."""
    value = action[name]
    if not isinstance(value, str):
        raise InstallError("manual_recovery_required")
    return value


def _string(state: dict[str, object], name: str) -> str:
    """Return one validated string field from a closed internal state object."""
    value = state[name]
    if not isinstance(value, str):
        raise InstallError("manual_recovery_required")
    return value


def _int(state: dict[str, object], name: str) -> int:
    """Return one validated integer field from a closed internal state object."""
    value = state[name]
    if type(value) is not int:
        raise InstallError("manual_recovery_required")
    return value


def _digest_if_regular(
    path: Path, started: float | None = None, deadline_seconds: int = 1_800
) -> str | None:
    """Return a regular file digest or ``None`` for an absent/unsafe path."""
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    return _digest(path, started, deadline_seconds)


def _digest(
    path: Path, started: float | None = None, deadline_seconds: int = 1_800
) -> str:
    """Hash one regular file with the enclosing operation's finite deadline."""
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(1024 * 1024):
            if started is not None:
                _deadline(started, deadline_seconds)
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Sync one directory after a file replacement or durable state update."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _deadline(started: float, seconds: int) -> None:
    """Raise when an install operation exceeds its required finite deadline."""
    if seconds < 1 or time.monotonic() - started >= seconds:
        raise InstallError("deadline_exceeded")


def _passing_validation() -> Validation:
    """Return the sole validation state that permits reporting installation success."""
    return Validation(Check.PASS, Check.PASS, Check.PASS, Check.PASS, Check.PASS)
