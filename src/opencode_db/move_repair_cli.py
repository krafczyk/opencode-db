"""Implement the human-only ``repair-move`` command boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import math

from .model import EXIT_OPERATIONAL_FAILURE, EXIT_PRECONDITION_REFUSED
from .move import (
    MAX_WRITER_TRANSACTION_TIMEOUT_SECONDS,
    MoveError,
    MoveOperationalError,
    WRITER_TRANSACTION_TIMEOUT_SECONDS,
)
from .move_cli import _is_terminal, _quote_move_value
from .move_repair import (
    MoveRepairRequest,
    ReviewedMoveRepairPlan,
    apply_move_repair,
    plan_move_repair,
)


RepairOptions = dict[str, str | bool]
"""Parsed repair option values before shared CLI validation."""


@dataclass(frozen=True)
class MoveRepairCommandRequest:
    """Represent one fully parsed human-only move repair command.

    ``database`` and path fields are absolute grammar-validated values,
    ``project_id`` selects one project, ``yes`` bypasses only confirmation, and
    ``application_timeout_seconds`` bounds the complete writer transaction.
    Constructing this value performs no I/O.
    """

    database: str
    project_id: str
    source_project_dir: str
    target_project_dir: str
    yes: bool
    application_timeout_seconds: float

    @property
    def command(self) -> str:
        """Return the stable top-level command name without side effects."""
        return "repair-move"


def parse_move_repair_command(
    arguments: list[str],
    *,
    usage_error: Callable[[str, str], Exception],
    option: Callable[[RepairOptions, str], str],
    absolute_path: Callable[[str, str, str], str],
    optional_id: Callable[[RepairOptions, str, str], str | None],
    default_database: Callable[[str], str],
) -> MoveRepairCommandRequest:
    """Parse the closed move-repair grammar without state access.

    Parameters: ``arguments`` begins with ``repair-move`` and callbacks retain
    the top-level CLI's shared usage and value validation. Returns a concrete
    request or raises the supplied usage exception for malformed options. The
    parser does not open SQLite, inspect Git, or read streams.
    """
    command = "repair-move"
    allowed = {
        "project-id",
        "source-project-dir",
        "target-project-dir",
        "db",
        "yes",
        "application-timeout-seconds",
    }
    required = {"project-id", "source-project-dir", "target-project-dir"}
    options: RepairOptions = {}
    position = 1
    while position < len(arguments):
        token = arguments[position]
        if not token.startswith("--"):
            raise usage_error(command, "Unexpected positional argument.")
        name = token[2:]
        if name not in allowed:
            raise usage_error(command, "Unsupported command option.")
        if name in options:
            raise usage_error(command, "Command options may not be repeated.")
        if name == "yes":
            options[name] = True
            position += 1
            continue
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            raise usage_error(command, "A command option is missing its value.")
        options[name] = arguments[position + 1]
        position += 2
    if missing := required - set(options):
        raise usage_error(command, f"Missing required option --{sorted(missing)[0]}.")
    project_id = optional_id(options, "project-id", command)
    assert project_id is not None
    source = absolute_path(option(options, "source-project-dir"), command, "source-project-dir")
    target = absolute_path(option(options, "target-project-dir"), command, "target-project-dir")
    database = (
        absolute_path(option(options, "db"), command, "db")
        if "db" in options
        else default_database(command)
    )
    timeout = WRITER_TRANSACTION_TIMEOUT_SECONDS
    if "application-timeout-seconds" in options:
        try:
            timeout = float(option(options, "application-timeout-seconds"))
        except ValueError as error:
            raise usage_error(
                command, "--application-timeout-seconds requires finite positive seconds."
            ) from error
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_WRITER_TRANSACTION_TIMEOUT_SECONDS:
            raise usage_error(
                command, "--application-timeout-seconds is outside supported finite bounds."
            )
    return MoveRepairCommandRequest(
        database,
        project_id,
        source,
        target,
        bool(options.get("yes")),
        timeout,
    )


def execute_move_repair(
    request: MoveRepairCommandRequest,
    *,
    stdin: object,
    stdout: object,
    stderr: object,
) -> int:
    """Plan, preview, authorize, and atomically apply one move repair.

    Parameters: ``request`` is grammar validated and streams are supplied by the
    top-level CLI. Returns zero after commit, exit 4 for safety refusals or
    cancellation, and exit 5 for bounded operational or output-delivery
    failures. Preview and confirmation are stdout; diagnostics are stderr. The
    function never changes checkout content.
    """
    try:
        reviewed = plan_move_repair(
            MoveRepairRequest(
                request.database,
                request.project_id,
                request.source_project_dir,
                request.target_project_dir,
            )
        )
        _render_move_repair_preview(reviewed, stdout)
        _flush(stdout)
        if not request.yes:
            if not _is_terminal(stdin) or not _is_terminal(stdout):
                _write(
                    stderr,
                    "opencode-db: move repair requires terminal stdin and stdout unless --yes; "
                    "no changes were applied\n",
                )
                return EXIT_PRECONDITION_REFUSED
            _write(stdout, "Apply move repair? [y/N] ")
            _flush(stdout)
            try:
                response = stdin.readline()
            except KeyboardInterrupt:
                response = ""
            if response.removesuffix("\n").removesuffix("\r") != "y":
                _write(stderr, "opencode-db: move repair cancelled; no changes were applied\n")
                return EXIT_PRECONDITION_REFUSED
        apply_move_repair(
            reviewed,
            application_timeout_seconds=request.application_timeout_seconds,
        )
        _write(stdout, "opencode-db: move repair committed\n")
        return 0
    except MoveOperationalError as error:
        _safe_diagnostic(stderr, str(error))
        return EXIT_OPERATIONAL_FAILURE
    except MoveError as error:
        _safe_diagnostic(stderr, str(error))
        return EXIT_PRECONDITION_REFUSED
    except (OSError, ValueError, TypeError):
        _safe_diagnostic(stderr, "move repair output could not be delivered")
        return EXIT_OPERATIONAL_FAILURE


def _render_move_repair_preview(reviewed: ReviewedMoveRepairPlan, stdout: object) -> None:
    """Write deterministic source/target action groups for one reviewed plan."""
    for mapping in reviewed.mappings:
        actions = sorted({membership.action for membership in mapping.memberships})
        for action in actions:
            categories = Counter(
                membership.category
                for membership in mapping.memberships
                if membership.action == action
            )
            rendered = ",".join(
                f"{category}={categories[category]}" for category in sorted(categories)
            )
            _write(
                stdout,
                f"source={_quote_move_value(mapping.source)} "
                f"target={_quote_move_value(mapping.target)} "
                f"action={action} categories={rendered}\n",
            )


def _write(stream: object, value: str) -> None:
    """Require one complete text-stream write before mutation may continue."""
    written = stream.write(value)
    if written is not None and written != len(value):
        raise OSError("short write")


def _flush(stream: object) -> None:
    """Require preview or prompt delivery through the selected output stream."""
    stream.flush()


def _safe_diagnostic(stderr: object, message: str) -> None:
    """Best-effort one bounded diagnostic after a refused or failed repair."""
    try:
        _write(stderr, f"opencode-db: {message[:512]}\n")
    except (OSError, ValueError, TypeError):
        pass
