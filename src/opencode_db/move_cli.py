"""Implement the human-only ``mv`` command around the sibling move domain.

This internal boundary owns move-specific parsing, preview rendering,
confirmation, and progress while accepting shared CLI validation callbacks.
It neither imports nor dispatches the top-level CLI, preventing circular
dependencies and keeping generic command validation in :mod:`opencode_db.cli`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .model import EXIT_OPERATIONAL_FAILURE, EXIT_PRECONDITION_REFUSED
from .move import (
    MoveError,
    MoveOperationalError,
    MoveRequest,
    ReviewedMovePlan,
    apply_sibling_move,
    plan_sibling_move,
)

MoveOptions = dict[str, str | bool]
"""Parsed move option values before shared validation is applied."""


@dataclass(frozen=True)
class MoveCommandRequest:
    """Represent one fully parsed human-only sibling move command.

    ``database`` is an explicit or bounded-default absolute path, ``project_id``
    selects one stored project, and ``target_project_dir`` is its copied target
    main worktree. ``method`` is presently always ``sibling``; ``yes`` bypasses
    only terminal confirmation; and ``progress`` enables stderr-only progress.
    Constructing this value has no SQLite, Git, filesystem, or stream effects.
    """

    database: str
    project_id: str
    target_project_dir: str
    method: str
    yes: bool
    progress: bool

    @property
    def command(self) -> str:
        """Return the stable top-level command name without side effects."""
        return "mv"


def parse_move_command(
    arguments: list[str],
    *,
    usage_error: Callable[[str, str], Exception],
    option: Callable[[MoveOptions, str], str],
    absolute_path: Callable[[str, str, str], str],
    optional_id: Callable[[MoveOptions, str, str], str | None],
    default_database: Callable[[str], str],
) -> MoveCommandRequest:
    """Parse the closed sibling move grammar without executing it.

    Parameters: ``arguments`` begins with ``mv``. The callbacks retain the
    top-level CLI's shared error and value-validation contract. Returns a move
    request with a concrete explicit or bounded-default database path. Raises
    the supplied usage exception for missing, duplicate, unknown, relative, or
    unsupported options. This parser neither opens SQLite nor executes Git.
    """
    command = "mv"
    allowed = {"project-id", "target-project-dir", "db", "method", "yes", "progress"}
    required = {"project-id", "target-project-dir"}
    booleans = {"yes", "progress"}
    options: MoveOptions = {}
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
        if name in booleans:
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
    target_project_dir = absolute_path(
        option(options, "target-project-dir"), command, "target-project-dir"
    )
    method = option(options, "method") if "method" in options else "sibling"
    if method != "sibling":
        raise usage_error(command, "--method supports only sibling.")
    database = (
        absolute_path(option(options, "db"), command, "db")
        if "db" in options
        else default_database(command)
    )
    return MoveCommandRequest(
        database,
        project_id,
        target_project_dir,
        method,
        bool(options.get("yes")),
        bool(options.get("progress")),
    )


def execute_move(request: MoveCommandRequest, *, stdin: object, stdout: object, stderr: object) -> int:
    """Plan, preview, authorize, and atomically apply one sibling move.

    Parameters: ``request`` is the fully grammar-validated human-only move
    selection; ``stdin``, ``stdout``, and ``stderr`` are the top-level CLI
    streams. Returns zero only after the SQLite transaction commits, the safety
    precondition exit class for planner refusals, detached streams, or cancelled
    confirmation, and the operational exit class for bounded SQLite or Git
    failures. Mappings and confirmation are stdout; diagnostics and optional
    aggregate progress are stderr. This function never moves filesystem content.
    """
    reporter = _MoveProgressReporter(stderr) if request.progress else None
    callback: Callable[[str, int | None, int | None, bool], None] | None = (
        reporter.update if reporter is not None else None
    )
    try:
        reviewed = plan_sibling_move(
            MoveRequest(request.database, request.project_id, request.target_project_dir),
            progress=callback,
        )
        _render_move_preview(reviewed, stdout)
        if not request.yes:
            if not _is_terminal(stdin) or not _is_terminal(stdout):
                stderr.write(
                    "opencode-db: move requires terminal stdin and stdout unless --yes; no changes were applied\n"
                )
                return EXIT_PRECONDITION_REFUSED
            stdout.write("Apply sibling move? [y/N] ")
            stdout.flush()
            try:
                response = stdin.readline()
            except KeyboardInterrupt:
                response = ""
            if response.removesuffix("\n").removesuffix("\r") != "y":
                stderr.write("opencode-db: move cancelled; no changes were applied\n")
                return EXIT_PRECONDITION_REFUSED
        apply_sibling_move(reviewed, progress=callback)
    except MoveOperationalError as error:
        if reporter is not None:
            reporter.fail()
        stderr.write(f"opencode-db: {error}\n")
        return EXIT_OPERATIONAL_FAILURE
    except MoveError as error:
        if reporter is not None:
            reporter.fail()
        stderr.write(f"opencode-db: {error}\n")
        return EXIT_PRECONDITION_REFUSED
    finally:
        if reporter is not None:
            reporter.close()
    stdout.write("opencode-db: move committed\n")
    return 0


def _render_move_preview(reviewed: ReviewedMovePlan, stdout: object) -> None:
    """Write one deterministic complete sibling mapping preview to stdout.

    Parameters: ``reviewed`` is an immutable, fully validated move plan and
    ``stdout`` is the selected human-output stream. Each source is already
    ordered lexically by the planner. The function reports only quoted
    ASCII-escaped paths and aggregate structured category counts; it does not
    expose project IDs, Git evidence, or database content.
    """
    for mapping in reviewed.mappings:
        categories = Counter(membership.category for membership in mapping.memberships)
        rendered_categories = ",".join(
            f"{category}={categories[category]}" for category in sorted(categories)
        )
        stdout.write(
            f"source={_quote_move_value(mapping.source)} target={_quote_move_value(mapping.target)} "
            f"categories={rendered_categories}\n"
        )


def _quote_move_value(value: str) -> str:
    """Return one single-quoted, single-line ASCII-safe public path value."""
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == "'":
            escaped.append("\\'")
        elif 32 <= codepoint <= 126:
            escaped.append(character)
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "'" + "".join(escaped) + "'"


def _is_terminal(stream: object) -> bool:
    """Return terminal capability without assuming a stream exposes ``isatty``."""
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except OSError:
        return False


class _MoveProgressReporter:
    """Render bounded aggregate move progress without disclosing move evidence.

    Terminal streams redraw one ASCII line per active phase and finish each phase
    with a newline. Redirected streams receive capped newline-delimited records
    without carriage returns. The reporter accepts only fixed phase labels and
    aggregate completed/total values supplied by the move domain.
    """

    _MAX_EMISSIONS = 100

    def __init__(self, stream: object) -> None:
        """Initialize a progress renderer for one stderr stream without output."""
        self._stream = stream
        self._terminal = _is_terminal(stream)
        self._phase: str | None = None
        self._emissions = 0
        self._finished = True

    def update(
        self, phase: str, completed: int | None, total: int | None, complete: bool
    ) -> None:
        """Render one bounded phase transition or aggregate progress observation."""
        if phase != self._phase:
            self.close()
            self._phase = phase
            self._emissions = 0
            self._finished = False
            self._emit(phase, completed, total, "started")
        state = "complete" if complete else "progress"
        self._emit(phase, completed, total, state, force=complete)
        if complete:
            self._finished = True
            if self._terminal:
                self._stream.write("\n")

    def fail(self) -> None:
        """Finish the active phase with one failure record after a move exception."""
        if self._phase is not None and not self._finished:
            self._emit(self._phase, None, None, "failed", force=True)
            self._finished = True
            if self._terminal:
                self._stream.write("\n")

    def close(self) -> None:
        """Terminate an unfinished terminal redraw before changing phase or returning."""
        if self._terminal and self._phase is not None and not self._finished:
            self._stream.write("\n")
        self._phase = None
        self._finished = True

    def _emit(
        self,
        phase: str,
        completed: int | None,
        total: int | None,
        state: str,
        *,
        force: bool = False,
    ) -> None:
        """Write one capped fixed-label progress message to the selected stderr stream."""
        limit = self._MAX_EMISSIONS if force else self._MAX_EMISSIONS - 1
        if self._emissions >= limit:
            return
        self._emissions += 1
        counts = "" if completed is None or total is None else f" {completed}/{total}"
        if self._terminal:
            bar = ""
            if total is not None and total > 0 and completed is not None:
                width = 20
                filled = min(width, max(0, completed) * width // total)
                bar = " [" + "#" * filled + "-" * (width - filled) + "]"
            self._stream.write(f"\rprogress: {phase}{counts}{bar} {state}")
        else:
            self._stream.write(f"progress: {phase}{counts} {state}\n")
