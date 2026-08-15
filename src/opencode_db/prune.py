"""Prune complete known session state from one explicitly selected SQLite database.

The public operation uses the current session-owned table map from
``transfer``. It estimates logical payload bytes without rendering row content,
refuses incomplete or unknown session-linked schemas, and changes only the
selected existing database in one immediate transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import sqlite3
import time

from .transfer import (
    CHILD_SESSION_TABLES,
    SESSION_SELECTOR_COLUMNS,
    SESSION_TABLES,
    TransferError,
    TransferOperationalError,
    _columns,
    _canonical_session_table_name,
    _existing_regular_path,
    _open_rw,
    _quote,
    _validate_database,
    _validate_session_schema,
)

_MAX_VALUE = (1 << 63) - 1
_MAX_NUMERIC_DIGITS = len(str(_MAX_VALUE))
_BUSY_TIMEOUT_MS = 10_000
_SELECTION_TABLE = "_opencode_db_prune_selection"
_TIME_PATTERN = re.compile(r"([1-9][0-9]*)([dmy])\Z")
_SIZE_PATTERN = re.compile(r"([1-9][0-9]*)(B|KiB|MiB|GiB|TiB)\Z")
_SIZE_MULTIPLIERS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
_TIME_MULTIPLIERS_MS = {"d": 86_400_000, "m": 30 * 86_400_000, "y": 365 * 86_400_000}


class PruneError(RuntimeError):
    """Report a bounded active-database pruning refusal without row contents.

    Parameters: ``message`` is a fixed public classification for invalid paths,
    schema, selectors, or ownership. Raising the exception does not commit a
    database mutation and never includes persisted session values.
    """

    def __init__(self, message: str) -> None:
        """Store one bounded, content-free diagnostic."""
        super().__init__(message[:256])


class PruneOperationalError(PruneError):
    """Report a bounded SQLite or filesystem failure during an active prune.

    This subtype distinguishes an unavailable writer, validation failure, or
    vacuum failure from a schema or selector safety refusal. A successful delete
    commit remains durable if a later requested vacuum fails.
    """


class PruneCommittedError(PruneOperationalError):
    """Report a failed post-commit vacuum while preserving the prune outcome.

    ``outcome`` records the deletion that committed before physical compaction
    failed. Callers must report that committed state so an operator does not
    repeat a destructive selector while trying to retry only the vacuum.
    """

    def __init__(self, message: str, outcome: "PruneOutcome") -> None:
        """Store a bounded diagnostic and the already committed prune outcome."""
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class PruneRequest:
    """Represent one grammar-validated active session pruning request.

    ``database`` is an absolute selected path, only one selector is populated,
    and optional flags control reporting, physical compaction, or explicit
    confirmation bypass. The value has no SQLite or filesystem side effects.
    """

    database: str
    project_id: str | None = None
    oldest: str | None = None
    keep_newest: str | None = None
    target_size: str | None = None
    estimate_size: bool = False
    vacuum: bool = False
    yes: bool = False
    command: str = "prune"


@dataclass(frozen=True)
class PruneOutcome:
    """Describe one committed session prune without exposing any row content.

    ``deleted_sessions`` is the exact selected session count;
    ``deleted_logical_bytes`` and ``logical_database_bytes`` use the documented
    logical payload estimate when requested and are otherwise ``None``.
    ``physical_database_bytes`` is present only after a requested successful
    vacuum. Constructing the result has no side effects.
    """

    deleted_sessions: int
    deleted_logical_bytes: int | None
    logical_database_bytes: int | None
    physical_database_bytes: int | None = None


def parse_selector(value: str) -> tuple[str, int]:
    """Parse one positive count or OpenCode session duration selector.

    Parameters: ``value`` is either a positive base-10 count or a positive
    integer followed by ``d``, ``m``, or ``y``. Returns ``("count", count)`` or
    ``("time", milliseconds)``. Raises :class:`PruneError` for zero, negative,
    malformed, or out-of-range values; no state is accessed.
    """
    if len(value) <= _MAX_NUMERIC_DIGITS and re.fullmatch(r"[1-9][0-9]*", value):
        numeric = int(value)
        if numeric <= _MAX_VALUE:
            return "count", numeric
    match = _TIME_PATTERN.fullmatch(value)
    if match is not None and len(match.group(1)) <= _MAX_NUMERIC_DIGITS:
        numeric = int(match.group(1))
        multiplier = _TIME_MULTIPLIERS_MS[match.group(2)]
        if numeric <= _MAX_VALUE // multiplier:
            return "time", numeric * multiplier
    raise PruneError("retention selector must be a positive count or duration")


def parse_target_size(value: str) -> int:
    """Parse a positive bounded byte size using the ``N[B|KiB|MiB|GiB|TiB]`` grammar.

    Parameters: ``value`` is an explicit base-10 quantity with an IEC byte unit.
    Returns its byte count up to signed 64-bit SQLite bounds. Raises
    :class:`PruneError` for ambiguous, zero, negative, malformed, or oversized
    values. Parsing has no filesystem or SQLite side effects.
    """
    match = _SIZE_PATTERN.fullmatch(value)
    if match is None:
        raise PruneError("target size must use N[B|KiB|MiB|GiB|TiB]")
    if len(match.group(1)) > _MAX_NUMERIC_DIGITS:
        raise PruneError("target size is outside supported bounds")
    numeric = int(match.group(1))
    multiplier = _SIZE_MULTIPLIERS[match.group(2)]
    if numeric > _MAX_VALUE // multiplier:
        raise PruneError("target size is outside supported bounds")
    return numeric * multiplier


def session_logical_bytes(connection: sqlite3.Connection, session_id: str) -> int:
    """Estimate one complete session's known owned-row payload bytes.

    Parameters: ``connection`` has passed :func:`validate_session_schema` and
    ``session_id`` is an exact session identifier. Returns the sum of byte lengths
    of every non-NULL column value in the session row and all known child rows;
    SQLite record headers, pages, indexes, freelist, and WAL bytes are excluded.
    Raises :class:`PruneError` for malformed schema metadata. It reads only
    aggregate byte lengths and does not render persisted values.
    """
    sizes = session_logical_sizes(connection, session_id=session_id)
    return sizes.get(session_id, 0)


def session_logical_sizes(
    connection: sqlite3.Connection,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, int]:
    """Estimate known owned-row payload bytes for matching sessions in batches.

    Parameters: ``connection`` has passed :func:`validate_session_schema`, and
    optional exact project/session IDs restrict the selected session rows.
    Returns every matching session ID mapped to its complete known logical byte
    estimate. Each owned table is aggregated once, so listing many estimates
    does not issue one query per session. Raises :class:`PruneError` for
    malformed ownership data and does not mutate or render persisted values.
    """
    filters: list[str] = []
    parameters: list[object] = []
    if project_id is not None:
        filters.append("session.project_id = ?")
        parameters.append(project_id)
    if session_id is not None:
        filters.append("session.id = ?")
        parameters.append(session_id)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    session_columns = _table_columns(connection, "session")
    session_expression = _payload_expression(session_columns, "session")
    session_rows = connection.execute(
        f"SELECT session.id, {session_expression} FROM session AS session "
        f"{where} ORDER BY session.id",
        tuple(parameters),
    ).fetchall()
    if any(
        len(row) != 2
        or not isinstance(row[0], str)
        or not row[0]
        or type(row[1]) is not int
        or row[1] < 0
        for row in session_rows
    ):
        raise PruneError("database session data is malformed")
    session_ids = [row[0] for row in session_rows]
    if len(session_ids) != len(set(session_ids)):
        raise PruneError("database session data is malformed")
    sizes = {row[0]: row[1] for row in session_rows}
    for table in CHILD_SESSION_TABLES:
        columns = _table_columns(connection, table)
        expression = _payload_expression(columns, "owned")
        selector = SESSION_SELECTOR_COLUMNS[table]
        records = connection.execute(
            f"SELECT owned.{_quote(selector)}, COALESCE(SUM({expression}), 0) "
            f"FROM {_quote(table)} AS owned JOIN session AS session "
            f"ON session.id = owned.{_quote(selector)} {where} "
            f"GROUP BY owned.{_quote(selector)}",
            tuple(parameters),
        ).fetchall()
        for owned_session_id, logical_bytes in records:
            if (
                owned_session_id not in sizes
                or type(logical_bytes) is not int
                or logical_bytes < 0
            ):
                raise PruneError("database logical size estimate is invalid")
            sizes[owned_session_id] += logical_bytes
    return sizes


def project_logical_bytes(connection: sqlite3.Connection, project_id: str) -> int:
    """Estimate the sum of complete known session state owned by one project.

    Parameters: ``connection`` has passed :func:`validate_session_schema` and
    ``project_id`` is exact. Returns the sum of :func:`session_logical_bytes` for
    matching session rows. It reads aggregate lengths and session identifiers but
    does not output row content or mutate the database.
    """
    return sum(session_logical_sizes(connection, project_id=project_id).values())


def project_logical_sizes(connection: sqlite3.Connection) -> dict[str, int]:
    """Estimate complete known session payload bytes grouped by project.

    Parameters: ``connection`` has passed :func:`validate_session_schema`.
    Returns all exact project IDs mapped to the sum of their session estimates,
    including zero for projects without sessions. Raises :class:`PruneError` for
    malformed or ambiguous ownership and performs no mutation.
    """
    project_rows = connection.execute("SELECT id FROM project ORDER BY id").fetchall()
    if any(
        len(row) != 1 or not isinstance(row[0], str) or not row[0]
        for row in project_rows
    ):
        raise PruneError("database project data is malformed")
    project_ids = [row[0] for row in project_rows]
    if len(project_ids) != len(set(project_ids)):
        raise PruneError("database project data is malformed")
    sizes = dict.fromkeys(project_ids, 0)
    for table in SESSION_TABLES:
        columns = _table_columns(connection, table)
        expression = _payload_expression(columns, "owned")
        selector = SESSION_SELECTOR_COLUMNS[table]
        if table == "session":
            records = connection.execute(
                f"SELECT owned.project_id, COALESCE(SUM({expression}), 0) "
                "FROM session AS owned GROUP BY owned.project_id"
            ).fetchall()
        else:
            records = connection.execute(
                f"SELECT session.project_id, COALESCE(SUM({expression}), 0) "
                f"FROM {_quote(table)} AS owned JOIN session AS session "
                f"ON session.id = owned.{_quote(selector)} "
                "GROUP BY session.project_id"
            ).fetchall()
        for project_id, logical_bytes in records:
            if (
                project_id not in sizes
                or type(logical_bytes) is not int
                or logical_bytes < 0
            ):
                raise PruneError("database logical size estimate is invalid")
            sizes[project_id] += logical_bytes
    return sizes


def validate_session_schema(connection: sqlite3.Connection) -> None:
    """Require the complete known session schema needed for safe estimates/deletes.

    Parameters: ``connection`` is an open selected SQLite database. Returns
    ``None`` after requiring all current transfer-owned tables, rejecting unknown
    session-linked tables or triggers, and requiring session timestamps. Raises
    :class:`PruneError` for unsupported schema; it does not mutate the database.
    """
    try:
        _validate_session_schema(connection, archive=False)
        columns = _columns(connection, "session")
    except TransferError as error:
        raise PruneError(str(error)) from error
    if not {"id", "project_id", "time_updated"} <= set(columns):
        raise PruneError("database session schema is incomplete")
    _require_project_table(connection)


def prune_sessions(
    database: str | Path,
    *,
    project_id: str | None = None,
    oldest: str | None = None,
    keep_newest: str | None = None,
    target_size: str | None = None,
    estimate_size: bool = False,
    vacuum: bool = False,
    now_ms: int | None = None,
) -> PruneOutcome:
    """Atomically prune selected sessions from one existing SQLite database.

    Parameters: ``database`` is an existing absolute regular file opened through
    SQLite ``mode=rw``; ``project_id`` optionally scopes candidates to one exact
    existing project; exactly one selector is supplied; ``estimate_size`` enables
    human reporting; and ``vacuum`` requests post-commit physical compaction.
    Returns committed counts and requested logical estimates. Raises
    :class:`PruneError` for selector, path, schema, project, or ownership
    refusals, :class:`PruneOperationalError` for bounded SQLite/filesystem
    failures, and :class:`PruneCommittedError` with the committed outcome when a
    requested post-commit vacuum fails. The function enables foreign keys,
    validates integrity before and after deletion, deletes known child rows and
    session rows in one immediate transaction, and rolls back all uncommitted
    changes on failure. Vacuum, when requested, runs only after that commit.
    """
    selector_name, raw_selector = _one_selector(oldest, keep_newest, target_size)
    if selector_name == "target_size":
        selector_kind = "size"
        selector_value = parse_target_size(raw_selector)
    else:
        selector_kind, selector_value = parse_selector(raw_selector)
    try:
        path = _existing_regular_path(database, "database")
    except TransferOperationalError as error:
        raise PruneOperationalError(str(error)) from error
    except TransferError as error:
        raise PruneError(str(error)) from error
    except OSError as error:
        raise PruneOperationalError("database could not be inspected") from error
    connection = _open_prune_connection(path)
    committed = False
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys = ON")
            _validate_for_prune(connection)
            _validate_global_session_ids(connection)
            if project_id is not None:
                _require_project(connection, project_id)
            candidates = _candidate_sessions(connection, project_id)
            logical_sizes = (
                session_logical_sizes(connection, project_id=project_id)
                if selector_name == "target_size" or estimate_size
                else None
            )
            deleted_ids = _select_deleted_ids(
                candidates,
                selector_name,
                selector_kind,
                selector_value,
                logical_sizes,
                now_ms=now_ms,
            )
            deleted_bytes = (
                sum(logical_sizes[session_id] for session_id in deleted_ids)
                if estimate_size and logical_sizes is not None
                else None
            )
            if deleted_ids:
                _prepare_selection(connection, deleted_ids)
                _refuse_retained_session_children(connection)
                _refuse_cross_boundary_session_foreign_keys(connection)
                for table in CHILD_SESSION_TABLES:
                    selector = SESSION_SELECTOR_COLUMNS[table]
                    connection.execute(
                        f"DELETE FROM {_quote(table)} WHERE {_quote(selector)} "
                        f"IN (SELECT id FROM temp.{_quote(_SELECTION_TABLE)})"
                    )
                deleted_sessions = connection.execute(
                    f"DELETE FROM session WHERE id IN (SELECT id FROM temp.{_quote(_SELECTION_TABLE)})"
                ).rowcount
                if deleted_sessions != len(deleted_ids):
                    raise PruneError("session deletion did not match selected sessions")
                _validate_for_prune(connection)
                logical_database_bytes = (
                    database_logical_bytes(connection) if estimate_size else None
                )
                connection.commit()
                committed = True
            else:
                logical_database_bytes = (
                    database_logical_bytes(connection) if estimate_size else None
                )
                # A no-match request is an observable no-op unless explicit VACUUM follows.
                connection.rollback()
        except PruneError:
            connection.rollback()
            raise
        except TransferOperationalError as error:
            connection.rollback()
            raise PruneOperationalError(str(error)) from error
        except TransferError as error:
            connection.rollback()
            raise PruneError(str(error)) from error
        except (sqlite3.Error, OSError, ValueError, OverflowError) as error:
            connection.rollback()
            raise PruneOperationalError("session prune failed") from error

        outcome = PruneOutcome(
            len(deleted_ids), deleted_bytes, logical_database_bytes, None
        )
        if vacuum:
            try:
                physical_database_bytes = _vacuum(connection)
            except PruneOperationalError as error:
                if committed:
                    raise PruneCommittedError(
                        "sessions were pruned but vacuum failed; do not repeat the prune request",
                        outcome,
                    ) from error
                raise PruneOperationalError("database vacuum failed") from error
            outcome = replace(
                outcome,
                physical_database_bytes=physical_database_bytes,
            )
        return outcome
    finally:
        connection.close()


def database_logical_bytes(connection: sqlite3.Connection) -> int:
    """Estimate all non-system table payload bytes in the current database view.

    Parameters: ``connection`` is an open validated SQLite connection. Returns
    the sum of byte lengths of every non-NULL table value, excluding SQLite page,
    schema, index, freelist, and WAL overhead. Raises :class:`PruneError` for
    malformed table metadata. It never renders row content or changes the DB.
    """
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    total = 0
    for (table,) in rows:
        if not isinstance(table, str):
            raise PruneError("database schema is invalid")
        total += _table_payload_bytes(connection, table)
    return total


def _one_selector(
    oldest: str | None, keep_newest: str | None, target_size: str | None
) -> tuple[str, str]:
    selections = [("oldest", oldest), ("keep_newest", keep_newest), ("target_size", target_size)]
    present = [(name, value) for name, value in selections if value is not None]
    if len(present) != 1:
        raise PruneError("exactly one prune selector is required")
    name, value = present[0]
    assert value is not None
    return name, value


def _open_prune_connection(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_rw(path)
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection
    except (sqlite3.Error, TransferError) as error:
        if connection is not None:
            connection.close()
        raise PruneOperationalError("database could not be opened in SQLite rw mode") from error


def _validate_for_prune(connection: sqlite3.Connection) -> None:
    try:
        _validate_database(connection)
    except TransferOperationalError as error:
        raise PruneOperationalError(str(error)) from error
    validate_session_schema(connection)


def _require_project_table(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'project'"
    ).fetchall()
    try:
        columns = _columns(connection, "project")
    except TransferError as error:
        raise PruneError(str(error)) from error
    if tables != [("project",)] or "id" not in columns:
        raise PruneError("database project schema is incomplete")


def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
    rows = connection.execute("SELECT id FROM project WHERE id = ?", (project_id,)).fetchall()
    if not rows:
        raise PruneError("project ID was not found")
    if len(rows) != 1:
        raise PruneError("project ID is ambiguous")


def _candidate_sessions(connection: sqlite3.Connection, project_id: str | None) -> list[tuple[str, int]]:
    where = "WHERE project_id = ?" if project_id is not None else ""
    parameters: tuple[object, ...] = (project_id,) if project_id is not None else ()
    rows = connection.execute(
        f"SELECT id, time_updated FROM session {where} ORDER BY time_updated DESC, id ASC", parameters
    ).fetchall()
    candidates: list[tuple[str, int]] = []
    for session_id, updated in rows:
        if not isinstance(session_id, str) or not session_id or type(updated) is not int:
            raise PruneError("database session data is malformed")
        candidates.append((session_id, updated))
    if len(candidates) != len({session_id for session_id, _updated in candidates}):
        raise PruneError("database session data is malformed")
    return candidates


def _validate_global_session_ids(connection: sqlite3.Connection) -> None:
    """Refuse non-unique session IDs before any project-scoped delete selection.

    Session rows can be project-scoped while the destructive predicate is the
    globally unscoped ``session.id``. This check therefore rejects a schema/data
    combination where one selected ID could delete a row from another project.
    """
    duplicate = connection.execute(
        "SELECT 1 FROM session GROUP BY id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise PruneError("session IDs are not globally unique")


def _select_deleted_ids(
    candidates: list[tuple[str, int]],
    selector_name: str,
    selector_kind: str,
    selector_value: int,
    logical_sizes: dict[str, int] | None,
    *,
    now_ms: int | None,
) -> list[str]:
    if selector_name == "oldest":
        return _oldest_selection(candidates, selector_kind, selector_value)
    if selector_name == "keep_newest":
        return _keep_newest_selection(candidates, selector_kind, selector_value, now_ms)
    if selector_name == "target_size":
        if logical_sizes is None:
            raise AssertionError("target-size selection requires logical estimates")
        retained = 0
        for index, (session_id, _updated) in enumerate(candidates):
            estimate = logical_sizes[session_id]
            if retained + estimate > selector_value:
                return [candidate_id for candidate_id, _time in candidates[index:]]
            retained += estimate
        return []
    raise AssertionError("unknown selector")


def _oldest_selection(
    candidates: list[tuple[str, int]], kind: str, value: int
) -> list[str]:
    if kind == "count":
        return [session_id for session_id, _updated in candidates[-value:]]
    oldest_first = sorted(candidates, key=lambda row: (row[1], row[0]))
    if not oldest_first:
        return []
    upper_bound = oldest_first[0][1] + value
    return [session_id for session_id, updated in oldest_first if updated <= upper_bound]


def _keep_newest_selection(
    candidates: list[tuple[str, int]], kind: str, value: int, now_ms: int | None
) -> list[str]:
    if kind == "count":
        return [session_id for session_id, _updated in candidates[value:]]
    cutoff = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff -= value
    return [session_id for session_id, updated in candidates if updated < cutoff]


def _prepare_selection(connection: sqlite3.Connection, session_ids: list[str]) -> None:
    connection.execute(f"CREATE TEMP TABLE {_quote(_SELECTION_TABLE)} (id TEXT PRIMARY KEY)")
    connection.executemany(
        f"INSERT INTO temp.{_quote(_SELECTION_TABLE)} VALUES (?)", ((session_id,) for session_id in session_ids)
    )


def _refuse_retained_session_children(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "session")
    if "parent_id" not in columns:
        return
    retained_child = connection.execute(
        f"SELECT 1 FROM session AS child JOIN temp.{_quote(_SELECTION_TABLE)} AS parent "
        "ON parent.id = child.parent_id LEFT JOIN temp."
        f"{_quote(_SELECTION_TABLE)} AS child_selected ON child_selected.id = child.id "
        "WHERE child_selected.id IS NULL LIMIT 1"
    ).fetchone()
    if retained_child is not None:
        raise PruneError("retained session depends on a selected session")


def _refuse_cross_boundary_session_foreign_keys(connection: sqlite3.Connection) -> None:
    """Refuse known-table foreign keys whose child and parent owners differ.

    Every declared foreign key among known session tables is grouped by SQLite's
    key ID so composite keys are checked as one relationship. A retained child
    can otherwise be removed by a cascade from a selected parent, while a
    selected child can retain a dependency outside the selected ownership set.
    """
    for child_table in SESSION_TABLES:
        grouped: dict[tuple[int, str], list[tuple[str, str]]] = {}
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quote(child_table)})"
        ).fetchall()
        for foreign_key in foreign_keys:
            if len(foreign_key) < 5:
                raise PruneError("database session schema is invalid")
            key_id, sequence, parent_name, child_column, parent_column = foreign_key[:5]
            if (
                type(key_id) is not int
                or type(sequence) is not int
                or not isinstance(child_column, str)
                or not child_column
            ):
                raise PruneError("database session schema is invalid")
            parent_table = _canonical_session_table_name(parent_name)
            if parent_table is None:
                continue
            if not isinstance(parent_column, str) or not parent_column:
                raise PruneError("database session schema is invalid")
            grouped.setdefault((key_id, parent_table), []).append(
                (child_column, parent_column)
            )
        for (_key_id, parent_table), columns in grouped.items():
            child_selector = SESSION_SELECTOR_COLUMNS[child_table]
            parent_selector = SESSION_SELECTOR_COLUMNS[parent_table]
            populated = " AND ".join(
                f"child.{_quote(child_column)} IS NOT NULL"
                for child_column, _parent_column in columns
            )
            equal = " AND ".join(
                f"parent.{_quote(parent_column)} = child.{_quote(child_column)}"
                for child_column, parent_column in columns
            )
            boundary = connection.execute(
                f"SELECT 1 FROM {_quote(child_table)} AS child "
                f"JOIN {_quote(parent_table)} AS parent ON {equal} "
                f"LEFT JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_child "
                f"ON selected_child.id = child.{_quote(child_selector)} "
                f"LEFT JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_parent "
                f"ON selected_parent.id = parent.{_quote(parent_selector)} "
                f"WHERE {populated} AND ("
                "(selected_child.id IS NULL AND selected_parent.id IS NOT NULL) OR "
                "(selected_child.id IS NOT NULL AND selected_parent.id IS NULL)"
                ") LIMIT 1"
            ).fetchone()
            if boundary is not None:
                raise PruneError("known session foreign key crosses prune selection")


def _table_payload_bytes(connection: sqlite3.Connection, table: str) -> int:
    columns = _table_columns(connection, table)
    expression = _payload_expression(columns)
    result = connection.execute(
        f"SELECT COALESCE(SUM({expression}), 0) FROM {_quote(table)}"
    ).fetchone()
    if result is None or type(result[0]) is not int or result[0] < 0:
        raise PruneError("database logical size estimate is invalid")
    return result[0]


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Return validated table columns using the pruning error vocabulary."""
    try:
        return _columns(connection, table)
    except TransferError as error:
        raise PruneError(str(error)) from error


def _payload_expression(columns: tuple[str, ...], alias: str | None = None) -> str:
    """Return a SQLite expression for documented logical value-byte estimates."""
    prefix = f"{alias}." if alias is not None else ""
    return " + ".join(
        f"COALESCE(length(CAST({prefix}{_quote(column)} AS BLOB)), 0)"
        for column in columns
    )


def _vacuum(connection: sqlite3.Connection) -> int:
    """Compact and revalidate the already validated database connection.

    Returns physical bytes from the connection's current page geometry. Raises
    :class:`PruneOperationalError` without reopening the target, preserving the
    post-commit boundary that produced a possible :class:`PruneCommittedError`.
    """
    try:
        connection.execute("VACUUM")
        _validate_for_prune(connection)
        page_count = connection.execute("PRAGMA page_count").fetchone()
        page_size = connection.execute("PRAGMA page_size").fetchone()
        if (
            page_count is None
            or page_size is None
            or type(page_count[0]) is not int
            or type(page_size[0]) is not int
            or page_count[0] < 0
            or page_size[0] <= 0
        ):
            raise PruneOperationalError("database vacuum failed")
        return page_count[0] * page_size[0]
    except (sqlite3.Error, PruneError) as error:
        raise PruneOperationalError("database vacuum failed") from error
