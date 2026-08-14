"""Plan fail-closed, metadata-only sibling project moves without mutation.

The public planner reads one coherent SQLite snapshot, validates a closed set of
structured project locations, checks an operator-copied sibling family locally,
and returns immutable evidence for a later mutation unit.  It never updates
SQLite, creates files, changes filesystem content, or contacts Git remotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit


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

MAX_GIT_OUTPUT_BYTES = 64 * 1024
"""Maximum stdout bytes accepted from one local Git probe."""

GIT_TIMEOUT_SECONDS = 2.0
"""Fixed wall-clock limit for one local Git probe."""

SQLITE_BUSY_TIMEOUT_MS = 2_000
"""Maximum connection-local wait for the move application's writer lock."""

REVALIDATION_TIMEOUT_SECONDS = 10.0
"""Maximum wall-clock time allowed for application-time freshness validation."""


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
    mapping is immutable, sorted by source path in a reviewed plan, and does not
    itself read or alter SQLite or the filesystem.
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
    out commit.  Values are immutable, bounded, local-only evidence.
    """

    project_identity: str
    checkout_state: str
    branch: str | None
    head: str


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


def plan_sibling_move(request: MoveRequest) -> ReviewedMovePlan:
    """Build one read-only, fail-closed sibling move plan from a SQLite snapshot.

    Parameters: ``request`` supplies an absolute existing database path, one
    nonempty project ID, and an absolute target main worktree path.  Returns an
    immutable :class:`ReviewedMovePlan` only when the current closed schema,
    exact selected rows, flat lexical family, existing source/target directories,
    and local Git correspondence all validate.  Raises :class:`MoveError` for
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
        project, rows = _capture_project(connection, project_id)
        state = CapturedState(fingerprint, project_id, project["worktree"], project["sandboxes"], rows)
        mappings = _build_mappings(
            state, target_main, deadline=time.monotonic() + REVALIDATION_TIMEOUT_SECONDS
        )
        connection.rollback()
        return ReviewedMovePlan(request, state, mappings)
    except MoveError:
        if connection is not None:
            connection.rollback()
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        if connection is not None:
            connection.rollback()
        raise MoveOperationalError("move planning could not read the database") from error
    finally:
        if connection is not None:
            connection.close()


def apply_sibling_move(reviewed: ReviewedMovePlan) -> None:
    """Atomically apply one previously reviewed sibling move plan.

    Parameters: ``reviewed`` is an immutable :class:`ReviewedMovePlan` returned
    by :func:`plan_sibling_move` for an existing absolute SQLite database. Returns
    ``None`` only after the exact structured-location transaction commits. Raises
    :class:`MoveError` when the reviewed state, schema, directory, Git evidence,
    target project-directory keys, integrity, or foreign keys have changed or are
    invalid; raises :class:`MoveOperationalError` if SQLite cannot acquire its
    bounded writer lock or perform the transaction. The function writes only the
    selected project's worktree, sandbox JSON, project-directory keys, session
    directories, and non-null workspace directories. It neither rewrites
    historical/free-form data nor changes filesystem or Git content.
    """
    if not isinstance(reviewed, ReviewedMovePlan):
        raise MoveError("move reviewed plan is malformed")
    database = _existing_database(reviewed.request.database)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise MoveOperationalError("move SQLite foreign keys could not be enabled")
        connection.execute("BEGIN IMMEDIATE")
        _validate_database_health(connection)
        current, mappings = _revalidate_reviewed_plan(connection, reviewed)
        _apply_project_locations(connection, current, mappings)
        _after_move_update_group("project")
        _apply_project_directory_locations(connection, current, mappings)
        _after_move_update_group("project_directory")
        _apply_row_locations(connection, current, mappings, "session")
        _after_move_update_group("session")
        _apply_row_locations(connection, current, mappings, "workspace")
        _after_move_update_group("workspace")
        _verify_applied_locations(connection, current, mappings)
        _validate_database_health(connection)
        _after_move_update_group("pre_commit")
        connection.commit()
    except MoveError:
        _rollback(connection)
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        _rollback(connection)
        raise MoveOperationalError("move application could not complete") from error
    finally:
        _rollback(connection)
        if connection is not None:
            connection.close()


def _rollback(connection: sqlite3.Connection | None) -> None:
    """Roll back an active move transaction without obscuring its original failure."""
    if connection is not None and connection.in_transaction:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass


def _validate_database_health(connection: sqlite3.Connection) -> None:
    """Require intact SQLite pages and no foreign-key violations before commit."""
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise MoveError("move database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MoveError("move database foreign key check failed")


def _revalidate_reviewed_plan(
    connection: sqlite3.Connection, reviewed: ReviewedMovePlan
) -> tuple[CapturedState, tuple[MoveMapping, ...]]:
    """Collect and compare complete current evidence under the writer lock."""
    if reviewed.request.project_id != reviewed.captured_state.project_id:
        raise MoveError("move reviewed plan is malformed")
    deadline = time.monotonic() + REVALIDATION_TIMEOUT_SECONDS
    fingerprint = _validate_schema(connection)
    _check_revalidation_deadline(deadline)
    project, rows = _capture_project(connection, reviewed.captured_state.project_id)
    current = CapturedState(
        fingerprint,
        reviewed.captured_state.project_id,
        project["worktree"],
        project["sandboxes"],
        rows,
    )
    if current != reviewed.captured_state:
        raise MoveError("move preview is stale: selected database state changed")
    _check_revalidation_deadline(deadline)
    target_main = _absolute_value(reviewed.request.target_project_dir, "target project directory")
    mappings = _build_mappings(current, target_main, deadline=deadline)
    if mappings != reviewed.mappings:
        raise MoveError("move preview is stale: directory or Git evidence changed")
    _check_revalidation_deadline(deadline)
    return current, mappings


def _check_revalidation_deadline(deadline: float) -> None:
    """Fail operationally when complete application-time validation exceeds its bound."""
    if time.monotonic() > deadline:
        raise MoveOperationalError("move revalidation timed out")


def _mapping_targets(mappings: tuple[MoveMapping, ...]) -> dict[tuple[str, tuple[str, ...]], str]:
    """Index every exact selected structured owner by immutable membership key."""
    targets: dict[tuple[str, tuple[str, ...]], str] = {}
    for mapping in mappings:
        for membership in mapping.memberships:
            key = (membership.category, membership.row_identity)
            if key in targets:
                raise MoveError("move reviewed memberships are malformed")
            targets[key] = mapping.target
    return targets


def _apply_project_locations(
    connection: sqlite3.Connection, state: CapturedState, mappings: tuple[MoveMapping, ...]
) -> None:
    """Update the selected project's exact worktree and sandbox JSON together."""
    targets = _mapping_targets(mappings)
    worktree = targets.get(("project.worktree", (state.project_id,)))
    if worktree is None:
        raise MoveError("move reviewed worktree membership is missing")
    sandboxes = json.loads(state.sandboxes)
    rewritten: list[str] = []
    for index, _sandbox in enumerate(sandboxes):
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


def _apply_project_directory_locations(
    connection: sqlite3.Connection, state: CapturedState, mappings: tuple[MoveMapping, ...]
) -> None:
    """Transition every original project-directory key only into a vacant target key."""
    targets = _mapping_targets(mappings)
    for row in (item for item in state.rows if item.table == "project_directory"):
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
    mappings: tuple[MoveMapping, ...],
    table: str,
) -> None:
    """Update exact selected session or non-null workspace directory rows."""
    categories = {"session": "session.directory", "workspace": "workspace.directory"}
    if table not in categories:
        raise MoveError("move location table is unsupported")
    targets = _mapping_targets(mappings)
    for row in (item for item in state.rows if item.table == table and item.directory is not None):
        target = targets.get((categories[table], row.identity))
        if target is None or len(row.identity) != 1:
            raise MoveError("move reviewed row membership is malformed")
        result = connection.execute(
            f"UPDATE {_quote(table)} SET directory = ? WHERE id = ? AND project_id = ? AND directory = ?",
            (target, row.identity[0], state.project_id, row.directory),
        )
        if result.rowcount != 1:
            raise MoveError(f"move {table} update affected an unexpected row count")


def _verify_applied_locations(
    connection: sqlite3.Connection, state: CapturedState, mappings: tuple[MoveMapping, ...]
) -> None:
    """Confirm selected row membership, exact targets, and null workspaces after updates."""
    targets = _mapping_targets(mappings)
    project = connection.execute(
        "SELECT worktree, sandboxes FROM project WHERE id = ?", (state.project_id,)
    ).fetchall()
    expected_sandboxes = [
        targets[("project.sandbox", (state.project_id, str(index)))]
        for index, _sandbox in enumerate(json.loads(state.sandboxes))
    ]
    expected_project = (targets[("project.worktree", (state.project_id,))], json.dumps(expected_sandboxes))
    if project != [expected_project]:
        raise MoveError("move project post-update validation failed")
    for table, category in (
        ("project_directory", "project_directory.directory"),
        ("session", "session.directory"),
        ("workspace", "workspace.directory"),
    ):
        expected = [row for row in state.rows if row.table == table]
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE project_id = ?", (state.project_id,)
        ).fetchone()
        if count != (len(expected),):
            raise MoveError(f"move {table} row count changed")
        for row in expected:
            directory = targets[(category, row.identity)] if row.directory is not None else None
            if table == "project_directory":
                if len(row.payload) != 3:
                    raise MoveError("move reviewed project-directory payload is malformed")
                found = connection.execute(
                    "SELECT COUNT(*) FROM project_directory "
                    "WHERE project_id = ? AND directory = ? AND type IS ? "
                    "AND strategy IS ? AND time_created IS ?",
                    (state.project_id, directory, *row.payload),
                ).fetchone()
            else:
                found = connection.execute(
                    f"SELECT COUNT(*) FROM {_quote(table)} WHERE id = ? AND project_id = ? AND directory IS ?",
                    (row.identity[0], state.project_id, directory),
                ).fetchone()
            if found != (1,):
                raise MoveError(f"move {table} post-update validation failed")


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


def _validate_schema(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    """Require the bounded closed location schema and return its stable fingerprint."""
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    if len(objects) > MAX_SCHEMA_OBJECTS:
        raise MoveError("move schema object limit exceeded")
    fingerprint: list[tuple[str, str, str, str | None]] = []
    for row in objects:
        if (
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:3])
            or row[3] is not None and not isinstance(row[3], str)
        ):
            raise MoveError("move schema is malformed")
        for identifier in row[:3]:
            _identifier(identifier, "schema identifier")
        fingerprint.append((row[0], row[1], row[2], row[3]))
    tables = {name for object_type, name, _table, _sql in fingerprint if object_type == "table"}
    required = {
        "project": (("id", "TEXT", True), ("worktree", "TEXT", True), ("sandboxes", "TEXT", True)),
        "project_directory": (
            ("project_id", "TEXT", True),
            ("directory", "TEXT", True),
            ("type", "TEXT", False),
            ("strategy", "TEXT", False),
            ("time_created", "INTEGER", True),
        ),
        "session": (("id", "TEXT", True), ("project_id", "TEXT", True), ("directory", "TEXT", True)),
        "workspace": (("id", "TEXT", True), ("project_id", "TEXT", True), ("directory", "TEXT", False)),
    }
    if not set(required) <= tables:
        raise MoveError("move schema is incomplete")
    primary_keys = {
        "project": ("id",),
        "project_directory": ("project_id", "directory"),
        "session": ("id",),
        "workspace": ("id",),
    }
    for table, columns in required.items():
        metadata = _table_columns(connection, table)
        for name, declared_type, required_not_null in columns:
            column = metadata.get(name)
            if column is None or column[0] != declared_type or (required_not_null and not column[1] and not column[2]):
                raise MoveError("move schema is incompatible")
        if tuple(
            name
            for name, _metadata in sorted(metadata.items(), key=lambda item: item[1][2])
            if _metadata[2]
        ) != primary_keys[table]:
            raise MoveError("move schema primary key is incompatible")
        if any(hidden for _type, _not_null, _position, hidden in metadata.values()):
            raise MoveError("move schema has generated columns")
        _refuse_location_indexes(connection, table)
        _require_location_foreign_keys(connection, table)
    if any(object_type == "trigger" and table in required for object_type, _name, table, _sql in fingerprint):
        raise MoveError("move schema has side-effecting triggers")
    _refuse_inbound_directory_references(connection, tables)
    for table in tables - set(required):
        columns = _table_columns(connection, table)
        if {"project_id", "directory"} <= set(columns):
            raise MoveError("move schema has unknown project directory state")
    return tuple(fingerprint)


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[str, bool, int, int]]:
    """Return closed column metadata while refusing malformed or generated fields."""
    rows = connection.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
    result: dict[str, tuple[str, bool, int, int]] = {}
    for row in rows:
        if len(row) < 7 or not isinstance(row[1], str) or not isinstance(row[2], str) or type(row[3]) is not int or type(row[5]) is not int or type(row[6]) is not int:
            raise MoveError("move schema is malformed")
        if row[1] in result:
            raise MoveError("move schema is malformed")
        result[row[1]] = (row[2].upper(), bool(row[3]), row[5], row[6])
    if not result:
        raise MoveError("move schema is malformed")
    return result


def _refuse_location_indexes(connection: sqlite3.Connection, table: str) -> None:
    """Reject unfamiliar unique constraints involving a rewritten location field."""
    for index in connection.execute(f"PRAGMA index_list({_quote(table)})").fetchall():
        if len(index) < 4 or not isinstance(index[1], str) or type(index[2]) is not int or not isinstance(index[3], str):
            raise MoveError("move schema is malformed")
        if not index[2]:
            continue
        columns = connection.execute(f"PRAGMA index_info({_quote(index[1])})").fetchall()
        names = tuple(row[2] for row in columns if len(row) > 2 and isinstance(row[2], str))
        if len(names) != len(columns):
            raise MoveError("move schema is malformed")
        expected_primary = table == "project_directory" and index[3] == "pk" and names == ("project_id", "directory")
        rewritten_column = "worktree" if table == "project" else "directory"
        if rewritten_column in names and not expected_primary:
            raise MoveError("move schema has unfamiliar unique location index")


def _require_location_foreign_keys(connection: sqlite3.Connection, table: str) -> None:
    """Require the current closed outbound project relationship for each location table."""
    rows = connection.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall()
    expected = () if table == "project" else (
        (0, 0, "project", "project_id", "id", "NO ACTION", "CASCADE", "NONE"),
    )
    observed: list[tuple[object, ...]] = []
    for row in rows:
        if (
            len(row) != 8
            or type(row[0]) is not int
            or type(row[1]) is not int
            or not all(isinstance(row[index], str) for index in range(2, 8))
        ):
            raise MoveError("move schema is malformed")
        observed.append(tuple(row))
    if tuple(observed) != expected:
        raise MoveError("move schema has unfamiliar foreign keys")


def _refuse_inbound_directory_references(connection: sqlite3.Connection, tables: set[str]) -> None:
    """Refuse any foreign key that would make a rewritten directory a parent key."""
    affected = {"project", "project_directory", "session", "workspace"}
    for table in tables:
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall():
            if len(row) < 5 or not isinstance(row[2], str) or not isinstance(row[4], str):
                raise MoveError("move schema is malformed")
            if row[2] in affected and row[4] == "directory":
                raise MoveError("move schema has inbound directory references")


def _capture_project(
    connection: sqlite3.Connection, project_id: str
) -> tuple[dict[str, str], tuple[CapturedRow, ...]]:
    """Capture exactly one selected project and every supported location-bearing row."""
    projects = connection.execute(
        "SELECT id, worktree, sandboxes FROM project WHERE id = ?", (project_id,)
    ).fetchall()
    if not projects:
        raise MoveError("project ID was not found")
    if len(projects) != 1:
        raise MoveError("project ID is ambiguous")
    project_row = projects[0]
    if len(project_row) != 3:
        raise MoveError("move project row is malformed")
    project = {
        "id": _identifier(project_row[0], "project ID"),
        "worktree": _absolute_value(project_row[1], "project worktree"),
        "sandboxes": _sandbox_text(project_row[2]),
    }
    rows: list[CapturedRow] = []
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
        ).fetchall()
        if len(records) > MAX_SELECTED_ROWS:
            raise MoveError("move selected row limit exceeded")
        for record in records:
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
            rows.append(CapturedRow(table, identity, directory, payload))
    if len(rows) + 1 > MAX_SELECTED_ROWS:
        raise MoveError("move selected row limit exceeded")
    _capture_size(project, rows)
    return project, tuple(rows)


def _sandbox_text(value: object) -> str:
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
        _absolute_value(sandbox, "project sandbox")
    return value


def _capture_size(project: dict[str, str], rows: list[CapturedRow]) -> None:
    """Enforce aggregate scalar bounds before immutable plan construction."""
    values = list(project.values())
    for row in rows:
        values.extend(row.identity)
        if row.directory is not None:
            values.append(row.directory)
        values.extend(row.payload)
    total = sum(_captured_scalar_size(value) for value in values)
    if total > MAX_CAPTURE_BYTES:
        raise MoveError("move captured scalar limit exceeded")


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


def _build_mappings(
    state: CapturedState, target_main: str, *, deadline: float
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
        _add_location(locations, sandbox, LocationMembership("project.sandbox", (state.project_id, str(index))))
    categories = {
        "project_directory": "project_directory.directory",
        "session": "session.directory",
        "workspace": "workspace.directory",
    }
    for row in state.rows:
        if row.directory is not None:
            _add_location(locations, row.directory, LocationMembership(categories[row.table], row.identity))
    if len(locations) > MAX_LOCATIONS:
        raise MoveError("move distinct location limit exceeded")
    mappings: list[MoveMapping] = []
    derived: dict[str, str] = {}
    for source in sorted(locations):
        _check_revalidation_deadline(deadline)
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
        _check_revalidation_deadline(deadline)
        target_git = _git_evidence(target, "target")
        _check_revalidation_deadline(deadline)
        _compare_git(source, target, source_git, target_git)
        mappings.append(MoveMapping(source, target, memberships, source_git, target_git))
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
    """Collect bounded local-only identity and checkout state evidence for one root."""
    root = _git_output(path, side, ("rev-parse", "--show-toplevel"), "repository root")
    if _single_line(root, "repository root") != os.path.normpath(path):
        raise MoveError(f"git {side} path is not a worktree root: path={_display(path)}")
    origin_result = _git_output(path, side, ("config", "--get", "remote.origin.url"), "origin", allow_missing=True)
    branch_result = _git_output(path, side, ("symbolic-ref", "--quiet", "--short", "HEAD"), "checkout state", allow_missing=True)
    head = _commit(_single_line(_git_output(path, side, ("rev-parse", "HEAD"), "HEAD"), "HEAD"), "HEAD")
    if origin_result is None:
        identity = f"root:{_root_commit(path, side)}"
    else:
        normalized = _normalize_origin(_single_line(origin_result, "origin"))
        identity = f"root:{_root_commit(path, side)}" if normalized is None else "origin:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if branch_result is None:
        return GitEvidence(identity, "detached", None, head)
    branch = _single_line(branch_result, "branch")
    if not branch or any(character.isspace() or ord(character) < 32 for character in branch):
        raise MoveError(f"git {side} branch output is malformed: path={_display(path)}")
    return GitEvidence(identity, "attached", branch, head)


def _root_commit(path: str, side: str) -> str:
    """Return exactly one root commit for local-origin fallback identity evidence."""
    output = _git_output(path, side, ("rev-list", "--max-parents=0", "HEAD"), "root commit")
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        raise MoveError(f"git {side} root commit is ambiguous: path={_display(path)}")
    return _commit(lines[0], "root commit")


def _git_output(
    path: str, side: str, arguments: tuple[str, ...], label: str, *, allow_missing: bool = False
) -> str | None:
    """Run one local no-shell Git probe with live stdout cap and forced reaping."""
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    output = bytearray()
    try:
        process = subprocess.Popen(
            ["git", "-C", path, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise MoveOperationalError("git probe could not capture stdout")
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MoveOperationalError("git probe timed out")
            events = selector.select(remaining)
            if not events:
                if process.poll() is not None:
                    break
                continue
            for key, _event in events:
                chunk = os.read(key.fd, min(4096, MAX_GIT_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_GIT_OUTPUT_BYTES:
                    raise MoveOperationalError("git probe stdout limit exceeded")
        status = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        if status != 0:
            if allow_missing and status == 1:
                return None
            raise MoveError(f"git {side} {label} probe failed: path={_display(path)}")
        try:
            return output.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise MoveError(f"git {side} {label} output is malformed: path={_display(path)}") from error
    except KeyboardInterrupt as error:
        raise MoveOperationalError("git probe was interrupted") from error
    except FileNotFoundError as error:
        raise MoveOperationalError("git is unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise MoveOperationalError("git probe timed out") from error
    except OSError as error:
        raise MoveOperationalError("git probe could not run") from error
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _single_line(value: str, label: str) -> str:
    """Return one bounded single-line Git value while rejecting malformed output."""
    if not value.endswith("\n") or value.count("\n") != 1 or "\r" in value:
        raise MoveError(f"git {label} output is malformed")
    result = value[:-1]
    if not result or len(result.encode("utf-8", "surrogatepass")) > MAX_VALUE_BYTES:
        raise MoveError(f"git {label} output is malformed")
    return result


def _commit(value: str, label: str) -> str:
    """Require a bounded SHA-1 or SHA-256 hexadecimal Git commit identifier."""
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None:
        raise MoveError(f"git {label} output is malformed")
    return value


def _normalize_origin(value: str) -> str | None:
    """Normalize a non-file Git origin without retaining credentials for diagnostics."""
    if len(value.encode("utf-8", "surrogatepass")) > MAX_VALUE_BYTES or any(ord(character) < 32 for character in value):
        raise MoveError("git origin output is malformed")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise MoveError("git origin output is malformed") from error
    if parsed.scheme:
        if parsed.scheme.lower() == "file" or not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError as error:
            raise MoveError("git origin output is malformed") from error
        host = parsed.hostname.lower()
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))
    if ":" in value and not any(character.isspace() for character in value):
        host_path = value.rsplit("@", 1)[-1]
        if host_path.startswith("/") or host_path.startswith("."):
            return None
        return host_path.rstrip("/")
    return None


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
