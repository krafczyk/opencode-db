"""Parse and render the closed OpenCode database cleanup command contract.

Cleanup preserves version-1 result encoding while exposing WAL recovery,
explicit work placement, and bounded storage diagnostics. Other commands use
their human-only interfaces. Execution delegates database mutations to their
command modules and never inspects processes or invokes OpenCode.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

from .artifacts import (
    CaptureError,
    abort_preview_operation,
    capture_source_set,
    load_preview_operation,
    operation_status,
    preview_checkpoint_status,
    prune_backup,
)
from .cleanup import DEFAULT_PREVIEW_DEADLINE_SECONDS, clean_snapshot
from .install import (
    install_from_selection,
    install_status,
    resume_install,
    rollback_install,
)
from .inspect import (
    InspectionError,
    InspectionOperationalError,
    InspectionRequest,
    inspect_database,
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
from .move_cli import (
    MoveCommandRequest,
    execute_move,
    parse_move_command,
)
from .move_repair_cli import (
    MoveRepairCommandRequest,
    execute_move_repair,
    parse_move_repair_command,
)
from .prune import (
    PRUNE_TIMEOUT_SECONDS,
    PruneCommittedError,
    PruneError,
    PruneOperationalError,
    PruneOutcome,
    PruneRequest,
    ReviewedPrunePlan,
    apply_prune_plan,
    parse_selector,
    parse_prune_timeout,
    parse_target_size,
    plan_prune,
)
from .prune_diagnostics import describe_prune_failure
from .transfer import (
    TransferError,
    TransferOperationalError,
    TransferRequest,
    TransferUsageError,
    export_sessions,
    import_sessions,
    parse_transfer_command,
)
from .target import (
    TargetError,
    select_default_database,
    sqlite_temporary_directory,
    storage_failure,
    validate_work_directory,
)

MAX_JSON_BYTES = 64 * 1024
"""Maximum UTF-8 byte length for a machine-mode result, including its newline."""

MAX_ID_LENGTH = 256
"""Maximum length accepted for an exact target-scoped identifier."""

MAX_DEADLINE_SECONDS = 86_400
"""Largest supported finite whole-second deadline."""

DEFAULT_DEADLINE_SECONDS = 1_800
"""Default installation/recovery deadline; preview uses its four-hour default."""

_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_COMMAND_OPTIONS = {
    "preview": {"database", "scratch-dir", "deadline-seconds", "json", "progress"},
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
    "resume": {"database", "operation", "deadline-seconds", "json", "progress"},
    "rollback": {"database", "operation", "deadline-seconds", "json"},
    "prune-backup": {"database", "snapshot", "json"},
}
_REQUIRED_OPTIONS = {
    "preview": set(),
    "install": {"candidate"},
    "status": set(),
    "abort": {"operation"},
    "resume": {"operation"},
    "rollback": {"operation"},
    "prune-backup": {"snapshot"},
}
for _options in _COMMAND_OPTIONS.values():
    _options.add("work-dir")


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


def parse_command(
    arguments: Sequence[str],
) -> CommandRequest | TransferRequest | InspectionRequest | MoveCommandRequest | MoveRepairCommandRequest | PruneRequest:
    """Parse one cleanup, transfer, inspection, move, or move-repair command.

    Parameters: ``arguments`` is an argv sequence excluding the program name.
    Returns a :class:`CommandRequest`, :class:`TransferRequest`,
    :class:`InspectionRequest`, :class:`MoveCommandRequest`,
    :class:`MoveRepairCommandRequest`, or :class:`PruneRequest` with validated
    fields; cleanup requests use a concrete environment-selected database string
    when their option is omitted. Every request accepts an absolute ``work_dir``
    for CLI work placement; execution validates its privacy and local scratch.
    Raises :class:`CliUsageError` or
    :class:`TransferUsageError` for unknown commands, missing options, invalid
    absolute paths, ambiguous IDs, duplicate options, and non-finite deadlines.
    The function never reads stdin, creates a database, or mutates a file.
    """
    values = list(arguments)
    if values and values[0] != "cleanup" and "--work-dir" in values:
        command = values[0]
        if values.count("--work-dir") != 1:
            raise CliUsageError(command, "Command options may not be repeated.")
        position = values.index("--work-dir")
        if position + 1 >= len(values) or values[position + 1].startswith("--"):
            raise CliUsageError(command, "--work-dir is missing its value.")
        work_dir = _absolute_path(values[position + 1], command, "work-dir")
        del values[position:position + 2]
        return replace(parse_command(values), work_dir=work_dir)
    if values and values[0] in {"list-projects", "show-project", "show-session", "list-sessions"}:
        return _parse_inspection_command(values)
    if values and values[0] == "prune":
        return _parse_prune_command(values)
    if values and values[0] in {"export", "import"}:
        return parse_transfer_command(values)
    if values and values[0] == "mv":
        return parse_move_command(
            values,
            usage_error=CliUsageError,
            option=_option,
            absolute_path=_absolute_path,
            optional_id=_optional_id,
            default_database=_default_database,
        )
    if values and values[0] == "repair-move":
        return parse_move_repair_command(
            values,
            usage_error=CliUsageError,
            option=_option,
            absolute_path=_absolute_path,
            optional_id=_optional_id,
            default_database=_default_database,
        )
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
        if option in {"json", "progress"}:
            options[option] = True
            position += 1
            continue
        if position + 1 >= len(values) or values[position + 1].startswith("--"):
            raise CliUsageError(command, "A command option is missing its value.")
        options[option] = values[position + 1]
        position += 2

    if missing := _REQUIRED_OPTIONS[action] - set(options):
        raise CliUsageError(command, f"Missing required option --{sorted(missing)[0]}.")
    database = (
        _absolute_path(_option(options, "database"), command, "database")
        if "database" in options
        else _default_database(command)
    )
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
        work_dir=_optional_path(options, "work-dir", command),
        progress=bool(options.get("progress", False)),
    )


def _parse_inspection_command(arguments: list[str]) -> InspectionRequest:
    """Parse one human-only read-only inspection command without filesystem access.

    Parameters: ``arguments`` begins with a public inspection command. Returns
    an :class:`InspectionRequest` whose optional ``--db`` is syntactically
    absolute. Raises :class:`CliUsageError` for missing, duplicate, unknown, or
    ambiguous arguments. The function does not resolve defaults, open SQLite,
    create files, or inspect processes.
    """
    command = arguments[0]
    allowed = {
        "list-projects": {"db", "estimate-project-size"},
        "show-project": {"db", "project-id", "estimate-project-size"},
        "show-session": {"db", "session-id", "estimate-session-size"},
        "list-sessions": {"db", "project-id", "estimate-session-size"},
    }[command]
    required = {
        "list-projects": set(),
        "show-project": {"project-id"},
        "show-session": {"session-id"},
        "list-sessions": set(),
    }[command]
    boolean_options = {"estimate-project-size", "estimate-session-size"}
    options: dict[str, str | bool] = {}
    position = 1
    while position < len(arguments):
        token = arguments[position]
        if not token.startswith("--"):
            raise CliUsageError(command, "Unexpected positional argument.")
        name = token[2:]
        if name not in allowed:
            raise CliUsageError(command, "Unsupported command option.")
        if name in options:
            raise CliUsageError(command, "Command options may not be repeated.")
        if name in boolean_options:
            options[name] = True
            position += 1
            continue
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            raise CliUsageError(command, "A command option is missing its value.")
        options[name] = arguments[position + 1]
        position += 2
    if missing := required - set(options):
        raise CliUsageError(command, f"Missing required option --{sorted(missing)[0]}.")
    database = (
        _absolute_path(_option(options, "db"), command, "db")
        if "db" in options
        else None
    )
    project_id = _optional_id(options, "project-id", command)
    session_id = _optional_id(options, "session-id", command)
    return InspectionRequest(
        command,
        database,
        project_id,
        session_id,
        bool(options.get("estimate-project-size", False)),
        bool(options.get("estimate-session-size", False)),
    )


def _parse_prune_command(arguments: list[str]) -> PruneRequest:
    """Parse one active session prune command without opening SQLite.

    Parameters: ``arguments`` begins with ``prune``. Returns one
    :class:`PruneRequest` with an explicit or bounded-default database target and
    exactly one validated retention selector or vacuum-only mode. Raises
    :class:`CliUsageError` for malformed paths, duplicate/unknown options,
    invalid IDs, or selectors. This function neither reads stdin nor changes a
    database.
    """
    command = "prune"
    allowed = {"db", "project-id", "oldest", "keep-newest", "estimate-size", "target-size", "timeout-seconds", "vacuum", "vacuum-only", "yes"}
    boolean_options = {"estimate-size", "vacuum", "vacuum-only", "yes"}
    options: dict[str, str | bool] = {}
    position = 1
    while position < len(arguments):
        token = arguments[position]
        if not token.startswith("--"):
            raise CliUsageError(command, "Unexpected positional argument.")
        name = token[2:]
        if name not in allowed:
            raise CliUsageError(command, "Unsupported command option.")
        if name in options:
            raise CliUsageError(command, "Command options may not be repeated.")
        if name in boolean_options:
            options[name] = True
            position += 1
            continue
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            raise CliUsageError(command, "A command option is missing its value.")
        options[name] = arguments[position + 1]
        position += 2
    selectors = [name for name in ("oldest", "keep-newest", "target-size") if name in options]
    vacuum_only = bool(options.get("vacuum-only", False))
    if vacuum_only and (
        selectors
        or "project-id" in options
        or "estimate-size" in options
        or "vacuum" in options
    ):
        raise CliUsageError(
            command,
            "--vacuum-only cannot be combined with prune selectors, --project-id, --estimate-size, or --vacuum.",
        )
    if not vacuum_only and len(selectors) != 1:
        raise CliUsageError(command, "Exactly one prune selector is required.")
    if selectors:
        selector = selectors[0]
        try:
            if selector == "target-size":
                parse_target_size(_option(options, selector))
            else:
                parse_selector(_option(options, selector))
        except PruneError as error:
            raise CliUsageError(command, str(error)) from error
    timeout_seconds = PRUNE_TIMEOUT_SECONDS
    if "timeout-seconds" in options:
        try:
            timeout_seconds = parse_prune_timeout(_option(options, "timeout-seconds"))
        except PruneError as error:
            raise CliUsageError(command, str(error)) from error
    database = _absolute_path(_option(options, "db"), command, "db") if "db" in options else _default_database(command)
    return PruneRequest(
        database=database,
        project_id=_optional_id(options, "project-id", command),
        oldest=_option(options, "oldest") if "oldest" in options else None,
        keep_newest=_option(options, "keep-newest") if "keep-newest" in options else None,
        target_size=_option(options, "target-size") if "target-size" in options else None,
        estimate_size=bool(options.get("estimate-size", False)),
        vacuum=bool(options.get("vacuum", False)),
        yes=bool(options.get("yes", False)),
        timeout_seconds=timeout_seconds,
        vacuum_only=vacuum_only,
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
        if result.operation_id:
            lines.insert(3, f"operation: {result.operation_id}")
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
        lines.extend(f"diagnostic: {item.message}" for item in result.diagnostics)
        return "\n".join(lines) + "\n"
    if result.diagnostics:
        return "\n".join([
            *(f"opencode-db: {item.message}" for item in result.diagnostics),
            *(f"next: {action}" for action in result.next_actions),
        ]) + "\n"
    return f"opencode-db: {result.status.value}\n"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the parser and renderer for console-script or module invocation.

    Parameters: ``arguments`` optionally replaces ``sys.argv[1:]``. Returns the
    documented exit class. Machine-mode parse errors emit one JSON result on
    stdout and no diagnostics there. ``cleanup preview`` captures an explicit
    target and cleans only a fresh scratch copy. Work-root selection preserves
    retained evidence across cleanup commands and sets the CLI temporary
    environment before SQLite work. ``mv`` and
    ``prune`` and ``repair-move`` read one terminal confirmation unless
    ``--yes`` is explicit.
    """
    values = list(sys.argv[1:] if arguments is None else arguments)
    if _wants_help(values):
        sys.stdout.write(_help_text(values))
        return 0
    json_mode = "--json" in values and (
        not values
        or values[0] not in {"list-projects", "show-project", "show-session", "list-sessions", "prune", "mv", "repair-move"}
    )
    try:
        request = parse_command(values)
    except (CliUsageError, TransferUsageError) as error:
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

    try:
        if isinstance(request, CommandRequest):
            if request.work_dir is not None:
                validate_work_directory(request.work_dir)
            temporary = _scratch_dir(request)
        else:
            temporary = request.work_dir
        with sqlite_temporary_directory(
            temporary,
            validate=not isinstance(request, CommandRequest),
            update_tmpdir=not isinstance(request, CommandRequest),
        ):
            return _dispatch_request(request)
    except TargetError as error:
        failure = Result.failure(
            command=request.command,
            status=Status.PRECONDITION_REFUSED,
            exit_code=EXIT_PRECONDITION_REFUSED,
            diagnostic_code=error.code,
            diagnostic_message="",
        )
        failure = _with_storage_diagnostic(failure, error.diagnostic)
        if isinstance(request, CommandRequest) and request.json:
            sys.stdout.write(render_json(failure))
        else:
            sys.stderr.write(render_human(failure))
        return EXIT_PRECONDITION_REFUSED


def _dispatch_request(request: CommandRequest | TransferRequest | InspectionRequest | PruneRequest | MoveCommandRequest | MoveRepairCommandRequest) -> int:
    """Execute a parsed request within its selected CLI temporary environment."""
    if isinstance(request, TransferRequest):
        return _execute_transfer(request)
    if isinstance(request, InspectionRequest):
        return _execute_inspection(request)
    if isinstance(request, PruneRequest):
        return _execute_prune(request)
    if isinstance(request, MoveCommandRequest):
        return execute_move(request, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    if isinstance(request, MoveRepairCommandRequest):
        return execute_move_repair(request, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    result = _execute(request)
    if request.json:
        sys.stdout.write(render_json(result))
    else:
        sys.stderr.write(render_human(result))
    return result.exit_code


def _execute_inspection(request: InspectionRequest) -> int:
    """Render one read-only inspection view to stdout without cleanup result encoding.

    Parameters: ``request`` has validated grammar and a possibly implicit
    database selection. Returns zero for a rendered view, the safety refusal
    class for unavailable selections/schema/data, or the operational class for
    bounded filesystem/SQLite read failures. It never opens the database writable
    or prints transcript content except from ``show-session``.
    """
    try:
        sys.stdout.write(
            inspect_database(
                request.database,
                request.command,
                project_id=request.project_id,
                session_id=request.session_id,
                estimate_project_size=request.estimate_project_size,
                estimate_session_size=request.estimate_session_size,
            )
        )
        return 0
    except InspectionOperationalError as error:
        sys.stderr.write(f"opencode-db: {_command_error(error, request.database, request.work_dir)}\n")
        return EXIT_OPERATIONAL_FAILURE
    except InspectionError as error:
        sys.stderr.write(f"opencode-db: {error}\n")
        return EXIT_PRECONDITION_REFUSED


def _execute_prune(request: PruneRequest) -> int:
    """Preview, authorize, and execute one active session prune safely.

    Parameters: ``request`` has passed grammar validation. Returns zero after a
    committed prune, requested vacuum, or reviewed zero-selection outcome; the
    safety-refusal exit code for detached streams, cancellation, unsupported
    schema, exact-project failures, or stale evidence; and the operational-failure
    exit code for SQLite, filesystem, or preview-output failures. The command
    calculates, renders, and flushes aggregate preview evidence from a read-only
    snapshot before terminal checks, input, or writable application. Unless
    ``request.yes`` is set, only an exact lowercase terminal response authorizes
    a remaining mutation. Operational failures include bounded phase, numeric
    SQLite/OS cause, location and recovery details. Output never contains session
    identifiers, raw SQLite messages, SQL, or content.
    """
    try:
        reviewed = plan_prune(request)
        _render_prune_preview(reviewed, sys.stdout)
        if reviewed.preview.sessions_to_prune == 0 and not (
            request.vacuum or request.vacuum_only
        ):
            outcome = apply_prune_plan(reviewed)
            sys.stdout.write(_render_prune_outcome(request, outcome))
            return 0
        if not request.yes:
            if not _is_terminal(sys.stdin) or not _is_terminal(sys.stdout):
                sys.stderr.write(
                    "opencode-db: prune requires terminal stdin and stdout unless --yes; no changes were applied\n"
                )
                return EXIT_PRECONDITION_REFUSED
            _write_prune_confirmation_prompt(reviewed, sys.stdout)
            try:
                response = sys.stdin.readline()
            except KeyboardInterrupt:
                response = ""
            if response not in ("y\n", "y\r", "y\r\n"):
                sys.stderr.write("opencode-db: prune cancelled; no changes were applied\n")
                return EXIT_PRECONDITION_REFUSED
        outcome = apply_prune_plan(reviewed)
        sys.stdout.write(_render_prune_outcome(request, outcome))
        return 0
    except PruneCommittedError as error:
        sys.stdout.write(_render_prune_outcome(request, error.outcome))
        sys.stderr.write(f"opencode-db: {describe_prune_failure(error, request.database, request.work_dir)}\n")
        return EXIT_OPERATIONAL_FAILURE
    except PruneOperationalError as error:
        sys.stderr.write(f"opencode-db: {describe_prune_failure(error, request.database, request.work_dir)}\n")
        return EXIT_OPERATIONAL_FAILURE
    except PruneError as error:
        sys.stderr.write(f"opencode-db: {error}\n")
        return EXIT_PRECONDITION_REFUSED


def _render_prune_preview(reviewed: ReviewedPrunePlan, stdout: object) -> None:
    """Write and flush one canonical aggregate prune preview.

    Parameters: ``reviewed`` is an immutable read-only prune or vacuum plan and
    ``stdout`` is the selected human-output stream. Returns ``None`` after writing the
    complete operation-specific preview and flushing it before any writable
    application. Raises :class:`PruneOperationalError` when output cannot be
    delivered and :class:`PruneError` for an unrenderable persisted timestamp;
    neither failure exposes session identifiers or content.
    """
    preview = reviewed.preview
    try:
        if reviewed.request.vacuum_only:
            lines = ["opencode-db: vacuum preview", "session_rows_changed: 0"]
        else:
            timestamp = _render_prune_preview_timestamp(
                preview.oldest_surviving_session_updated
            )
            lines = [
                "opencode-db: prune preview",
                f"sessions_to_prune: {preview.sessions_to_prune}",
                f"sessions_to_keep: {preview.sessions_to_keep}",
                f"oldest_surviving_session_updated: {timestamp}",
            ]
            if reviewed.request.estimate_size:
                lines.extend(
                    (
                        "projected_logical_bytes_deleted: "
                        f"{preview.projected_logical_bytes_deleted}",
                        "projected_logical_database_bytes_after_prune: "
                        f"{preview.projected_logical_database_bytes_after_prune}",
                    )
                )
        rendered = "\n".join(lines) + "\n"
        if stdout.write(rendered) != len(rendered):
            raise PruneOperationalError("could not write prune preview", phase="preview_output")
        stdout.flush()
    except PruneError:
        raise
    except (AttributeError, OSError, ValueError, TypeError) as error:
        raise PruneOperationalError("could not write prune preview", phase="preview_output") from error


def _render_prune_preview_timestamp(value: int | None) -> str:
    """Render one bounded epoch-millisecond preview time in local ISO 8601 form.

    Parameters: ``value`` is the retained session update time from reviewed
    aggregate evidence, or ``None`` when the selected scope has no survivor.
    Returns ``none`` or a timezone-aware local ISO 8601 timestamp with a numeric
    UTC offset. Raises :class:`PruneError` for malformed or unrepresentable
    persisted values without exposing the value itself.
    """
    if value is None:
        return "none"
    if type(value) is not int:
        raise PruneError("prune preview timestamp is invalid")
    try:
        seconds, milliseconds = divmod(value, 1_000)
        rendered = (
            datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=seconds, milliseconds=milliseconds)
        ).astimezone()
        return rendered.isoformat()
    except (OSError, OverflowError, ValueError) as error:
        raise PruneError("prune preview timestamp is invalid") from error


def _prune_confirmation_prompt(reviewed: ReviewedPrunePlan) -> str:
    """Return the exact confirmation prompt for the reviewed remaining mutation.

    Parameters: ``reviewed`` provides aggregate selected-session count and
    the reviewed request carries the vacuum option. Returns the terminal prompt
    for a prune, vacuum, or both; it performs no I/O and exposes no private evidence.
    """
    if reviewed.preview.sessions_to_prune == 0:
        return "Vacuum database? [y/N] "
    if reviewed.request.vacuum:
        return "Prune matching sessions and vacuum database? [y/N] "
    return "Prune matching sessions? [y/N] "


def _write_prune_confirmation_prompt(
    reviewed: ReviewedPrunePlan, stdout: object
) -> None:
    """Write and flush the complete prompt before reading authorization input.

    Parameters: ``reviewed`` selects the mutation-specific prompt and ``stdout``
    is the already validated terminal stream. Returns ``None`` after
    complete delivery. Raises :class:`PruneOperationalError` on a short write or
    output failure, before writable application begins.
    """
    prompt = _prune_confirmation_prompt(reviewed)
    try:
        if stdout.write(prompt) != len(prompt):
            raise PruneOperationalError("could not write prune confirmation prompt", phase="confirmation_output")
        stdout.flush()
    except PruneError:
        raise
    except (AttributeError, OSError, ValueError, TypeError) as error:
        raise PruneOperationalError(
            "could not write prune confirmation prompt", phase="confirmation_output"
        ) from error


def _render_prune_outcome(request: PruneRequest, outcome: PruneOutcome) -> str:
    """Render safe aggregate prune results, including a committed partial result."""
    if request.vacuum_only:
        return (
            "opencode-db: vacuumed\n"
            f"physical_database_bytes_after_vacuum: {outcome.physical_database_bytes}\n"
        )
    lines = [
        "opencode-db: pruned",
        f"pruned_sessions: {outcome.deleted_sessions}",
    ]
    if request.estimate_size:
        lines.extend(
            (
                f"estimated_logical_bytes_deleted: {outcome.deleted_logical_bytes}",
                "estimated_logical_database_bytes_after_prune: "
                f"{outcome.logical_database_bytes}",
            )
        )
    physical_bytes = outcome.physical_database_bytes
    if physical_bytes is not None:
        lines.append(f"physical_database_bytes_after_vacuum: {physical_bytes}")
    return "\n".join(lines) + "\n"


def _is_terminal(stream: object) -> bool:
    """Return terminal capability without assuming a stream exposes ``isatty``."""
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except OSError:
        return False


def _execute_transfer(request: TransferRequest) -> int:
    """Run one transfer command without using the closed cleanup result schema.

    Parameters: ``request`` is a grammar-validated transfer selection with a
    concrete explicit or environment-selected database path.
    Returns zero after successful export or import, the operational-failure exit
    class for filesystem, SQLite, integrity, or publication failures, and the
    precondition-refusal class for safety refusals. It reports only paths,
    project IDs, and row counts, not session content; archive/database work is
    delegated to :mod:`transfer`.
    """
    try:
        if request.command == "export":
            assert request.export_dir is not None
            outcome = export_sessions(
                request.database, request.project_dir, request.export_dir
            )
            sys.stdout.write(
                "\n".join(
                    (
                        "opencode-db: exported",
                        f"export_dir: {outcome.export_dir}",
                        f"source_project_id: {outcome.source_project_id}",
                        f"import_file: {outcome.import_file}",
                        f"exported_sessions: {outcome.exported_sessions}",
                    )
                )
                + "\n"
            )
            return 0
        assert request.import_file is not None
        outcome = import_sessions(
            request.project_dir, request.database, request.import_file
        )
        sys.stdout.write(
            "\n".join(
                (
                    "opencode-db: imported",
                    f"target_project_id: {outcome.target_project_id}",
                    f"imported_sessions: {outcome.imported_sessions}",
                )
            )
            + "\n"
        )
        return 0
    except TransferOperationalError as error:
        sys.stderr.write(f"opencode-db: {_command_error(error, request.database, request.export_dir)}\n")
        return EXIT_OPERATIONAL_FAILURE
    except TransferError as error:
        sys.stderr.write(f"opencode-db: {error}\n")
        return EXIT_PRECONDITION_REFUSED


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
    deadline = request.deadline_seconds or DEFAULT_PREVIEW_DEADLINE_SECONDS
    progress = _preview_progress(request)
    progress("hashing and capturing source")
    started = time.monotonic()
    capture = capture_source_set(
        request.database,
        scratch_dir=_scratch_dir(request),
        work_dir=request.work_dir,
        deadline_seconds=deadline,
        on_operation=lambda snapshot, operation: progress(
            f"operation {operation}; snapshot {snapshot}\nnext: {_preview_resume_action(request, operation)}"
        ),
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
            "work_invalid",
            "work_not_private",
        }:
            status = Status.PRECONDITION_REFUSED
            exit_code = EXIT_PRECONDITION_REFUSED
        else:
            status = Status.OPERATIONAL_FAILURE
            exit_code = EXIT_OPERATIONAL_FAILURE
        failure = Result.failure(
            command=request.command,
            status=status,
            exit_code=exit_code,
            diagnostic_code=capture.status,
            diagnostic_message=capture.status,
            target=capture.target or request.database,
            snapshot_id=capture.snapshot_id,
            operation_id=capture.operation_id,
        )
        return _with_storage_diagnostic(failure, capture.diagnostic_message)
    scratch = _scratch_dir(request)
    remaining = max(0, deadline - math.ceil(time.monotonic() - started))
    outcome = clean_snapshot(
        capture.snapshot_dir,
        scratch_dir=scratch,
        deadline_seconds=remaining,
        progress=progress,
    )
    return _preview_outcome_result(request, outcome, capture.operation_id, capture.snapshot_id)


def _preview_outcome_result(request: CommandRequest, outcome: object, operation_id: str | None, snapshot_id: str | None) -> Result:
    """Render completed candidates or paused work with the exact resume selector."""
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
        if outcome.diagnostic_message:
            diagnostics = (Diagnostic(outcome.diagnostic_code, outcome.diagnostic_message),)
    if outcome.resumable:
        diagnostics += (Diagnostic("preview_paused", "Preview paused; completed stages were retained. Resume with a fresh deadline."),)
    if outcome.diagnostic_code in {"preview_busy", "preview_checkpoint_invalid", "preview_not_resumable", "scratch_wrong_host", "snapshot_invalid", "artifact_schema_unsupported", "candidate_changed", "report_changed", "artifact_not_private"}:
        status = Status.PRECONDITION_REFUSED
        exit_code = EXIT_PRECONDITION_REFUSED
    return Result(
        command=request.command,
        ok=outcome.completeness == "complete",
        status=status,
        exit_code=exit_code,
        target=outcome.target if outcome.target != "unknown" else request.database,
        snapshot_id=snapshot_id,
        operation_id=operation_id,
        candidate_id=outcome.candidate_id,
        report_sha256=outcome.report_sha256,
        completeness=outcome.completeness,
        validation=outcome.validation,
        preview=outcome.preview,
        next_actions=(
            (_preview_resume_action(request, operation_id),)
            if outcome.resumable and operation_id else _preview_actions(request, outcome)
        ),
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
            outcome = install_status(request.database, request.operation_id, work_dir=request.work_dir)
        except Exception:
            return _artifact_failure(request, "candidate_changed")
        return _install_outcome_result(request, outcome)
    try:
        evidence = operation_status(request.database, request.operation_id, work_dir=request.work_dir)
        stage = preview_checkpoint_status(request.database, request.operation_id, work_dir=request.work_dir)
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
                f"{evidence.host or 'unknown'}; scratch {location}; saved stage {stage or 'none'}"[:1024],
            ),
        ),
        next_actions=_status_actions(
            request.database, evidence.operation_id, evidence.state, request.work_dir
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
        evidence = abort_preview_operation(request.database, request.operation_id, work_dir=request.work_dir)
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
        scratch_dir=_scratch_dir(request),
        work_dir=request.work_dir,
        deadline_seconds=request.deadline_seconds or DEFAULT_DEADLINE_SECONDS,
    )
    return _install_outcome_result(request, outcome)


def _resume_result(request: CommandRequest) -> Result:
    """Resume the exact selected preview checkpoint or installation intent log."""
    assert request.operation_id is not None
    if request.operation_id.startswith("operation-"):
        try:
            snapshot_dir, evidence = load_preview_operation(
                request.database, request.operation_id, work_dir=request.work_dir,
            )
        except (CaptureError, TargetError) as error:
            return _artifact_failure(request, error.code)
        scratch = str(Path(evidence.scratch_path).parent) if evidence.scratch_path else _scratch_dir(request)
        resumed = replace(request, scratch_dir=scratch)
        with sqlite_temporary_directory(scratch, validate=False, update_tmpdir=False):
            outcome = clean_snapshot(
                snapshot_dir, scratch_dir=scratch,
                deadline_seconds=request.deadline_seconds or DEFAULT_PREVIEW_DEADLINE_SECONDS,
                progress=_preview_progress(request),
            )
        return _preview_outcome_result(resumed, outcome, evidence.operation_id, evidence.snapshot_id)
    return _install_outcome_result(
        request,
        resume_install(
            request.database,
            request.operation_id,
            work_dir=request.work_dir,
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
            work_dir=request.work_dir,
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
        prune_backup(request.database, request.snapshot_id, work_dir=request.work_dir)
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
    diagnostic = getattr(outcome, "diagnostic_message", None)
    if diagnostic is None:
        diagnostic = (
            Result.failure(
                command=request.command,
                status=status,
                exit_code=exit_code,
                diagnostic_code=code_name,
                diagnostic_message="",
            ).diagnostics[0].message
            if code_name else f"installation is {state_name}"
        )
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
        next_actions=_install_actions(request.database, operation_id, state_name, request.work_dir),
        diagnostics=(Diagnostic(code_name or "install_state", diagnostic),),
    )


def _install_actions(
    database: str, operation_id: object, state: object, work_dir: str | None = None
) -> tuple[str, ...]:
    """Return exact recovery actions only for an incomplete persisted install."""
    if (
        not isinstance(operation_id, str)
        or operation_id == "unknown"
        or state not in {"install_incomplete", "rolling_back"}
    ):
        return ()
    target = shlex.quote(database) + _work_option(work_dir)
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


def _status_actions(database: str, operation_id: str, state: str, work_dir: str | None = None) -> tuple[str, ...]:
    """Return valid bounded next actions for one read-only operation status."""
    if operation_id.startswith("operation-") and state in {"capturing", "captured", "previewing", "invalid", "complete", "uncertain"}:
        actions = (
            f"opencode-db cleanup resume --database {shlex.quote(database)}{_work_option(work_dir)} --operation {operation_id} --deadline-seconds {DEFAULT_PREVIEW_DEADLINE_SECONDS}",
        )
        if state == "previewing":
            actions += (
                "opencode-db cleanup abort "
                f"--database {shlex.quote(database)}{_work_option(work_dir)} --operation {operation_id}",
            )
        return actions
    return ()


def _preview_resume_action(request: CommandRequest, operation_id: str) -> str:
    """Build a copyable selector with a fresh caller-selected attempt budget."""
    return (
        f"opencode-db cleanup resume --database {shlex.quote(request.database)}"
        f"{_work_option(request.work_dir)} --operation {operation_id}"
        + f" --deadline-seconds {request.deadline_seconds or DEFAULT_PREVIEW_DEADLINE_SECONDS}"
        + (" --progress" if request.progress else "")
    )


def _preview_progress(request: CommandRequest) -> Callable[[str], None]:
    """Emit bounded stage/elapsed updates on stderr, automatically on a human TTY."""
    started = time.monotonic()
    enabled = request.progress or (not request.json and bool(getattr(sys.stderr, "isatty", lambda: False)()))

    def announce(stage: str) -> None:
        if enabled:
            try:
                sys.stderr.write(f"opencode-db [{time.monotonic() - started:.0f}s]: {stage}\n")
                sys.stderr.flush()
            except OSError:
                pass
    return announce


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
        + _work_option(request.work_dir)
        + (f" --scratch-dir {shlex.quote(request.scratch_dir)}" if request.scratch_dir else "")
    )
    if completeness == "complete":
        return (base,)
    if completeness == "uncertain" and isinstance(report_sha256, str):
        return (f"{base} --approve-uncertain-report {report_sha256}",)
    return ()


def _scratch_dir(request: CommandRequest) -> str:
    """Select explicit scratch, work-root scratch, or the existing TMPDIR default."""
    return request.scratch_dir or os.path.join(
        request.work_dir or os.environ.get("TMPDIR", "/tmp"),
        "scratch" if request.work_dir else "opencode-db",
    )


def _work_option(work_dir: str | None) -> str:
    """Preserve the explicit retained-store selection in copyable follow-ups."""
    return f" --work-dir {shlex.quote(work_dir)}" if work_dir else ""


def _with_storage_diagnostic(result: Result, message: str | None) -> Result:
    """Attach only a tool-generated capacity diagnostic, never raw exceptions."""
    if message is None:
        return result
    return replace(result, diagnostics=(Diagnostic(result.diagnostics[0].code, message),))


def _command_error(error: BaseException, database: str | None, work: str | None = None) -> str:
    """Report storage error causes with known paths and safe current counts."""
    paths = [Path(database).parent] if database else []
    if work:
        paths.append(Path(work))
    paths.append(Path(os.environ.get("SQLITE_TMPDIR", os.environ.get("TMPDIR", "/tmp"))))
    return storage_failure(error, *paths) or str(error)


def _option(options: dict[str, str | bool], name: str) -> str:
    value = options[name]
    if not isinstance(value, str):
        raise AssertionError(f"{name} must have a value")
    return value


def _absolute_path(value: str, command: str, name: str) -> str:
    if "\x00" in value or value == ":memory:" or not os.path.isabs(value):
        raise CliUsageError(command, f"--{name} requires an absolute filesystem path.")
    return value


def _default_database(command: str) -> str:
    """Return the environment-selected target or one bounded usage refusal.

    Parameters: ``command`` identifies the parser context. Returns the concrete
    absolute default path without inspecting it. Raises :class:`CliUsageError`
    when neither documented environment base is absolute; later command layers
    retain responsibility for file validation and database access.
    """
    try:
        return select_default_database()
    except TargetError as error:
        raise CliUsageError(command, "Default database path is unavailable.") from error


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
    return values == ["cleanup"] or "--help" in values or "-h" in values


def _help_text(values: Sequence[str]) -> str:
    top_level = {"mv", "repair-move", "export", "import", "list-projects", "show-project", "show-session", "list-sessions", "prune"}
    command = values[0] if values and values[0] in top_level else " ".join(values[:2])
    synopses = {
        "mv": "opencode-db mv --project-id ID --target-project-dir ABSOLUTE_TARGET_PROJECT_DIR [--db ABSOLUTE_DB] [--method sibling] [--application-timeout-seconds SECONDS] [--yes] [--progress]",
        "repair-move": "opencode-db repair-move --project-id ID --source-project-dir ABSOLUTE_SOURCE_PROJECT_DIR --target-project-dir ABSOLUTE_TARGET_PROJECT_DIR [--db ABSOLUTE_DB] [--application-timeout-seconds SECONDS] [--yes]",
        "list-projects": "opencode-db list-projects [--db ABSOLUTE_DB] [--estimate-project-size]",
        "show-project": "opencode-db show-project [--db ABSOLUTE_DB] --project-id ID [--estimate-project-size]",
        "show-session": "opencode-db show-session [--db ABSOLUTE_DB] --session-id ID [--estimate-session-size]",
        "list-sessions": "opencode-db list-sessions [--db ABSOLUTE_DB] [--estimate-session-size] [--project-id ID]",
        "prune": "opencode-db prune [--db ABSOLUTE_DB] ([--project-id ID] (--oldest N|TIME | --keep-newest N|TIME | --target-size N[B|KiB|MiB|GiB|TiB]) [--estimate-size] [--vacuum] | --vacuum-only) [--timeout-seconds SECONDS] [--yes]",
        "export": "opencode-db export [--db ABSOLUTE_DB] --project-dir ABSOLUTE_PROJECT_DIR --export-dir ABSOLUTE_EXPORT_DIR",
        "import": "opencode-db import --target-project-dir ABSOLUTE_TARGET_PROJECT_DIR [--db ABSOLUTE_DB] --import ABSOLUTE_IMPORT_FILE",
        "cleanup preview": "opencode-db cleanup preview [--database ABSOLUTE_PATH] [--scratch-dir ABSOLUTE_PATH] [--deadline-seconds N] [--json]",
        "cleanup install": "opencode-db cleanup install [--database ABSOLUTE_PATH] --candidate ID [--approve-uncertain-report SHA256] [--scratch-dir ABSOLUTE_PATH] [--deadline-seconds N] [--json]",
        "cleanup status": "opencode-db cleanup status [--database ABSOLUTE_PATH] [--operation ID] [--json]",
        "cleanup abort": "opencode-db cleanup abort [--database ABSOLUTE_PATH] --operation ID [--json]",
        "cleanup resume": "opencode-db cleanup resume [--database ABSOLUTE_PATH] --operation ID [--deadline-seconds N] [--json]",
        "cleanup rollback": "opencode-db cleanup rollback [--database ABSOLUTE_PATH] --operation ID [--deadline-seconds N] [--json]",
        "cleanup prune-backup": "opencode-db cleanup prune-backup [--database ABSOLUTE_PATH] --snapshot ID [--json]",
    }
    details = {
        "prune": (
            "Preview fields: sessions_to_prune, sessions_to_keep, "
            "oldest_surviving_session_updated, and optional projected logical bytes.\n"
            "--yes bypasses only the prompt; zero matches skip confirmation unless "
            "--vacuum remains. --vacuum-only compacts without deleting sessions, and "
            "stale previews refuse with instructions to rerun."
        )
    }
    wal_help = (
        "WAL repair (stop OpenCode first):\n"
        "  1. opencode-db cleanup preview [--database ABSOLUTE_PATH] [--work-dir ABSOLUTE_PATH]\n"
        "     Replays/checkpoints WAL on a copy and validates a sidecar-free candidate.\n"
        "  2. Run the exact 'next' cleanup install command printed by preview.\n"
        "     Only install replaces the active database; snapshots are retained.\n"
        "--work-dir selects an existing mode-0700 directory for snapshots, candidates,\n"
        "reports and default scratch (WORK_DIR/scratch). Reuse it for later cleanup commands.\n"
        "--scratch-dir overrides only local SQLite scratch; final install staging stays beside the database.\n"
    )
    synopses = {
        name: synopsis + " [--work-dir ABSOLUTE_PATH]" + (" [--progress]" if name in {"cleanup preview", "cleanup resume"} else "")
        for name, synopsis in synopses.items()
    }
    wal_help += (
        "Preview saves completed stages. If interrupted or timed out, use cleanup resume\n"
        "with its operation ID and the same --work-dir; registered local scratch is reused.\n"
        "--progress prints stage/elapsed updates on stderr (automatic on a human terminal).\n"
        "A single SQLite backup or integrity scan restarts if interrupted mid-stage; use\n"
        "--deadline-seconds to adjust the four-hour preview/resume default (max 86400).\n"
        "Initial source capture must finish first; partial capture is not resumable.\n"
    )
    if values and values[0] in top_level:
        selected = values[0]
        detail = (f"\n{details[selected]}" if selected in details else "") + (
            "\n--work-dir selects an existing private local directory for SQLite temporary files.\n"
            "The active database, WAL/journal and export archives keep their selected locations."
        )
        return synopses[selected] + detail + "\n"
    if command in synopses:
        return synopses[command] + "\n" + wal_help
    if values and values[0] == "cleanup":
        return wal_help + "\n" + "\n".join(
            synopsis for name, synopsis in synopses.items() if name.startswith("cleanup ")
        ) + "\n"
    return (
        "\n".join(
            ["usage: opencode-db (cleanup COMMAND | mv | repair-move | export | import | list-projects | show-project | show-session | list-sessions | prune) [OPTIONS]", wal_help, *synopses.values()]
        )
        + "\n"
    )
