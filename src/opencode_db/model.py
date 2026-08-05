"""Closed version-1 types for cleanup command requests and machine results.

This module owns the public wire shape shared by human and JSON renderers. It
contains no filesystem or SQLite operations, so parsing persisted data cannot
mutate an operator database or artifact store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Any, ClassVar

from . import RESULT_SCHEMA_VERSION, __version__

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_DECISION_REQUIRED = 3
EXIT_PRECONDITION_REFUSED = 4
EXIT_OPERATIONAL_FAILURE = 5
EXIT_MANUAL_RECOVERY_REQUIRED = 6

MAX_DIAGNOSTICS = 8
"""Maximum number of safe diagnostic summaries in one result."""

MAX_PREVIEW_PROJECTS = 128
"""Maximum recognizable project records in a version-1 preview."""

MAX_PREVIEW_SESSIONS = 4
"""Maximum recent session records in a version-1 preview."""

MAX_PREVIEW_TEXT = 512
"""Maximum character length for one preview identity field or table name."""


class Status(StrEnum):
    """Closed status values emitted by schema-version-1 results.

    Values distinguish terminal success, uncertain decisions, safety refusals,
    operational failures, and recovery states. Future producers must use a new
    schema version before adding a value.
    """

    COMPLETE = "complete"
    UNCERTAIN = "uncertain"
    SOURCE_CHANGED = "source_changed"
    INVALID = "invalid"
    INSTALLED = "installed"
    INSTALL_INCOMPLETE = "install_incomplete"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
    ROLLED_BACK = "rolled_back"
    BACKUP_PRUNED = "backup_pruned"
    STATUS_OK = "status_ok"
    ABORTED = "aborted"
    SCRATCH_CLEANUP_REQUIRED = "scratch_cleanup_required"
    TARGET_INVALID = "target_invalid"
    PRECONDITION_REFUSED = "precondition_refused"
    SYNTAX_ERROR = "syntax_error"
    OPERATIONAL_FAILURE = "operational_failure"


class Check(StrEnum):
    """Closed validation outcome for one SQLite validation gate."""

    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class Validation:
    """Describe the five mandatory validation gates in a result.

    Each parameter is a :class:`Check` outcome. The type returns a JSON-ready
    closed mapping through :meth:`to_dict` and has no side effects.
    """

    integrity: Check = Check.NOT_RUN
    foreign_keys: Check = Check.NOT_RUN
    checkpoint: Check = Check.NOT_RUN
    clean_reopen: Check = Check.NOT_RUN
    sidecars_absent: Check = Check.NOT_RUN

    def to_dict(self) -> dict[str, str]:
        """Return the closed JSON representation without performing validation."""
        return {
            "integrity": self.integrity.value,
            "foreign_keys": self.foreign_keys.value,
            "checkpoint": self.checkpoint.value,
            "clean_reopen": self.clean_reopen.value,
            "sidecars_absent": self.sidecars_absent.value,
        }


@dataclass(frozen=True)
class CommandRequest:
    """Represent one syntactically valid, explicit cleanup command.

    Parameters record only parsed operator input. The request does not resolve
    paths, access stdin, open SQLite, or mutate artifacts; later units supply
    that execution behavior.
    """

    command: str
    database: str
    json: bool = False
    scratch_dir: str | None = None
    deadline_seconds: int | None = None
    candidate_id: str | None = None
    operation_id: str | None = None
    snapshot_id: str | None = None
    approve_uncertain_report: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    """Provide one bounded, credential-free diagnostic summary.

    Parameters are a stable ``code`` and its trusted ``message``. Callers use
    :meth:`to_dict` for JSON output; construction has no external side effects.
    """

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """Return the closed JSON representation of this diagnostic."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class SourceFile:
    """Record the observed content identity of one supported source file.

    Parameters identify the fixed ``name`` (``main``, ``wal``, ``shm``, or
    ``journal``), whether it was ``present``, and, when present, its exact
    ``size`` and SHA-256 ``sha256`` digest. :meth:`to_dict` returns a canonical
    JSON-ready representation. Construction and serialization have no
    filesystem side effects.
    """

    name: str
    present: bool
    size: int | None
    sha256: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the closed JSON representation without reading the source."""
        return {
            "name": self.name,
            "present": self.present,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SourceManifest:
    """Bind an exact captured SQLite source set to one canonical target.

    Parameters record the schema and tool versions, canonical ``target``,
    opaque target-scoped ``target_id``, and all four standard ``files``. The
    manifest is persisted by the artifact layer and can be read with
    :meth:`from_dict`, which rejects unknown future shapes. It does not open
    SQLite or modify source files.
    """

    target: str
    target_id: str
    files: tuple[SourceFile, ...]
    schema_version: int = 1
    tool_version: str = __version__

    def to_dict(self) -> dict[str, object]:
        """Return the versioned canonical JSON-ready manifest mapping."""
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "target": self.target,
            "target_id": self.target_id,
            "files": [source_file.to_dict() for source_file in self.files],
        }

    @classmethod
    def from_dict(cls, value: object) -> "SourceManifest":
        """Parse one version-1 manifest without accepting future extensions.

        Parameters: ``value`` is a JSON-decoded manifest object. Returns the
        immutable :class:`SourceManifest`. Raises :class:`ValueError` when the
        version, fields, file set, or file identities are invalid. Parsing has
        no filesystem or SQLite side effects.
        """
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "tool_version",
            "target",
            "target_id",
            "files",
        }:
            raise ValueError("manifest does not match the version-1 closed schema")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not all(
                isinstance(value[name], str)
                for name in ("tool_version", "target", "target_id")
            )
        ):
            raise ValueError("unsupported or invalid manifest metadata")
        files = value["files"]
        if not isinstance(files, list) or len(files) != 4:
            raise ValueError("manifest has an invalid source-file set")
        parsed: list[SourceFile] = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "present",
                "size",
                "sha256",
            }:
                raise ValueError("manifest has an invalid source-file record")
            if (
                not isinstance(item["name"], str)
                or not isinstance(item["present"], bool)
                or item["size"] is not None
                and (type(item["size"]) is not int or item["size"] < 0)
                or item["sha256"] is not None
                and (
                    not isinstance(item["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                )
            ):
                raise ValueError("manifest has an invalid source-file identity")
            if item["present"] != (
                item["size"] is not None and item["sha256"] is not None
            ):
                raise ValueError("manifest has inconsistent source-file presence")
            parsed.append(SourceFile(**item))
        if tuple(item.name for item in parsed) != ("main", "wal", "shm", "journal"):
            raise ValueError("manifest has an unsupported source-file set")
        return cls(
            target=value["target"],
            target_id=value["target_id"],
            files=tuple(parsed),
            schema_version=value["schema_version"],
            tool_version=value["tool_version"],
        )


@dataclass(frozen=True)
class CaptureOutcome:
    """Describe one copy-only source capture attempt and its retained evidence.

    Parameters identify the canonical ``target`` when it resolved and opaque
    target-scoped IDs, state whether the snapshot was ``accepted``, classify the
    bounded ``status``, and name the private ``snapshot_dir`` when capture
    reached artifact creation.
    The artifact layer returns this type without opening SQLite or producing a
    candidate; callers must not treat an unaccepted outcome as installable.
    """

    target: str | None
    target_id: str | None
    snapshot_id: str | None
    operation_id: str | None
    snapshot_dir: Path | None
    accepted: bool
    status: str


@dataclass(frozen=True)
class AcceptedSnapshot:
    """Identify one revalidated accepted retained source snapshot.

    Parameters bind a private ``snapshot_dir`` to its captured manifest and
    target-scoped identifiers. Artifact readers return this type only after
    checking every manifest and retained source digest. Constructing it has no
    filesystem effects; consuming SQLite must still occur only on a scratch
    copy.
    """

    snapshot_dir: Path
    snapshot_id: str
    operation_id: str
    manifest: SourceManifest


@dataclass(frozen=True)
class CleanupOutcome:
    """Describe the result of creating and validating one SQLite candidate.

    Parameters identify the source snapshot, resulting ``completeness`` class,
    required validation gates, an optional bounded ``preview``, and immutable
    retained candidate/report paths when the candidate is installable.
    ``candidate_path`` and ``report_path`` remain ``None`` for invalid or
    operational outcomes, while a readable invalid candidate can still retain
    its safe preview. This value does not itself write files or authorize
    installation.
    """

    snapshot_id: str
    target: str
    status: str
    completeness: str
    validation: Validation
    installable: bool
    candidate_id: str | None = None
    candidate_path: Path | None = None
    report_path: Path | None = None
    report_sha256: str | None = None
    preview: dict[str, Any] | None = None
    diagnostic_code: str | None = None


@dataclass(frozen=True)
class CandidateEvidence:
    """Bind one selected retained candidate to immutable target-scoped evidence.

    Parameters identify the retained candidate and report paths, their verified
    digests, the bound snapshot, and the candidate completeness class. Artifact
    selection returns this type only after rehashing both files and validating
    their closed report references. Constructing it performs no I/O and does not
    authorize active-database mutation.
    """

    snapshot_id: str
    candidate_id: str
    candidate_path: Path
    report_path: Path
    candidate_sha256: str
    report_sha256: str
    completeness: str


@dataclass(frozen=True)
class InstallOutcome:
    """Describe a durable installation or recovery operation.

    ``operation_id`` identifies the persisted intent log, ``state`` is the
    recorded installation phase, and ``validation`` contains only non-migrating
    SQLite checks performed at the active target.  Construction is read-only;
    :mod:`opencode_db.install` performs the associated filesystem work.
    """

    operation_id: str
    snapshot_id: str
    candidate_id: str
    state: str
    validation: Validation = field(default_factory=Validation)
    code: str | None = None


@dataclass(frozen=True)
class OperationEvidence:
    """Describe one target-scoped capture or preview operation for recovery.

    Parameters retain the exact operation/snapshot IDs, state, creating host,
    and registered scratch path. Status and abort use this type without process
    inspection. It has no side effects and callers must verify target scope
    before acting on the recorded scratch path.
    """

    operation_id: str
    snapshot_id: str
    state: str
    host: str | None
    scratch_path: str | None


@dataclass(frozen=True)
class Result:
    """Represent one complete closed schema-version-1 command result.

    Parameters correspond exactly to the public machine-result object. Values
    are serializable through :meth:`to_dict`; :meth:`from_json` rejects unknown
    schemas, fields, and enum values without reading or changing external
    state. Preview contents, when present, use the approved bounded project,
    session, and SQLite issue-count object.
    """

    schema_version: int = RESULT_SCHEMA_VERSION
    command: str = "unknown"
    ok: bool = False
    status: Status = Status.OPERATIONAL_FAILURE
    exit_code: int = EXIT_OPERATIONAL_FAILURE
    target: str | None = None
    snapshot_id: str | None = None
    candidate_id: str | None = None
    operation_id: str | None = None
    report_sha256: str | None = None
    completeness: str | None = None
    validation: Validation = field(default_factory=Validation)
    preview: dict[str, Any] | None = None
    next_actions: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    _JSON_FIELDS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "command",
        "ok",
        "status",
        "exit_code",
        "target",
        "snapshot_id",
        "candidate_id",
        "operation_id",
        "report_sha256",
        "completeness",
        "validation",
        "preview",
        "next_actions",
        "diagnostics",
    )

    @classmethod
    def json_fields(cls) -> tuple[str, ...]:
        """Return result keys in the schema's stable closed-field order."""
        return cls._JSON_FIELDS

    @classmethod
    def failure(
        cls,
        *,
        command: str,
        status: Status,
        exit_code: int,
        diagnostic_code: str,
        diagnostic_message: str,
        target: str | None = None,
        snapshot_id: str | None = None,
        candidate_id: str | None = None,
        operation_id: str | None = None,
    ) -> "Result":
        """Build a bounded safe failure result from an internal error context.

        ``diagnostic_message`` is intentionally not emitted: it can contain
        secrets or arbitrary exception data. The returned result instead uses a
        fixed public message selected by ``diagnostic_code`` and has no side
        effects. Optional target-scoped IDs retain existing diagnostic evidence
        without authorizing a candidate.
        """
        safe_messages = {
            "bootstrap_unavailable": "Cleanup execution is not available in this bootstrap release.",
            "invalid_arguments": "Command arguments do not match the supported grammar.",
            "target_invalid": "The database target must be one existing absolute regular file.",
            "source_changed": "The source database set changed during capture.",
            "unsupported_journal": "Rollback-journal evidence was preserved but is unsupported.",
            "storage_capacity": "Required storage capacity or inodes are unavailable.",
            "scratch_not_local": "Scratch storage is not on an admitted local filesystem.",
            "scratch_not_private": "Scratch storage is not private.",
            "scratch_capacity": "Scratch storage capacity or inodes are unavailable.",
            "artifact_not_private": "Private artifact storage cannot be established.",
            "artifact_schema_unsupported": "Retained artifact state is not a supported schema.",
            "scratch_invalid": "Scratch storage must be an absolute filesystem path.",
            "deadline_exceeded": "The operation deadline expired before capture completed.",
            "copy_failed": "Source capture could not complete safely.",
            "snapshot_invalid": "The retained source snapshot is not accepted or has changed.",
            "cleanup_invalid": "Normal SQLite recovery could not produce a valid candidate.",
            "checkpoint_incomplete": "SQLite could not complete the required checkpoint.",
            "backup_failed": "SQLite backup could not complete safely.",
            "candidate_persist_failed": "The validated candidate could not be retained safely.",
            "candidate_changed": "The selected candidate is unavailable or has changed.",
            "report_changed": "The selected immutable report is unavailable or has changed.",
            "approval_required": "The uncertain candidate requires its exact report digest approval.",
            "snapshot_required": "The selected backup is required by a nonterminal recovery operation.",
            "unexpected_error": "An unexpected internal error occurred.",
        }
        return cls(
            command=command,
            status=status,
            exit_code=exit_code,
            target=target,
            snapshot_id=snapshot_id,
            candidate_id=candidate_id,
            operation_id=operation_id,
            diagnostics=(
                Diagnostic(
                    code=diagnostic_code,
                    message=safe_messages.get(
                        diagnostic_code, "The operation could not be completed."
                    ),
                ),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete JSON-ready result object without external effects."""
        return {
            "schema_version": self.schema_version,
            "command": self.command,
            "ok": self.ok,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "target": self.target,
            "snapshot_id": self.snapshot_id,
            "candidate_id": self.candidate_id,
            "operation_id": self.operation_id,
            "report_sha256": self.report_sha256,
            "completeness": self.completeness,
            "validation": self.validation.to_dict(),
            "preview": self.preview,
            "next_actions": list(self.next_actions),
            "diagnostics": [
                diagnostic.to_dict()
                for diagnostic in self.diagnostics[:MAX_DIAGNOSTICS]
            ],
        }

    @classmethod
    def from_json(cls, value: object) -> "Result":
        """Parse one persisted version-1 result without accepting extensions.

        Parameters: ``value`` must be a JSON-decoded object containing exactly
        the schema fields. Returns a :class:`Result`. Raises :class:`ValueError`
        for unknown schemas, fields, or invalid closed enum values. It has no
        filesystem, database, or artifact-store side effects.
        """
        if not isinstance(value, dict) or set(value) != set(cls._JSON_FIELDS):
            raise ValueError("result does not match the version-1 closed schema")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != RESULT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported result schema version")
        try:
            status = Status(value["status"])
            validation_value = value["validation"]
            if not isinstance(validation_value, dict) or set(validation_value) != {
                "integrity",
                "foreign_keys",
                "checkpoint",
                "clean_reopen",
                "sidecars_absent",
            }:
                raise ValueError("invalid validation object")
            validation = Validation(
                **{key: Check(item) for key, item in validation_value.items()}
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("result contains an unknown closed value") from error
        if not isinstance(value["command"], str) or not isinstance(value["ok"], bool):
            raise ValueError("result has invalid command or success fields")
        if type(value["exit_code"]) is not int:
            raise ValueError("result has an invalid exit code")
        nullable_strings = (
            "target",
            "snapshot_id",
            "candidate_id",
            "operation_id",
            "report_sha256",
        )
        if any(
            value[name] is not None and not isinstance(value[name], str)
            for name in nullable_strings
        ):
            raise ValueError("result has an invalid nullable string field")
        if value["completeness"] not in (None, "complete", "uncertain", "invalid"):
            raise ValueError("result has an invalid completeness value")
        if value["preview"] is not None and not _valid_preview(value["preview"]):
            raise ValueError("result has an invalid preview object")
        if not isinstance(value["next_actions"], list) or not all(
            isinstance(action, str) for action in value["next_actions"]
        ):
            raise ValueError("result has invalid next actions")
        diagnostics_value = value["diagnostics"]
        if (
            not isinstance(diagnostics_value, list)
            or len(diagnostics_value) > MAX_DIAGNOSTICS
            or any(
                not isinstance(item, dict)
                or set(item) != {"code", "message"}
                or not all(isinstance(part, str) for part in item.values())
                for item in diagnostics_value
            )
        ):
            raise ValueError("result has invalid diagnostics")
        return cls(
            command=value["command"],
            ok=value["ok"],
            status=status,
            exit_code=value["exit_code"],
            target=value["target"],
            snapshot_id=value["snapshot_id"],
            candidate_id=value["candidate_id"],
            operation_id=value["operation_id"],
            report_sha256=value["report_sha256"],
            completeness=value["completeness"],
            validation=validation,
            preview=value["preview"],
            next_actions=tuple(value["next_actions"]),
            diagnostics=tuple(Diagnostic(**item) for item in diagnostics_value),
        )


def _valid_preview(value: object) -> bool:
    """Return whether one result preview has the closed bounded version-1 shape."""
    if not isinstance(value, dict) or set(value) != {
        "projects",
        "recent_sessions",
        "table_issue_counts",
        "database_issue_count",
    }:
        return False
    projects = value["projects"]
    if projects != "unavailable":
        if not isinstance(projects, list) or len(projects) > MAX_PREVIEW_PROJECTS:
            return False
        for project in projects:
            if not isinstance(project, dict) or set(project) not in (
                {"id", "name", "worktree"},
                {"id", "name", "worktree", "origin"},
            ):
                return False
            if (
                not isinstance(project["id"], str)
                or not isinstance(project["worktree"], str)
                or project["name"] is not None
                and not isinstance(project["name"], str)
                or "origin" in project
                and not isinstance(project["origin"], str)
                or any(
                    isinstance(item, str) and len(item) > MAX_PREVIEW_TEXT
                    for item in project.values()
                )
            ):
                return False
    sessions = value["recent_sessions"]
    if sessions != "unavailable":
        if not isinstance(sessions, list) or len(sessions) > MAX_PREVIEW_SESSIONS:
            return False
        for session in sessions:
            if not isinstance(session, dict) or set(session) != {
                "id",
                "title",
                "project_id",
                "time_updated",
            }:
                return False
            if (
                not all(
                    isinstance(session[name], str)
                    for name in ("id", "title", "project_id")
                )
                or type(session["time_updated"]) is not int
                or any(
                    len(session[name]) > MAX_PREVIEW_TEXT
                    for name in ("id", "title", "project_id")
                )
            ):
                return False
    table_counts = value["table_issue_counts"]
    return (
        isinstance(table_counts, dict)
        and len(table_counts) <= MAX_PREVIEW_PROJECTS
        and all(
            isinstance(name, str)
            and len(name) <= MAX_PREVIEW_TEXT
            and type(count) is int
            and count >= 0
            for name, count in table_counts.items()
        )
        and type(value["database_issue_count"]) is int
        and value["database_issue_count"] >= 0
    )
