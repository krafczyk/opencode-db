"""Parse and render the closed OpenCode database cleanup command contract.

The module freezes the version-1 grammar and result encoding. It does not open
SQLite, inspect processes, read stdin, create databases, or mutate artifacts;
those explicit operations belong to later implementation units.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Sequence

from .model import (
    EXIT_PRECONDITION_REFUSED,
    EXIT_USAGE,
    CommandRequest,
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
    snapshot_id = _optional_id(options, "snapshot", command)
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
    if result.diagnostics:
        return f"opencode-db: {result.diagnostics[0].message}\n"
    return f"opencode-db: {result.status.value}\n"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the parser and renderer for console-script or module invocation.

    Parameters: ``arguments`` optionally replaces ``sys.argv[1:]``. Returns the
    documented exit class. Machine-mode parse errors emit one JSON result on
    stdout and no diagnostics there; valid commands safely refuse execution
    until later units implement target and artifact operations. The function
    does not prompt, read stdin, open SQLite, create databases, or mutate files.
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

    result = Result.failure(
        command=request.command,
        status=Status.PRECONDITION_REFUSED,
        exit_code=EXIT_PRECONDITION_REFUSED,
        diagnostic_code="bootstrap_unavailable",
        diagnostic_message="execution is deferred",
        target=request.database,
    )
    if request.json:
        sys.stdout.write(render_json(result))
    else:
        sys.stderr.write(render_human(result))
    return result.exit_code


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
