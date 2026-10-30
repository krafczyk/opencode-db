"""Persist bounded, hash-bound preview checkpoints in registered local scratch.

Only completed stages are reusable. SQLite recovery mutates its input, so a
durable ``recovering`` intent deliberately requires a fresh copy after a crash.
Source snapshots and active databases are never modified by this module.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .artifacts import CaptureError, _hash_file, _read_canonical_json, _write_json, new_artifact_id
from .model import AcceptedSnapshot, _valid_preview

STAGES = ("captured", "copied", "recovering", "recovered", "candidate", "integrity", "foreign_keys", "validated", "published")
"""Ordered durable boundaries; recovering is an intent, not reusable input."""

_FILES = {"source.sqlite", "source.sqlite-wal", "candidate.sqlite"}
_KEYS = {
    "schema_version", "snapshot_id", "operation_id", "target_id", "manifest_sha256",
    "stage", "candidate_id", "files", "passive_checkpoint", "truncate_checkpoint", "preview",
}


def _read_checkpoint(path: Path) -> dict[str, Any]:
    """Bound metadata reads before applying the closed checkpoint schema."""
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise CaptureError("preview_checkpoint_invalid")
        value, _ = _read_canonical_json(path)
    except (OSError, CaptureError) as error:
        raise CaptureError("preview_checkpoint_invalid") from error
    _validate(value)
    return value


@dataclass
class PreviewCheckpoint:
    """Hold one validated checkpoint and its exact private metadata path.

    ``path`` is inside marker-validated scratch; ``value`` is a closed mapping
    bound to a revalidated snapshot. Construction performs no I/O. Use
    :func:`open_checkpoint` to read persisted state and methods below to verify
    files or durably advance it. Only tool-generated bounded preview metadata
    is stored, never transcripts or raw SQLite error messages.
    """

    path: Path
    value: dict[str, Any]

    @property
    def stage(self) -> str:
        """Return the last durable stage without accessing the filesystem."""
        return self.value["stage"]

    def reached(self, stage: str) -> bool:
        """Return whether the checkpoint reached known ``stage``; raise on unknown names."""
        return STAGES.index(self.stage) >= STAGES.index(stage)

    def verify(self, started: float, deadline_seconds: int) -> None:
        """Rehash completed files before reuse within the caller's finite budget.

        Returns ``None``; raises :class:`CaptureError` for changed/private-path
        violations or expiry. Reads only fixed scratch filenames. An unfinished
        recovery has no reusable files and must be restaged by cleanup.
        """
        for name, expected in self.value["files"].items():
            path = self.path.parent / name
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
                raise CaptureError("preview_checkpoint_invalid")
            if _hash_file(path, started, deadline_seconds) != (expected["size"], expected["sha256"]):
                raise CaptureError("preview_checkpoint_invalid")

    def save(self, stage: str, **updates: Any) -> None:
        """Atomically persist a completed boundary or recovery intent.

        ``stage`` is a known boundary and ``updates`` supplies closed checkpoint
        fields. Returns ``None``; raises :class:`CaptureError` for invalid state
        and OS errors on write/fsync failure. Memory advances only after the
        private JSON replacement and parent-directory sync succeed.
        """
        value = {**self.value, **updates, "stage": stage}
        _validate(value)
        _write_json(self.path, value)
        self.value = value


def open_checkpoint(snapshot: AcceptedSnapshot, scratch: Path) -> PreviewCheckpoint:
    """Read or initialize a checkpoint inside already-authorized scratch.

    ``snapshot`` must have verified manifests/digests; ``scratch`` must have a
    matching catalog registration and private operation marker. Returns a
    :class:`PreviewCheckpoint`; raises :class:`CaptureError` for malformed,
    future, cross-snapshot or changed state. Initialization writes metadata only.
    """
    path = scratch / ".opencode-db-preview.json"
    identity = {
        "schema_version": 1,
        "snapshot_id": snapshot.snapshot_id,
        "operation_id": snapshot.operation_id,
        "target_id": snapshot.manifest.target_id,
        "manifest_sha256": hashlib.sha256(json.dumps(
            snapshot.manifest.to_dict(), sort_keys=True, ensure_ascii=True, allow_nan=False,
        ).encode("ascii")).hexdigest(),
    }
    if path.exists() or path.is_symlink():
        value = _read_checkpoint(path)
        if any(value[key] != expected for key, expected in identity.items()):
            raise CaptureError("preview_checkpoint_invalid")
    else:
        value = {
            **identity, "stage": "captured", "candidate_id": new_artifact_id("candidate"),
            "files": {}, "passive_checkpoint": None, "truncate_checkpoint": None, "preview": None,
        }
        _write_json(path, value)
    return PreviewCheckpoint(path, value)


def checkpoint_files(paths: list[Path], started: float, deadline_seconds: int) -> dict[str, dict[str, object]]:
    """Hash closed known scratch files for the next durable stage.

    ``paths`` contains tool-selected fixed filenames; deadline parameters bound
    reading. Returns filename-to-size/digest identities. Raises
    :class:`CaptureError` for unknown names or hashing failures. No writes occur.
    """
    result = {}
    for path in paths:
        if path.name not in _FILES:
            raise CaptureError("preview_checkpoint_invalid")
        size, digest = _hash_file(path, started, deadline_seconds)
        result[path.name] = {"size": size, "sha256": digest}
    return result


def checkpoint_stage(scratch: Path, operation_id: str, snapshot_id: str, target_id: str) -> str | None:
    """Read a stage for status without hashing data or opening SQLite.

    Parameters bind an already marker-validated scratch path to its catalog
    IDs. Returns the durable stage, or ``None`` for legacy/missing metadata.
    Raises :class:`CaptureError` for invalid/future state. Reads bounded metadata.
    """
    path = scratch / ".opencode-db-preview.json"
    if not path.exists() and not path.is_symlink():
        return None
    value = _read_checkpoint(path)
    if (value["operation_id"], value["snapshot_id"], value["target_id"]) != (operation_id, snapshot_id, target_id):
        raise CaptureError("preview_checkpoint_invalid")
    return value["stage"]


def checkpoint_candidate(scratch: Path, operation_id: str, snapshot_id: str, target_id: str) -> str | None:
    """Return the checkpoint-bound publication ID for explicit abort cleanup.

    Parameters bind marker-authorized scratch to its exact catalog IDs. Returns
    ``None`` for legacy/missing metadata or the validated opaque candidate ID.
    Raises :class:`CaptureError` for malformed/cross-target state. Reads only
    bounded private metadata and grants no authority over registered candidates.
    """
    if checkpoint_stage(scratch, operation_id, snapshot_id, target_id) is None:
        return None
    value = _read_checkpoint(scratch / ".opencode-db-preview.json")
    return value["candidate_id"]


def _validate(value: object) -> None:
    """Reject future, malformed or internally contradictory checkpoint metadata."""
    if not isinstance(value, dict) or set(value) != _KEYS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CaptureError("preview_checkpoint_invalid")
    patterns = {
        "snapshot_id": r"snapshot-\d{8}T\d{6}Z-[0-9a-f]{24}",
        "operation_id": r"operation-\d{8}T\d{6}Z-[0-9a-f]{24}",
        "candidate_id": r"candidate-\d{8}T\d{6}Z-[0-9a-f]{24}",
        "target_id": r"[0-9a-f]{32}", "manifest_sha256": r"[0-9a-f]{64}",
    }
    if any(not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None for key, pattern in patterns.items()):
        raise CaptureError("preview_checkpoint_invalid")
    stage = value["stage"]
    if stage not in STAGES or not isinstance(value["files"], dict):
        raise CaptureError("preview_checkpoint_invalid")
    names = set(value["files"])
    expected = (
        set() if stage in {"captured", "recovering"}
        else {"source.sqlite"} if stage == "recovered"
        else {"candidate.sqlite"} if STAGES.index(stage) >= STAGES.index("candidate")
        else names
    )
    if names != expected or not names <= _FILES or stage == "copied" and not {"source.sqlite"} <= names <= {"source.sqlite", "source.sqlite-wal"}:
        raise CaptureError("preview_checkpoint_invalid")
    for identity in value["files"].values():
        if not isinstance(identity, dict) or set(identity) != {"size", "sha256"} or type(identity["size"]) is not int or identity["size"] < 0 or not isinstance(identity["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None:
            raise CaptureError("preview_checkpoint_invalid")
    for name in ("passive_checkpoint", "truncate_checkpoint"):
        row = value[name]
        if STAGES.index(stage) >= STAGES.index("recovered"):
            if not isinstance(row, list) or len(row) != 3 or any(type(item) is not int for item in row) or row[0] < 0 or row[1] < -1 or row[2] < -1:
                raise CaptureError("preview_checkpoint_invalid")
            if (row[1] == -1) != (row[2] == -1):
                raise CaptureError("preview_checkpoint_invalid")
            if name == "truncate_checkpoint" and row not in ([0, 0, 0], [0, -1, -1]):
                raise CaptureError("preview_checkpoint_invalid")
        elif row is not None:
            raise CaptureError("preview_checkpoint_invalid")
    if STAGES.index(stage) >= STAGES.index("validated"):
        if not _valid_preview(value["preview"]):
            raise CaptureError("preview_checkpoint_invalid")
    elif value["preview"] is not None:
        raise CaptureError("preview_checkpoint_invalid")
