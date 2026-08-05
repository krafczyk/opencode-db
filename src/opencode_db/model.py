"""Closed version-1 types for cleanup command requests and machine results.

This module owns the public wire shape shared by human and JSON renderers. It
contains no filesystem or SQLite operations, so parsing persisted data cannot
mutate an operator database or artifact store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from . import RESULT_SCHEMA_VERSION

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_DECISION_REQUIRED = 3
EXIT_PRECONDITION_REFUSED = 4
EXIT_OPERATIONAL_FAILURE = 5
EXIT_MANUAL_RECOVERY_REQUIRED = 6

MAX_DIAGNOSTICS = 8
"""Maximum number of safe diagnostic summaries in one result."""


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
class Result:
    """Represent one complete closed schema-version-1 command result.

    Parameters correspond exactly to the public machine-result object. Values
    are serializable through :meth:`to_dict`; :meth:`from_json` rejects unknown
    schemas, fields, and enum values without reading or changing external
    state. Preview contents remain ``None`` until a later unit implements the
    approved lightweight preview object.
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
    ) -> "Result":
        """Build a bounded safe failure result from an internal error context.

        ``diagnostic_message`` is intentionally not emitted: it can contain
        secrets or arbitrary exception data. The returned result instead uses a
        fixed public message selected by ``diagnostic_code`` and has no side
        effects.
        """
        safe_messages = {
            "bootstrap_unavailable": "Cleanup execution is not available in this bootstrap release.",
            "invalid_arguments": "Command arguments do not match the supported grammar.",
            "unexpected_error": "An unexpected internal error occurred.",
        }
        return cls(
            command=command,
            status=status,
            exit_code=exit_code,
            target=target,
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
        if value["preview"] is not None and not isinstance(value["preview"], dict):
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
