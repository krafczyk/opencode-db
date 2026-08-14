"""Plan fail-closed, metadata-only sibling project moves without mutation.

The public planner reads one coherent SQLite snapshot, validates a closed set of
structured project locations, checks an operator-copied sibling family locally,
and returns immutable evidence for a later mutation unit.  It never updates
SQLite, creates files, changes filesystem content, or contacts Git remotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
from collections.abc import Callable

from . import move_git, move_schema


MAX_SCHEMA_OBJECTS = 1_024
"""Maximum schema objects admitted to one reviewed sibling move plan."""

MAX_SELECTED_ROWS = 250_000
"""Maximum selected structured location rows admitted to one reviewed plan."""

MAX_LOCATIONS = 16_384
"""Maximum distinct non-null structured locations admitted to one reviewed plan."""

MAX_SANDBOX_ENTRIES = 16_384
"""Maximum project sandbox entries admitted to one reviewed plan."""

MAX_VALUE_BYTES = 16 * 1024
"""Maximum UTF-8 byte length of a captured identifier or path."""

MAX_SANDBOX_BYTES = 16 * 1024 * 1024
"""Maximum UTF-8 byte length of the project sandbox JSON value."""

MAX_CAPTURE_BYTES = 64 * 1024 * 1024
"""Maximum aggregate UTF-8 byte length of captured structured scalar values."""

MAX_SCHEMA_FINGERPRINT_BYTES = 64 * 1024 * 1024
"""Maximum aggregate scalar bytes retained in one schema fingerprint."""

MAX_GIT_OUTPUT_BYTES = 64 * 1024
"""Maximum stdout bytes accepted from one local Git probe."""

GIT_TIMEOUT_SECONDS = 2.0
"""Fixed wall-clock limit for one local Git probe."""

GIT_CLEANUP_TIMEOUT_SECONDS = 2.0
"""Fixed wall-clock limit for terminating and reaping one Git probe."""

SQLITE_BUSY_TIMEOUT_MS = 2_000
"""Maximum connection-local wait for the move application's writer lock."""

REVALIDATION_TIMEOUT_SECONDS = 10.0
"""Maximum wall-clock time allowed for application-time freshness validation."""

WRITER_TRANSACTION_TIMEOUT_SECONDS = 10.0
"""Maximum wall-clock time for all work after the writer transaction begins."""

SQLITE_PROGRESS_OPCODES = 1_000
"""SQLite virtual-machine instructions between transaction deadline checks."""

_ROW_LOCATION_CATEGORIES = {
    "project_directory": "project_directory.directory",
    "session": "session.directory",
    "workspace": "workspace.directory",
}


class MoveError(RuntimeError):
    """Report a bounded sibling-move safety refusal without mutable side effects.

    Parameters: ``message`` is a fixed diagnostic assembled from validated
    evidence.  Construction truncates it to a bounded size and never exposes
    SQLite exception text, Git stderr, credentials, or arbitrary row content.
    """

    def __init__(self, message: str) -> None:
        """Store one bounded, content-free public move diagnostic."""
        super().__init__(message[:512])


class MoveOperationalError(MoveError):
    """Report a bounded SQLite, filesystem, or Git execution failure.

    This subtype distinguishes unavailable or interrupted local operations from
    validation refusals.  It adds no state and does not mutate SQLite or the
    filesystem.
    """


@dataclass(frozen=True)
class MoveRequest:
    """Select one project and copied target main worktree for read-only planning.

    ``database`` is an absolute existing SQLite path checked by planning and
    application; ``project_id`` identifies exactly one project; and
    ``target_project_dir`` is the absolute lexical target main worktree path.
    Constructing this immutable value performs no filesystem, SQLite, or Git I/O.
    """

    database: str | Path
    project_id: str
    target_project_dir: str


@dataclass(frozen=True)
class LocationMembership:
    """Identify one structured metadata owner of a validated source location.

    ``category`` names the closed location field and ``row_identity`` preserves
    the owning row's stable primary-key values for later freshness checks.  The
    value is immutable and has no side effects.
    """

    category: str
    row_identity: tuple[str, ...]


@dataclass(frozen=True)
class MoveMapping:
    """Describe one validated lexical source-to-target sibling mapping.

    ``source`` and ``target`` are exact absolute lexical paths, and
    ``memberships`` records every structured field that owns ``source``.  The
    ``source_git`` and ``target_git`` are :class:`GitEvidence` values that bind
    the corresponding local checkout identity, state, commit, and Git-admin
    paths. The mapping is immutable, sorted by source path in a reviewed plan,
    and does not itself read or alter SQLite or the filesystem.
    """

    source: str
    target: str
    memberships: tuple[LocationMembership, ...]
    source_git: "GitEvidence"
    target_git: "GitEvidence"

    @property
    def categories(self) -> tuple[str, ...]:
        """Return stable structured category names without performing I/O."""
        return tuple(membership.category for membership in self.memberships)


@dataclass(frozen=True)
class CapturedRow:
    """Preserve one selected structured row for later freshness comparison.

    ``table`` is one supported table, ``identity`` is its stable row key,
    ``directory`` is its original nullable or non-null location value, and
    ``payload`` binds non-key project-directory values across preview and apply.
    The payload is omitted from representations so reviewed state does not
    render database content. This immutable record has no side effects.
    """

    table: str
    identity: tuple[str, ...]
    directory: str | None
    payload: tuple[object, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class CapturedState:
    """Hold one coherent database snapshot captured by the read-only planner.

    ``schema_fingerprint`` preserves all non-system schema objects; ``project``
    stores the selected project's location-bearing scalars; and ``rows`` retains
    every selected project-directory, session, and workspace row, including null
    workspace directories. The value is immutable evidence used to reject a
    changed preview before any location updates.
    """

    schema_fingerprint: tuple[tuple[str, str, str, str | None], ...]
    project_id: str
    worktree: str
    sandboxes: str
    rows: tuple[CapturedRow, ...]


@dataclass(frozen=True)
class GitEvidence:
    """Record credential-free local Git correspondence evidence for one checkout.

    ``project_identity`` is a digest of a normalized non-file origin or a root
    commit fallback, ``checkout_state`` is ``attached`` or ``detached``,
    ``branch`` is present only when attached, and ``head`` is the exact checked
    out commit. ``git_dir`` and ``git_common_dir`` are bounded absolute paths
    emitted by Git for the checkout's administrative metadata; they detect
    copied linked worktrees that still point into a source family. Values are
    immutable, bounded, local-only evidence and do not mutate Git state.
    """

    project_identity: str
    checkout_state: str
    branch: str | None
    head: str
    git_dir: str
    git_common_dir: str


@dataclass(frozen=True)
class ReviewedMovePlan:
    """Represent one complete immutable sibling-move decision ready for review.

    ``request`` identifies the operator selection, ``captured_state`` binds the
    coherent SQLite snapshot, and ``mappings`` contains every validated distinct
    structured source in deterministic order. It authorizes no work itself;
    callers pass it to :func:`apply_sibling_move` only after confirmation.
    """

    request: MoveRequest
    captured_state: CapturedState
    mappings: tuple[MoveMapping, ...]


def plan_sibling_move(
    request: MoveRequest,
    *,
    progress: Callable[[str, int | None, int | None, bool], None] | None = None,
) -> ReviewedMovePlan:
    """Build one read-only, fail-closed sibling move plan from a SQLite snapshot.

    Parameters: ``request`` supplies an absolute existing database path, one
    nonempty project ID, and an absolute target main worktree path.  Returns an
    immutable :class:`ReviewedMovePlan` only when the current closed schema,
    exact selected rows, flat lexical family, existing source/target directories,
    and local Git correspondence all validate. ``progress``, when supplied,
    receives fixed phase names and aggregate completed/total observations; it
    cannot alter planning. Raises :class:`MoveError` for
    malformed data or ineligible layouts and :class:`MoveOperationalError` for
    SQLite, filesystem, Git availability, cap, timeout, or interruption failures.
    It opens SQLite read-only in exactly one explicit read transaction and never
    writes rows, changes filesystem content, or contacts a remote.
    """
    database = _existing_database(request.database)
    target_main = _absolute_value(request.target_project_dir, "target project directory")
    project_id = _identifier(request.project_id, "project ID")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, isolation_level=None)
        connection.execute("BEGIN")
        fingerprint = _validate_schema(connection)
        project, rows = _capture_project(connection, project_id, progress=progress)
        state = CapturedState(fingerprint, project_id, project["worktree"], project["sandboxes"], rows)
        mappings = _build_mappings(
            state,
            target_main,
            deadline=time.monotonic() + REVALIDATION_TIMEOUT_SECONDS,
            progress=progress,
        )
        return ReviewedMovePlan(request, state, mappings)
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise MoveOperationalError("move planning could not read the database") from error
    finally:
        _cleanup_connection(connection, "planning", sys.exception())


def apply_sibling_move(
    reviewed: ReviewedMovePlan,
    *,
    progress: Callable[[str, int | None, int | None, bool], None] | None = None,
) -> None:
    """Atomically apply one previously reviewed sibling move plan.

    Parameters: ``reviewed`` is an immutable :class:`ReviewedMovePlan` returned
    by :func:`plan_sibling_move` for an existing absolute SQLite database. Returns
    ``None`` only after the exact structured-location transaction commits. Raises
    :class:`MoveError` when the reviewed state, schema, directory, Git evidence,
    target project-directory keys, integrity, or foreign keys have changed or are
    invalid; ``progress``, when supplied, receives fixed revalidation and update
    group aggregate observations without affecting the transaction. Raises
    :class:`MoveOperationalError` if SQLite cannot acquire its
    bounded writer lock or perform the transaction. The function writes only the
    selected project's worktree, sandbox JSON, project-directory keys, session
    directories, and non-null workspace directories. It neither rewrites
    historical/free-form data nor changes filesystem or Git content.
    """
    if not isinstance(reviewed, ReviewedMovePlan):
        raise MoveError("move reviewed plan is malformed")
    database = _existing_database(reviewed.request.database)
    connection: sqlite3.Connection | None = None
    deadline: float | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise MoveOperationalError("move SQLite foreign keys could not be enabled")
        connection.execute("BEGIN IMMEDIATE")
        deadline = time.monotonic() + WRITER_TRANSACTION_TIMEOUT_SECONDS
        _install_transaction_deadline(connection, deadline)
        _check_transaction_deadline(deadline)
        _validate_database_health(connection, deadline)
        _report_progress(progress, "revalidation", 0, 1, False)
        current, mappings = _revalidate_reviewed_plan(connection, reviewed, deadline)
        _report_progress(progress, "revalidation", 1, 1, True)
        targets = _mapping_targets(mappings, deadline)
        _report_progress(progress, "update groups", 0, 5, False)
        _apply_project_locations(connection, current, targets, deadline)
        _after_move_update_group("project")
        _check_transaction_deadline(deadline)
        _report_progress(progress, "update groups", 1, 5, False)
        _apply_project_directory_locations(connection, current, targets, deadline)
        _after_move_update_group("project_directory")
        _check_transaction_deadline(deadline)
        _report_progress(progress, "update groups", 2, 5, False)
        _apply_row_locations(connection, current, targets, "session", deadline)
        _after_move_update_group("session")
        _check_transaction_deadline(deadline)
        _report_progress(progress, "update groups", 3, 5, False)
        _apply_row_locations(connection, current, targets, "workspace", deadline)
        _after_move_update_group("workspace")
        _check_transaction_deadline(deadline)
        _report_progress(progress, "update groups", 4, 5, False)
        _verify_applied_locations(connection, current, targets, deadline)
        _validate_database_health(connection, deadline)
        _after_move_update_group("pre_commit")
        _check_transaction_deadline(deadline)
        connection.commit()
        _report_progress(progress, "update groups", 5, 5, True)
    except MoveError:
        raise
    except sqlite3.Error as error:
        if deadline is not None and time.monotonic() >= deadline:
            raise MoveOperationalError("move application timed out") from error
        raise MoveOperationalError("move application could not complete") from error
    except (OSError, ValueError, TypeError) as error:
        raise MoveOperationalError("move application could not complete") from error
    finally:
        _cleanup_connection(connection, "application", sys.exception())


def _cleanup_connection(
    connection: sqlite3.Connection | None, phase: str, original: BaseException | None
) -> None:
    """Confirm rollback and close without leaking raw SQLite cleanup failures."""
    if connection is None:
        return
    failure: sqlite3.Error | None = None
    rollback_failed = False
    try:
        connection.set_progress_handler(None, 0)
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error as error:
        failure = error
        rollback_failed = True
    try:
        connection.close()
    except sqlite3.Error as error:
        if failure is None:
            failure = error
    if failure is None:
        return
    message = (
        f"move {phase} rollback could not be confirmed"
        if rollback_failed
        else f"move {phase} cleanup could not be confirmed"
    )
    operational = MoveOperationalError(message)
    if original is not None:
        raise operational from original
    raise operational from failure


def _report_progress(
    progress: Callable[[str, int | None, int | None, bool], None] | None,
    phase: str,
    completed: int | None,
    total: int | None,
    complete: bool,
) -> None:
    """Send one aggregate progress observation when a caller opted in."""
    if progress is not None:
        try:
            progress(phase, completed, total, complete)
        except Exception:
            pass


def _install_transaction_deadline(connection: sqlite3.Connection, deadline: float) -> None:
    """Interrupt SQLite virtual-machine work after the writer deadline expires."""
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), SQLITE_PROGRESS_OPCODES)


def _check_transaction_deadline(deadline: float) -> None:
    """Fail operationally when the bounded writer transaction deadline expires."""
    if time.monotonic() >= deadline:
        raise MoveOperationalError("move application timed out")


def _validate_database_health(connection: sqlite3.Connection, deadline: float) -> None:
    """Require intact SQLite pages and no foreign-key violations before commit."""
    integrity = connection.execute("PRAGMA integrity_check")
    first_integrity = next(integrity, None)
    _check_transaction_deadline(deadline)
    if first_integrity != ("ok",) or next(integrity, None) is not None:
        raise MoveError("move database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MoveError("move database foreign key check failed")
    _check_transaction_deadline(deadline)


def _revalidate_reviewed_plan(
    connection: sqlite3.Connection, reviewed: ReviewedMovePlan, deadline: float
) -> tuple[CapturedState, tuple[MoveMapping, ...]]:
    """Collect and compare complete current evidence under the writer lock."""
    if reviewed.request.project_id != reviewed.captured_state.project_id:
        raise MoveError("move reviewed plan is malformed")
    fingerprint = _validate_schema(connection, deadline=deadline)
    _check_transaction_deadline(deadline)
    project, rows = _capture_project(connection, reviewed.captured_state.project_id, deadline=deadline)
    current = CapturedState(
        fingerprint,
        reviewed.captured_state.project_id,
        project["worktree"],
        project["sandboxes"],
        rows,
    )
    if current != reviewed.captured_state:
        raise MoveError("move preview is stale: selected database state changed")
    _check_transaction_deadline(deadline)
    target_main = _absolute_value(reviewed.request.target_project_dir, "target project directory")
    mappings = _build_mappings(
        current,
        target_main,
        deadline=deadline,
        deadline_check=_check_transaction_deadline,
    )
    if mappings != reviewed.mappings:
        raise MoveError("move preview is stale: directory or Git evidence changed")
    _check_transaction_deadline(deadline)
    return current, mappings


def _check_revalidation_deadline(deadline: float) -> None:
    """Fail operationally when complete application-time validation exceeds its bound."""
    if time.monotonic() > deadline:
        raise MoveOperationalError("move revalidation timed out")


def _mapping_targets(
    mappings: tuple[MoveMapping, ...], deadline: float
) -> dict[tuple[str, tuple[str, ...]], str]:
    """Index every exact selected structured owner by immutable membership key."""
    targets: dict[tuple[str, tuple[str, ...]], str] = {}
    for mapping in mappings:
        _check_transaction_deadline(deadline)
        for membership in mapping.memberships:
            _check_transaction_deadline(deadline)
            key = (membership.category, membership.row_identity)
            if key in targets:
                raise MoveError("move reviewed memberships are malformed")
            targets[key] = mapping.target
    return targets


def _apply_project_locations(
    connection: sqlite3.Connection,
    state: CapturedState,
    targets: dict[tuple[str, tuple[str, ...]], str],
    deadline: float,
) -> None:
    """Update the selected project's exact worktree and sandbox JSON together."""
    worktree = targets.get(("project.worktree", (state.project_id,)))
    if worktree is None:
        raise MoveError("move reviewed worktree membership is missing")
    sandboxes = json.loads(state.sandboxes)
    rewritten: list[str] = []
    for index, _sandbox in enumerate(sandboxes):
        _check_transaction_deadline(deadline)
        target = targets.get(("project.sandbox", (state.project_id, str(index))))
        if target is None:
            raise MoveError("move reviewed sandbox membership is missing")
        rewritten.append(target)
    result = connection.execute(
        "UPDATE project SET worktree = ?, sandboxes = ? "
        "WHERE id = ? AND worktree = ? AND sandboxes = ?",
        (worktree, json.dumps(rewritten), state.project_id, state.worktree, state.sandboxes),
    )
    if result.rowcount != 1:
        raise MoveError("move project update affected an unexpected row count")
    _check_transaction_deadline(deadline)


def _apply_project_directory_locations(
    connection: sqlite3.Connection,
    state: CapturedState,
    targets: dict[tuple[str, tuple[str, ...]], str],
    deadline: float,
) -> None:
    """Transition every original project-directory key only into a vacant target key."""
    for row in state.rows:
        _check_transaction_deadline(deadline)
        if row.table != "project_directory":
            continue
        target = targets.get(("project_directory.directory", row.identity))
        if target is None or row.directory is None or len(row.identity) != 2:
            raise MoveError("move reviewed project-directory membership is malformed")
        if connection.execute(
            "SELECT 1 FROM project_directory WHERE project_id = ? AND directory = ?",
            (row.identity[0], target),
        ).fetchone() is not None:
            raise MoveError("move project-directory target key is occupied")
        if len(row.payload) != 3:
            raise MoveError("move reviewed project-directory payload is malformed")
        result = connection.execute(
            "UPDATE project_directory SET directory = ? "
            "WHERE project_id = ? AND directory = ? AND type IS ? "
            "AND strategy IS ? AND time_created IS ?",
            (target, row.identity[0], row.directory, *row.payload),
        )
        if result.rowcount != 1:
            raise MoveError("move project-directory update affected an unexpected row count")


def _apply_row_locations(
    connection: sqlite3.Connection,
    state: CapturedState,
    targets: dict[tuple[str, tuple[str, ...]], str],
    table: str,
    deadline: float,
) -> None:
    """Update exact selected session or non-null workspace directory rows."""
    if table not in {"session", "workspace"}:
        raise MoveError("move location table is unsupported")
    for row in state.rows:
        _check_transaction_deadline(deadline)
        if row.table != table or row.directory is None:
            continue
        target = targets.get((_ROW_LOCATION_CATEGORIES[table], row.identity))
        if target is None or len(row.identity) != 1:
            raise MoveError("move reviewed row membership is malformed")
        result = connection.execute(
            f"UPDATE {_quote(table)} SET directory = ? WHERE id = ? AND project_id = ? AND directory = ?",
            (target, row.identity[0], state.project_id, row.directory),
        )
        if result.rowcount != 1:
            raise MoveError(f"move {table} update affected an unexpected row count")


def _verify_applied_locations(
    connection: sqlite3.Connection,
    state: CapturedState,
    targets: dict[tuple[str, tuple[str, ...]], str],
    deadline: float,
) -> None:
    """Confirm selected row membership, exact targets, and null workspaces after updates."""
    project = connection.execute("SELECT worktree, sandboxes FROM project WHERE id = ?", (state.project_id,))
    expected_sandboxes: list[str] = []
    for index, _sandbox in enumerate(json.loads(state.sandboxes)):
        _check_transaction_deadline(deadline)
        expected_sandboxes.append(targets[("project.sandbox", (state.project_id, str(index)))])
    expected_project = (targets[("project.worktree", (state.project_id,))], json.dumps(expected_sandboxes))
    if next(project, None) != expected_project or next(project, None) is not None:
        raise MoveError("move project post-update validation failed")
    for table in _ROW_LOCATION_CATEGORIES:
        category = _ROW_LOCATION_CATEGORIES[table]
        expected: list[CapturedRow] = []
        for row in state.rows:
            _check_transaction_deadline(deadline)
            if row.table == table:
                expected.append(row)
        columns = (
            "project_id, directory, type, strategy, time_created"
            if table == "project_directory"
            else "id, project_id, directory"
        )
        order = "project_id, directory" if table == "project_directory" else "id"
        observed = iter(
            connection.execute(
                f"SELECT {columns} FROM {_quote(table)} WHERE project_id = ? ORDER BY {order}",
                (state.project_id,),
            )
        )
        for row in expected:
            _check_transaction_deadline(deadline)
            directory = targets[(category, row.identity)] if row.directory is not None else None
            if table == "project_directory":
                if len(row.payload) != 3:
                    raise MoveError("move reviewed project-directory payload is malformed")
                expected_row = (state.project_id, directory, *row.payload)
            else:
                expected_row = (row.identity[0], state.project_id, directory)
            if next(observed, None) != expected_row:
                raise MoveError(f"move {table} post-update validation failed")
        if next(observed, None) is not None:
            raise MoveError(f"move {table} row count changed")
        _check_transaction_deadline(deadline)


def _after_move_update_group(group: str) -> None:
    """Provide a no-op deterministic test seam after a named mutation group."""
    del group


def _existing_database(value: str | Path) -> Path:
    """Return an existing absolute regular SQLite path without creating anything."""
    path = Path(value)
    if "\x00" in os.fspath(path) or not path.is_absolute():
        raise MoveError("database must be an absolute existing regular file")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise MoveError("database must be an absolute existing regular file") from error
    except OSError as error:
        raise MoveOperationalError("database could not be inspected") from error
    if not stat.S_ISREG(mode):
        raise MoveError("database must be an absolute existing regular file")
    return path


def _validate_schema(
    connection: sqlite3.Connection, *, deadline: float | None = None
) -> tuple[tuple[str, str, str, str | None], ...]:
    """Require the bounded closed location schema and return its stable fingerprint."""
    return move_schema.validate_schema(
        connection,
        move_error=MoveError,
        quote=_quote,
        max_schema_objects=MAX_SCHEMA_OBJECTS,
        max_fingerprint_bytes=MAX_SCHEMA_FINGERPRINT_BYTES,
        max_scalar_bytes=MAX_VALUE_BYTES,
        check_deadline=lambda: _check_optional_deadline(deadline),
    )


def _capture_project(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    progress: Callable[[str, int | None, int | None, bool], None] | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, str], tuple[CapturedRow, ...]]:
    """Capture exactly one selected project and every supported location-bearing row.

    ``progress`` receives only aggregate collection counts after their bounded
    totals are known. It does not change the snapshot queries or captured state.
    """
    row_tables = ("project_directory", "session", "workspace")
    total: int | None = None
    if progress is not None:
        total = 1
        for table in row_tables:
            _check_optional_deadline(deadline)
            count = connection.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE project_id = ?", (project_id,)
            ).fetchone()
            if count is None or len(count) != 1 or type(count[0]) is not int:
                raise MoveError("move selected row count is malformed")
            total += count[0]
            if total > MAX_SELECTED_ROWS:
                raise MoveError("move selected row limit exceeded")
        _report_progress(progress, "collection", 0, total, False)
    projects = connection.execute(
        "SELECT id, worktree, sandboxes FROM project WHERE id = ? LIMIT 2", (project_id,)
    )
    project_row = next(projects, None)
    if project_row is None:
        raise MoveError("project ID was not found")
    if next(projects, None) is not None:
        raise MoveError("project ID is ambiguous")
    if len(project_row) != 3:
        raise MoveError("move project row is malformed")
    project = {
        "id": _identifier(project_row[0], "project ID"),
        "worktree": _absolute_value(project_row[1], "project worktree"),
        "sandboxes": _sandbox_text(project_row[2], deadline=deadline),
    }
    captured_bytes = sum(_captured_scalar_size(value) for value in project.values())
    if captured_bytes > MAX_CAPTURE_BYTES:
        raise MoveError("move captured scalar limit exceeded")
    rows: list[CapturedRow] = []
    completed = 1
    for table, identity_columns, nullable, payload_columns in (
        ("project_directory", ("project_id", "directory"), False, ("type", "strategy", "time_created")),
        ("session", ("id",), False, ()),
        ("workspace", ("id",), True, ()),
    ):
        selected = ", ".join(
            _quote(column) for column in (*identity_columns, "directory", *payload_columns)
        )
        order = ", ".join(_quote(column) for column in identity_columns)
        records = connection.execute(
            f"SELECT {selected} FROM {_quote(table)} WHERE project_id = ? "
            f"ORDER BY {order} LIMIT {MAX_SELECTED_ROWS + 1}",
            (project_id,),
        )
        while batch := records.fetchmany(256):
            _check_optional_deadline(deadline)
            for record in batch:
                if completed >= MAX_SELECTED_ROWS:
                    raise MoveError("move selected row limit exceeded")
                if len(record) != len(identity_columns) + 1 + len(payload_columns):
                    raise MoveError("move location row is malformed")
                identity = tuple(
                    _identifier(value, f"{table} row identity")
                    for value in record[: len(identity_columns)]
                )
                directory_value = record[len(identity_columns)]
                directory = (
                    None
                    if directory_value is None and nullable
                    else _absolute_value(directory_value, f"{table} directory")
                )
                payload = tuple(record[len(identity_columns) + 1 :])
                candidate_bytes = sum(_captured_scalar_size(value) for value in identity)
                if directory is not None:
                    candidate_bytes += _captured_scalar_size(directory)
                candidate_bytes += sum(_captured_scalar_size(value) for value in payload)
                if captured_bytes + candidate_bytes > MAX_CAPTURE_BYTES:
                    raise MoveError("move captured scalar limit exceeded")
                captured_bytes += candidate_bytes
                rows.append(CapturedRow(table, identity, directory, payload))
                completed += 1
                _report_progress(progress, "collection", completed, total, False)
                _check_optional_deadline(deadline)
    _report_progress(progress, "collection", completed, total, True)
    return project, tuple(rows)


def _sandbox_text(value: object, *, deadline: float | None = None) -> str:
    """Validate bounded sandbox JSON without preserving mutable decoded objects."""
    if not isinstance(value, str):
        raise MoveError("move sandbox JSON is malformed")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_SANDBOX_BYTES:
        raise MoveError("move sandbox JSON limit exceeded")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise MoveError("move sandbox JSON is malformed") from error
    if not isinstance(decoded, list) or len(decoded) > MAX_SANDBOX_ENTRIES:
        raise MoveError("move sandbox JSON is malformed")
    for sandbox in decoded:
        _check_optional_deadline(deadline)
        _absolute_value(sandbox, "project sandbox")
    return value


def _captured_scalar_size(value: object) -> int:
    """Return a deterministic byte charge for one captured SQLite scalar."""
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    if isinstance(value, bytes):
        return len(value)
    if value is None:
        return 0
    if type(value) in (int, float):
        return 8
    raise MoveError("move captured scalar is malformed")


def _check_optional_deadline(deadline: float | None) -> None:
    """Check the writer deadline when a helper runs inside application."""
    if deadline is not None:
        _check_transaction_deadline(deadline)


def _build_mappings(
    state: CapturedState,
    target_main: str,
    *,
    deadline: float,
    progress: Callable[[str, int | None, int | None, bool], None] | None = None,
    deadline_check: Callable[[float], None] = _check_revalidation_deadline,
) -> tuple[MoveMapping, ...]:
    """Derive, validate, and evidence every distinct lexical sibling mapping."""
    source_parent = os.path.dirname(state.worktree)
    if not source_parent or os.path.basename(state.worktree) == "":
        raise MoveError("move source worktree layout is invalid")
    if os.path.basename(target_main) != os.path.basename(state.worktree):
        raise MoveError("move target basename does not match source worktree")
    target_parent = os.path.dirname(target_main)
    if target_parent == source_parent:
        raise MoveError("move target family has the same parent as source")
    locations: dict[str, list[LocationMembership]] = {}
    _add_location(locations, state.worktree, LocationMembership("project.worktree", (state.project_id,)))
    sandboxes = json.loads(state.sandboxes)
    for index, sandbox in enumerate(sandboxes):
        deadline_check(deadline)
        _add_location(locations, sandbox, LocationMembership("project.sandbox", (state.project_id, str(index))))
    for row in state.rows:
        if row.directory is not None:
            _add_location(
                locations,
                row.directory,
                LocationMembership(_ROW_LOCATION_CATEGORIES[row.table], row.identity),
            )
    if len(locations) > MAX_LOCATIONS:
        raise MoveError("move distinct location limit exceeded")
    mappings: list[MoveMapping] = []
    derived: dict[str, str] = {}
    total = len(locations)
    _report_progress(progress, "Git pair validation", 0, total, False)
    for source in sorted(locations):
        deadline_check(deadline)
        memberships = tuple(sorted(locations[source], key=lambda item: (item.category, item.row_identity)))
        if os.path.dirname(source) != source_parent or not os.path.basename(source):
            raise MoveError(
                f"move sibling layout is invalid: categories={_categories(memberships)} path={_display(source)}"
            )
        target = os.path.join(target_parent, os.path.basename(source))
        if source == target:
            raise MoveError(
                f"move source and target are identical: categories={_categories(memberships)} path={_display(source)}"
            )
        existing = derived.get(target)
        if existing is not None and existing != source:
            raise MoveError(
                f"move derived target conflicts: categories={_categories(memberships)} target={_display(target)}"
            )
        derived[target] = source
        _require_directory(source, "source", memberships)
        _require_directory(target, "target", memberships)
        source_git = _git_evidence(source, "source")
        deadline_check(deadline)
        target_git = _git_evidence(target, "target")
        deadline_check(deadline)
        _compare_git(source, target, source_git, target_git)
        mappings.append(MoveMapping(source, target, memberships, source_git, target_git))
        _report_progress(progress, "Git pair validation", len(mappings), total, len(mappings) == total)
    source_admin_paths = tuple(
        admin_path
        for mapping in mappings
        for admin_path in (mapping.source_git.git_dir, mapping.source_git.git_common_dir)
    )
    for mapping in mappings:
        deadline_check(deadline)
        for target_admin_path in (mapping.target_git.git_dir, mapping.target_git.git_common_dir):
            if any(_points_into(target_admin_path, source_admin_path) for source_admin_path in source_admin_paths):
                raise MoveError("git target administrative metadata points into the source family")
    return tuple(mappings)


def _add_location(
    locations: dict[str, list[LocationMembership]], path: object, membership: LocationMembership
) -> None:
    """Add one validated path membership while preserving duplicate owning rows."""
    value = _absolute_value(path, membership.category)
    locations.setdefault(value, []).append(membership)


def _require_directory(path: str, side: str, memberships: tuple[LocationMembership, ...]) -> None:
    """Require one existing directory without imposing inode, mode, owner, or symlink policy."""
    try:
        exists = Path(path).is_dir()
    except OSError as error:
        raise MoveOperationalError(f"move {side} directory could not be inspected") from error
    if not exists:
        raise MoveError(
            f"move {side} directory is missing: categories={_categories(memberships)} path={_display(path)}"
        )


def _git_evidence(path: str, side: str) -> GitEvidence:
    """Collect bounded local Git evidence through the dedicated subprocess boundary."""
    return move_git.collect_evidence(
        path,
        side,
        evidence_factory=GitEvidence,
        move_error=MoveError,
        operational_error=MoveOperationalError,
        display=_display,
        maximum_output_bytes=MAX_GIT_OUTPUT_BYTES,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        cleanup_timeout_seconds=GIT_CLEANUP_TIMEOUT_SECONDS,
    )


def _git_output(
    path: str, side: str, arguments: tuple[str, ...], label: str, *, allow_missing: bool = False
) -> str | None:
    """Run one bounded local Git probe through the internal Git module."""
    return move_git.git_output(
        path,
        side,
        arguments,
        label,
        allow_missing=allow_missing,
        move_error=MoveError,
        operational_error=MoveOperationalError,
        display=_display,
        maximum_output_bytes=MAX_GIT_OUTPUT_BYTES,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        cleanup_timeout_seconds=GIT_CLEANUP_TIMEOUT_SECONDS,
    )


def _points_into(path: str, parent: str) -> bool:
    """Return whether one normalized absolute Git-admin path is inside another."""
    return path == parent or path.startswith(parent + os.sep)


def _compare_git(source: str, target: str, source_git: GitEvidence, target_git: GitEvidence) -> None:
    """Require identical local project identity, checkout state, branch, and HEAD."""
    for dimension, expected, observed in (
        ("project identity", source_git.project_identity, target_git.project_identity),
        ("checkout state", source_git.checkout_state, target_git.checkout_state),
        ("branch", source_git.branch, target_git.branch),
        ("HEAD", source_git.head, target_git.head),
    ):
        if expected != observed:
            raise MoveError(
                f"git {dimension} mismatch: source={_display(source)} target={_display(target)} "
                f"expected={_display(str(expected))} observed={_display(str(observed))}"
            )


def _absolute_value(value: object, label: str) -> str:
    """Require one bounded nonempty absolute lexical path string."""
    if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
        raise MoveError(f"move {label} must be an absolute path")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_VALUE_BYTES:
        raise MoveError(f"move {label} value limit exceeded")
    return value


def _identifier(value: object, label: str) -> str:
    """Require one bounded nonempty text identifier without accepting SQLite coercions."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise MoveError(f"move {label} is malformed")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_VALUE_BYTES:
        raise MoveError(f"move {label} value limit exceeded")
    return value


def _categories(memberships: tuple[LocationMembership, ...]) -> str:
    """Render a deterministic non-content category list for a refusal."""
    return ",".join(membership.category for membership in memberships)


def _display(value: str) -> str:
    """Quote one single-line ASCII-escaped evidence value for public diagnostics."""
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif 32 <= codepoint <= 126:
            escaped.append(character)
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return '"' + "".join(escaped) + '"'


def _quote(identifier: str) -> str:
    """Quote a fixed schema identifier for SQLite metadata queries."""
    return '"' + identifier.replace('"', '""') + '"'
