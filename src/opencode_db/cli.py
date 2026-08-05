"""Parse and render the closed OpenCode database cleanup command contract.

The module freezes the version-1 grammar and result encoding. Preview composes
immutable capture with normal SQLite cleanup; it never inspects processes,
reads stdin, or opens an active or retained database through SQLite.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sys
import time
from collections.abc import Sequence

from .artifacts import (
    CaptureError,
    abort_preview_operation,
    capture_source_set,
    operation_status,
    prune_backup,
)
from .cleanup import clean_snapshot
from .install import (
    install_from_selection,
    install_status,
    resume_install,
    rollback_install,
)
from .model import (
    EXIT_DECISION_REQUIRED,
    EXIT_MANUAL_RECOVERY_REQUIRED,
    EXIT_OPERATIONAL_FAILURE,
    EXIT_PRECONDITION_REFUSED,
    EXIT_USAGE,
    CommandRequest,
    Diagnostic,
    Result,
    Status,
)

MAX_JSON_BYTES = 64 * 1024
"""Maximum UTF-8 byte length for a machine-mode result, including its newline."""

MAX_ID_LENGTH = 256
"""Maximum length accepted for an exact target-scoped identifier."""

MAX_DEADLINE_SECONDS = 86_400
"""Largest supported finite whole-second deadline."""

DEFAULT_DEADLINE_SECONDS = 1_800
"""Default overall operation deadline in seconds for later execution units."""

_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_COMMAND_OPTIONS = {
    "preview": {"database", "scratch-dir", "deadline-seconds", "json"},
    "install": {
        "database",
        "candidate",
        "approve-uncertain-report",
        "scratch-dir",
        "deadline-seconds",
        "json",
    },
    "status": {"database", "operation", "json"},
    "abort": {"database", "operation", "json"},
    "resume": {"database", "operation", "deadline-seconds", "json"},
    "rollback": {"database", "operation", "deadline-seconds", "json"},
    "prune-backup": {"database", "snapshot", "json"},
}
_REQUIRED_OPTIONS = {
    "preview": {"database"},
    "install": {"database", "candidate"},
    "status": {"database"},
    "abort": {"database", "operation"},
    "resume": {"database", "operation"},
    "rollback": {"database", "operation"},
    "prune-backup": {"database", "snapshot"},
}


class CliUsageError(ValueError):
    """Signal one bounded command-grammar error before execution.

    ``command`` identifies the most specific command parsed so far. The error
    is rendered as a schema result in machine mode and does not write streams,
    access the database, or mutate artifacts by itself.
    """

    def __init__(self, command: str, message: str) -> None:
        """Store a safe command identifier and a bounded public error message."""
        super().__init__(message[:256])
        self.command = command


def parse_command(arguments: Sequence[str]) -> CommandRequest:
    """Parse a normative cleanup command without accessing external state.

    Parameters: ``arguments`` is an argv sequence excluding the program name.
    Returns a :class:`CommandRequest` with only validated, explicit fields.
    Raises :class:`CliUsageError` for unknown commands, missing options, invalid
    absolute paths, ambiguous IDs, duplicate options, and non-finite deadlines.
    The function never reads stdin, creates a database, or mutates a file.
    """
    values = list(arguments)
    if len(values) < 2 or values[0] != "cleanup":
        raise CliUsageError("unknown", "Expected a cleanup command.")
    action = values[1]
    command = f"cleanup {action}"
    if action not in _COMMAND_OPTIONS:
        raise CliUsageError(command, "Unknown cleanup command.")

    options: dict[str, str | bool] = {}
    position = 2
    while position < len(values):
        token = values[position]
        if not token.startswith("--"):
            raise CliUsageError(command, "Unexpected positional argument.")
        option = token[2:]
        if option not in _COMMAND_OPTIONS[action]:
            raise CliUsageError(command, "Unsupported command option.")
        if option in options:
            raise CliUsageError(command, "Command options may not be repeated.")
        if option == "json":
            options[option] = True
            position += 1
            continue
        if position + 1 >= len(values) or values[position + 1].startswith("--"):
            raise CliUsageError(command, "A command option is missing its value.")
        options[option] = values[position + 1]
        position += 2

    if missing := _REQUIRED_OPTIONS[action] - set(options):
        raise CliUsageError(command, f"Missing required option --{sorted(missing)[0]}.")
    database = _absolute_path(_option(options, "database"), command, "database")
    scratch_dir = _optional_path(options, "scratch-dir", command)
    deadline = _optional_deadline(options, command)
    candidate_id = _optional_id(options, "candidate", command)
    operation_id = _optional_id(options, "operation", command)
    snapshot_id = _optional_snapshot_id(options, command)
    report = _optional_digest(options, "approve-uncertain-report", command)
    return CommandRequest(
        command=command,
        database=database,
        json=bool(options.get("json", False)),
        scratch_dir=scratch_dir,
        deadline_seconds=deadline,
        candidate_id=candidate_id,
        operation_id=operation_id,
        snapshot_id=snapshot_id,
        approve_uncertain_report=report,
    )


def render_json(result: Result) -> str:
    """Serialize one result as deterministic, ASCII-safe, bounded JSON.

    Parameters: ``result`` is a closed schema result. Returns exactly one
    newline-terminated document with sorted keys and no NaN values. If a future
    result exceeds the machine output cap, returns a fixed operational-failure
    result instead. Serialization has no filesystem or database side effects.
    """
    rendered = (
        json.dumps(result.to_dict(), sort_keys=True, ensure_ascii=True, allow_nan=False)
        + "\n"
    )
    if len(rendered.encode("ascii")) <= MAX_JSON_BYTES:
        return rendered
    fallback = Result.failure(
        command=result.command,
        status=Status.OPERATIONAL_FAILURE,
        exit_code=5,
        diagnostic_code="unexpected_error",
        diagnostic_message="result exceeded output bound",
        target=result.target,
    )
    return (
        json.dumps(
            fallback.to_dict(), sort_keys=True, ensure_ascii=True, allow_nan=False
        )
        + "\n"
    )


def render_human(result: Result) -> str:
    """Render a bounded human diagnostic from the same closed result object.

    Parameters: ``result`` is a closed schema result. Returns one
    newline-terminated public diagnostic without exposing arbitrary exception
    text. Rendering has no filesystem, database, or process side effects.
    """
    if result.preview is not None:
        lines = [
            f"opencode-db: {result.status.value}",
            f"target: {result.target}",
            f"snapshot: {result.snapshot_id}",
            f"candidate: {result.candidate_id}",
            f"report: {result.report_sha256}",
        ]
        projects = result.preview.get("projects")
        if isinstance(projects, list):
            lines.extend(
                "project: "
                + " | ".join(
                    str(project.get(name, ""))
                    for name in ("id", "name", "worktree", "origin")
                )
                for project in projects
                if isinstance(project, dict)
            )
        sessions = result.preview.get("recent_sessions")
        if isinstance(sessions, list):
            lines.extend(
                "session: "
                + " | ".join(
                    str(session.get(name, ""))
                    for name in ("id", "title", "project_id", "time_updated")
                )
                for session in sessions
                if isinstance(session, dict)
            )
        lines.extend(f"next: {action}" for action in result.next_actions)
        return "\n".join(lines) + "\n"
    if result.diagnostics:
        return f"opencode-db: {result.diagnostics[0].message}\n"
    return f"opencode-db: {result.status.value}\n"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the parser and renderer for console-script or module invocation.

    Parameters: ``arguments`` optionally replaces ``sys.argv[1:]``. Returns the
    documented exit class. Machine-mode parse errors emit one JSON result on
    stdout and no diagnostics there. ``cleanup preview`` captures an explicit
    target and cleans only a fresh scratch copy; other commands safely refuse
    execution until later units implement their artifact operations. The
    function does not prompt or read stdin.
    """
    values = list(sys.argv[1:] if arguments is None else arguments)
    if _wants_help(values):
        sys.stdout.write(_help_text(values))
        return 0
    json_mode = "--json" in values
    try:
        request = parse_command(values)
    except CliUsageError as error:
        result = Result.failure(
            command=error.command,
            status=Status.SYNTAX_ERROR,
            exit_code=EXIT_USAGE,
            diagnostic_code="invalid_arguments",
            diagnostic_message=str(error),
        )
        if json_mode:
            sys.stdout.write(render_json(result))
        else:
            sys.stderr.write(render_human(result))
        return EXIT_USAGE

    result = _execute(request)
    if request.json:
        sys.stdout.write(render_json(result))
    else:
        sys.stderr.write(render_human(result))
    return result.exit_code


def _execute(request: CommandRequest) -> Result:
    """Execute preview evidence operations or refuse deferred installation.

    Parameters: ``request`` is grammar-validated operator input. Returns one
    closed :class:`Result`, including actual cleanup classification for preview.
    Capture, cleanup, installation recovery, and exact backup pruning use only
    their corresponding private artifact protocols. The function never invokes
    OpenCode or performs active-database retention.
    """
    if request.command == "cleanup status":
        return _status_result(request)
    if request.command == "cleanup abort":
        return _abort_result(request)
    if request.command == "cleanup install":
        return _install_result(request)
    if request.command == "cleanup resume":
        return _resume_result(request)
    if request.command == "cleanup rollback":
        return _rollback_result(request)
    if request.command == "cleanup prune-backup":
        return _prune_backup_result(request)
    if request.command != "cleanup preview":
        return Result.failure(
            command=request.command,
            status=Status.PRECONDITION_REFUSED,
            exit_code=EXIT_PRECONDITION_REFUSED,
            diagnostic_code="bootstrap_unavailable",
            diagnostic_message="execution is deferred",
            target=request.database,
        )
    deadline = request.deadline_seconds or DEFAULT_DEADLINE_SECONDS
    started = time.monotonic()
    capture = capture_source_set(
        request.database,
        scratch_dir=request.scratch_dir,
        deadline_seconds=deadline,
    )
    if not capture.accepted or capture.snapshot_dir is None:
        if capture.status == "source_changed":
            status = Status.SOURCE_CHANGED
            exit_code = EXIT_PRECONDITION_REFUSED
        elif capture.status == "target_invalid":
            status = Status.TARGET_INVALID
            exit_code = EXIT_PRECONDITION_REFUSED
        elif capture.status in {
            "unsupported_journal",
            "storage_capacity",
            "scratch_not_local",
            "scratch_not_private",
            "scratch_capacity",
            "artifact_not_private",
            "artifact_schema_unsupported",
            "scratch_invalid",
        }:
            status = Status.PRECONDITION_REFUSED
            exit_code = EXIT_PRECONDITION_REFUSED
        else:
            status = Status.OPERATIONAL_FAILURE
            exit_code = EXIT_OPERATIONAL_FAILURE
        return Result.failure(
            command=request.command,
            status=status,
            exit_code=exit_code,
            diagnostic_code=capture.status,
            diagnostic_message=capture.status,
            target=capture.target or request.database,
            snapshot_id=capture.snapshot_id,
            operation_id=capture.operation_id,
        )
    scratch = request.scratch_dir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "opencode-db"
    )
    remaining = max(0, deadline - math.ceil(time.monotonic() - started))
    outcome = clean_snapshot(
        capture.snapshot_dir,
        scratch_dir=scratch,
        deadline_seconds=remaining,
    )
    status = Status(outcome.status)
    exit_code = (
        0
        if outcome.completeness == "complete"
        else EXIT_DECISION_REQUIRED
        if outcome.completeness == "uncertain"
        else EXIT_OPERATIONAL_FAILURE
    )
    diagnostics: tuple[Diagnostic, ...] = ()
    if outcome.diagnostic_code is not None:
        diagnostics = Result.failure(
            command=request.command,
            status=Status.INVALID,
            exit_code=EXIT_OPERATIONAL_FAILURE,
            diagnostic_code=outcome.diagnostic_code,
            diagnostic_message=outcome.diagnostic_code,
        ).diagnostics
    return Result(
        command=request.command,
        ok=outcome.completeness == "complete",
        status=status,
        exit_code=exit_code,
        target=outcome.target,
        snapshot_id=outcome.snapshot_id,
        candidate_id=outcome.candidate_id,
        report_sha256=outcome.report_sha256,
        completeness=outcome.completeness,
        validation=outcome.validation,
        preview=outcome.preview,
        next_actions=_preview_actions(request, outcome),
        diagnostics=diagnostics,
    )


def _status_result(request: CommandRequest) -> Result:
    """Render one exact operation's host-local scratch recovery information.

    Parameters: ``request`` contains an explicit target and optional operation
    ID. Returns a read-only status result; no processes, SQLite connections, or
    source files are inspected. Unknown or cross-target operation IDs are
    precondition refusals.
    """
    if request.operation_id is None:
        return Result(
            command=request.command,
            ok=True,
            status=Status.STATUS_OK,
            exit_code=0,
            target=request.database,
            diagnostics=(Diagnostic("operation_status", "No operation was selected."),),
        )
    if request.operation_id.startswith("install-"):
        try:
            outcome = install_status(request.database, request.operation_id)
        except Exception:
            return _artifact_failure(request, "candidate_changed")
        return _install_outcome_result(request, outcome)
    try:
        evidence = operation_status(request.database, request.operation_id)
    except CaptureError as error:
        return _artifact_failure(request, error.code)
    location = evidence.scratch_path or "none"
    return Result(
        command=request.command,
        ok=True,
        status=Status.STATUS_OK,
        exit_code=0,
        target=request.database,
        snapshot_id=evidence.snapshot_id,
        operation_id=evidence.operation_id,
        diagnostics=(
            Diagnostic(
                "operation_status",
                f"operation {evidence.operation_id} is {evidence.state} on host "
                f"{evidence.host or 'unknown'}; scratch {location}"[:1024],
            ),
        ),
        next_actions=_status_actions(
            request.database, evidence.operation_id, evidence.state
        ),
    )


def _abort_result(request: CommandRequest) -> Result:
    """Abort only an exact same-host registered preview scratch operation.

    Parameters: ``request`` contains a parsed target and required operation ID.
    Returns ``aborted`` only after exact scratch removal and catalog update, or
    ``scratch_cleanup_required`` for another host. No process inspection occurs.
    """
    assert request.operation_id is not None
    try:
        evidence = abort_preview_operation(request.database, request.operation_id)
    except CaptureError as error:
        return _artifact_failure(request, error.code)
    if evidence.state == "scratch_cleanup_required":
        return Result(
            command=request.command,
            ok=False,
            status=Status.SCRATCH_CLEANUP_REQUIRED,
            exit_code=EXIT_MANUAL_RECOVERY_REQUIRED,
            target=request.database,
            snapshot_id=evidence.snapshot_id,
            operation_id=evidence.operation_id,
            diagnostics=(
                Diagnostic(
                    "scratch_cleanup_required",
                    (
                        f"scratch remains on host {evidence.host}; path {evidence.scratch_path}"
                    )[:1024],
                ),
            ),
        )
    return Result(
        command=request.command,
        ok=True,
        status=Status.ABORTED,
        exit_code=0,
        target=request.database,
        snapshot_id=evidence.snapshot_id,
        operation_id=evidence.operation_id,
        diagnostics=(Diagnostic("aborted", "Registered preview scratch was removed."),),
    )


def _install_result(request: CommandRequest) -> Result:
    """Install one exact selected candidate without service-state coordination.

    The request binds uncertainty approval to the immutable report digest and
    delegates all active mutations to the durable installation state machine.
    It never reads stdin, inspects processes, or invokes OpenCode.
    """
    assert request.candidate_id is not None
    outcome = install_from_selection(
        request.database,
        request.candidate_id,
        request.approve_uncertain_report,
        scratch_dir=request.scratch_dir
        or os.path.join(os.environ.get("TMPDIR", "/tmp"), "opencode-db"),
        deadline_seconds=request.deadline_seconds or DEFAULT_DEADLINE_SECONDS,
    )
    return _install_outcome_result(request, outcome)


def _resume_result(request: CommandRequest) -> Result:
    """Resume only the exact durable installation operation selected by the CLI."""
    assert request.operation_id is not None
    return _install_outcome_result(
        request,
        resume_install(
            request.database,
            request.operation_id,
            deadline_seconds=request.deadline_seconds or DEFAULT_DEADLINE_SECONDS,
        ),
    )


def _rollback_result(request: CommandRequest) -> Result:
    """Explicitly restore exact retained source bytes for one incomplete install."""
    assert request.operation_id is not None
    return _install_outcome_result(
        request,
        rollback_install(
            request.database,
            request.operation_id,
            deadline_seconds=request.deadline_seconds or DEFAULT_DEADLINE_SECONDS,
        ),
    )


def _prune_backup_result(request: CommandRequest) -> Result:
    """Prune one catalog-selected retained snapshot without touching the target DB.

    ``request`` supplies an exact parsed target and complete snapshot ID. Returns
    ``backup_pruned`` only after the artifact layer durably removes that group
    and its catalog record; malformed, foreign, changed, or required evidence
    becomes a precondition refusal. The helper never opens SQLite or inspects
    processes.
    """
    assert request.snapshot_id is not None
    try:
        prune_backup(request.database, request.snapshot_id)
    except CaptureError as error:
        return _artifact_failure(request, error.code)
    return Result(
        command=request.command,
        ok=True,
        status=Status.BACKUP_PRUNED,
        exit_code=0,
        target=request.database,
        snapshot_id=request.snapshot_id,
        diagnostics=(Diagnostic("backup_pruned", "The selected backup was pruned."),),
    )


def _install_outcome_result(request: CommandRequest, outcome: object) -> Result:
    """Map one durable installation observation to the closed result schema."""
    state = getattr(outcome, "state", "install_incomplete")
    state_name = state if isinstance(state, str) else "install_incomplete"
    code = getattr(outcome, "code", None)
    code_name = code if isinstance(code, str) else ""
    status = {
        "installed": Status.INSTALLED,
        "rolled_back": Status.ROLLED_BACK,
        "manual_recovery_required": Status.MANUAL_RECOVERY_REQUIRED,
        "source_changed": Status.SOURCE_CHANGED,
        "approval_required": Status.UNCERTAIN,
        "candidate_changed": Status.PRECONDITION_REFUSED,
        "report_changed": Status.PRECONDITION_REFUSED,
        "snapshot_invalid": Status.PRECONDITION_REFUSED,
    }.get(
        code_name,
        {
            "installed": Status.INSTALLED,
            "rolled_back": Status.ROLLED_BACK,
            "manual_recovery_required": Status.MANUAL_RECOVERY_REQUIRED,
        }.get(state_name, Status.INSTALL_INCOMPLETE),
    )
    exit_code = (
        0
        if status in {Status.INSTALLED, Status.ROLLED_BACK}
        else EXIT_DECISION_REQUIRED
        if status is Status.UNCERTAIN
        else EXIT_PRECONDITION_REFUSED
        if status in {Status.SOURCE_CHANGED, Status.PRECONDITION_REFUSED}
        else EXIT_MANUAL_RECOVERY_REQUIRED
        if status is Status.MANUAL_RECOVERY_REQUIRED
        else EXIT_OPERATIONAL_FAILURE
    )
    operation_id = getattr(outcome, "operation_id", None)
    candidate_id = getattr(outcome, "candidate_id", request.candidate_id)
    snapshot_id = getattr(outcome, "snapshot_id", None)
    validation = getattr(outcome, "validation", None)
    return Result(
        command=request.command,
        ok=status in {Status.INSTALLED, Status.ROLLED_BACK, Status.STATUS_OK},
        status=status,
        exit_code=exit_code,
        target=request.database,
        snapshot_id=snapshot_id if snapshot_id != "unknown" else None,
        candidate_id=candidate_id if candidate_id != "unknown" else None,
        operation_id=operation_id
        if operation_id != "unknown"
        else request.operation_id,
        validation=validation if validation is not None else Result().validation,
        next_actions=_install_actions(request.database, operation_id, state_name),
        diagnostics=(Diagnostic("install_state", f"installation is {state_name}"),),
    )


def _install_actions(
    database: str, operation_id: object, state: object
) -> tuple[str, ...]:
    """Return exact recovery actions only for an incomplete persisted install."""
    if (
        not isinstance(operation_id, str)
        or operation_id == "unknown"
        or state not in {"install_incomplete", "rolling_back"}
    ):
        return ()
    target = shlex.quote(database)
    return (
        f"opencode-db cleanup resume --database {target} --operation {operation_id}",
        f"opencode-db cleanup rollback --database {target} --operation {operation_id}",
    )


def _artifact_failure(request: CommandRequest, code: str) -> Result:
    """Map one retained-evidence refusal to a credential-free result object."""
    return Result.failure(
        command=request.command,
        status=Status.PRECONDITION_REFUSED,
        exit_code=EXIT_PRECONDITION_REFUSED,
        diagnostic_code=code,
        diagnostic_message=code,
        target=request.database,
        snapshot_id=request.snapshot_id,
        candidate_id=request.candidate_id,
        operation_id=request.operation_id,
    )


def _status_actions(database: str, operation_id: str, state: str) -> tuple[str, ...]:
    """Return valid bounded next actions for one read-only operation status."""
    if state == "previewing":
        return (
            "opencode-db cleanup abort "
            f"--database {shlex.quote(database)} --operation {operation_id}",
        )
    return ()


def _preview_actions(request: CommandRequest, outcome: object) -> tuple[str, ...]:
    """Return exact bounded follow-up commands for one retained preview result.

    Parameters: ``request`` supplies the explicit target and ``outcome`` is the
    cleanup result. Returns no action for invalid candidates, one exact install
    action for complete candidates, and a report-digest-bound install action for
    uncertain candidates. The helper reads and writes no external state.
    """
    candidate_id = getattr(outcome, "candidate_id", None)
    completeness = getattr(outcome, "completeness", None)
    report_sha256 = getattr(outcome, "report_sha256", None)
    if not isinstance(candidate_id, str):
        return ()
    base = (
        "opencode-db cleanup install "
        f"--database {shlex.quote(request.database)} --candidate {candidate_id}"
    )
    if completeness == "complete":
        return (base,)
    if completeness == "uncertain" and isinstance(report_sha256, str):
        return (f"{base} --approve-uncertain-report {report_sha256}",)
    return ()


def _option(options: dict[str, str | bool], name: str) -> str:
    value = options[name]
    if not isinstance(value, str):
        raise AssertionError(f"{name} must have a value")
    return value


def _absolute_path(value: str, command: str, name: str) -> str:
    if "\x00" in value or value == ":memory:" or not os.path.isabs(value):
        raise CliUsageError(command, f"--{name} requires an absolute filesystem path.")
    return value


def _optional_path(
    options: dict[str, str | bool], name: str, command: str
) -> str | None:
    return (
        _absolute_path(_option(options, name), command, name)
        if name in options
        else None
    )


def _optional_id(options: dict[str, str | bool], name: str, command: str) -> str | None:
    if name not in options:
        return None
    value = _option(options, name)
    if len(value) > MAX_ID_LENGTH or not _ID_PATTERN.fullmatch(value):
        raise CliUsageError(command, f"--{name} requires one exact identifier.")
    return value


def _optional_snapshot_id(options: dict[str, str | bool], command: str) -> str | None:
    """Return only a complete opaque snapshot ID for the prune-backup selector."""
    if "snapshot" not in options:
        return None
    value = _option(options, "snapshot")
    if re.fullmatch(r"snapshot-\d{8}T\d{6}Z-[0-9a-f]{24}", value) is None:
        raise CliUsageError(
            command, "--snapshot requires one complete snapshot identifier."
        )
    return value


def _optional_digest(
    options: dict[str, str | bool], name: str, command: str
) -> str | None:
    if name not in options:
        return None
    value = _option(options, name)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CliUsageError(command, f"--{name} requires a lowercase SHA-256 digest.")
    return value


def _optional_deadline(options: dict[str, str | bool], command: str) -> int | None:
    if "deadline-seconds" not in options:
        return None
    value = _option(options, "deadline-seconds")
    try:
        numeric = float(value)
    except ValueError as error:
        raise CliUsageError(
            command, "--deadline-seconds requires a finite integer."
        ) from error
    if (
        not math.isfinite(numeric)
        or not numeric.is_integer()
        or not 1 <= numeric <= MAX_DEADLINE_SECONDS
    ):
        raise CliUsageError(
            command, "--deadline-seconds is outside supported finite bounds."
        )
    return int(numeric)


def _wants_help(values: Sequence[str]) -> bool:
    return "--help" in values or "-h" in values


def _help_text(values: Sequence[str]) -> str:
    command = " ".join(values[:2])
    synopses = {
        "cleanup preview": "opencode-db cleanup preview --database ABSOLUTE_PATH [--scratch-dir ABSOLUTE_PATH] [--deadline-seconds N] [--json]",
        "cleanup install": "opencode-db cleanup install --database ABSOLUTE_PATH --candidate ID [--approve-uncertain-report SHA256] [--scratch-dir ABSOLUTE_PATH] [--deadline-seconds N] [--json]",
        "cleanup status": "opencode-db cleanup status --database ABSOLUTE_PATH [--operation ID] [--json]",
        "cleanup abort": "opencode-db cleanup abort --database ABSOLUTE_PATH --operation ID [--json]",
        "cleanup resume": "opencode-db cleanup resume --database ABSOLUTE_PATH --operation ID [--deadline-seconds N] [--json]",
        "cleanup rollback": "opencode-db cleanup rollback --database ABSOLUTE_PATH --operation ID [--deadline-seconds N] [--json]",
        "cleanup prune-backup": "opencode-db cleanup prune-backup --database ABSOLUTE_PATH --snapshot ID [--json]",
    }
    if command in synopses:
        return synopses[command] + "\n"
    return (
        "\n".join(["usage: opencode-db cleanup COMMAND [OPTIONS]", *synopses.values()])
        + "\n"
    )
