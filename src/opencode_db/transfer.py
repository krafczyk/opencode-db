"""Export and import complete project-scoped OpenCode session SQLite state.

The transfer archive is a private SQLite file containing only the current known
session tables and closed metadata.  This module opens operator-selected
databases only through SQLite ``mode=rw`` and refuses schemas that could omit
unknown session-owned state.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .artifacts import new_artifact_id
from .target import TargetError, select_default_database

ARCHIVE_METADATA_TABLE = "_opencode_db_transfer_metadata"
"""Reserved table name holding the closed session-transfer archive metadata."""

ARCHIVE_SCHEMA_VERSION = 1
"""Supported private archive schema version."""

SESSION_SELECTOR_COLUMNS = {
    "session": "id",
    "message": "session_id",
    "part": "session_id",
    "todo": "session_id",
    "session_message": "session_id",
    "session_input": "session_id",
    "session_context_epoch": "session_id",
    "session_share": "session_id",
    "event_sequence": "aggregate_id",
    "event": "aggregate_id",
}
"""Column binding each transferred table row to its owning session."""

SESSION_TABLES = tuple(SESSION_SELECTOR_COLUMNS)
"""Current OpenCode tables whose rows are owned by a session."""

CHILD_SESSION_TABLES = (
    "message",
    "part",
    "todo",
    "session_message",
    "session_input",
    "session_context_epoch",
    "session_share",
    "event_sequence",
    "event",
)
"""Current session-owned tables replaced when their session is imported."""

_SELECTION_TABLE = "_opencode_db_selected_sessions"
_COPY_BATCH_SIZE = 256
_ASCII_LOWERCASE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


class TransferError(RuntimeError):
    """Report a safe session-transfer refusal without database row contents.

    Parameters: ``message`` is a bounded protocol or operational classification.
    The exception is raised before publication or after rolling back an import;
    it never includes session contents, prompts, credentials, or SQLite details.
    """

    def __init__(self, message: str) -> None:
        """Store one bounded, content-free public transfer diagnostic."""
        super().__init__(message[:256])


class TransferOperationalError(TransferError):
    """Report a bounded transfer filesystem, SQLite, integrity, or publication failure.

    This subtype distinguishes failures that prevent an otherwise valid transfer
    from safety-precondition refusals. It has no additional state or side effects.
    """


class TransferUsageError(ValueError):
    """Report an invalid top-level transfer command grammar.

    Parameters: ``command`` identifies the parsed transfer command and
    ``message`` describes its bounded input-shape failure.  Construction does
    not access databases or files.
    """

    def __init__(self, command: str, message: str) -> None:
        """Store the command identifier and safe public grammar message."""
        super().__init__(message[:256])
        self.command = command


@dataclass(frozen=True)
class TransferRequest:
    """Represent one grammar-validated export or import request.

    Parameters identify absolute paths. ``database`` is either explicit or the
    documented environment default; ``project_dir`` is the export source project
    for ``export`` or target project for ``import``; ``export_dir`` and
    ``import_file`` are present only for their matching operation. The value
    neither opens SQLite nor changes filesystem state.
    """

    command: str
    database: str
    project_dir: str
    export_dir: str | None = None
    import_file: str | None = None


@dataclass(frozen=True)
class ExportOutcome:
    """Describe one successfully published private session-transfer archive.

    ``export_dir`` is the canonical directory used, ``source_project_id`` is
    the uniquely resolved OpenCode project, and ``import_file`` is the single
    private SQLite archive.  Constructing this result has no side effects.
    """

    export_dir: Path
    source_project_id: str
    import_file: Path
    exported_sessions: int


@dataclass(frozen=True)
class ImportOutcome:
    """Describe one committed session-transfer import.

    ``target_project_id`` is the uniquely resolved destination project and
    ``imported_sessions`` counts archive session rows atomically replaced in the
    selected database.  Constructing this result has no side effects.
    """

    target_project_id: str
    imported_sessions: int


def parse_transfer_command(arguments: list[str]) -> TransferRequest:
    """Parse one closed top-level session transfer command without I/O.

    Parameters: ``arguments`` is argv excluding the program name and must begin
    with ``export`` or ``import``. Returns a :class:`TransferRequest` with
    absolute paths only. Raises :class:`TransferUsageError` for unknown,
    duplicate, missing, or unsupported options.  Parsing does not inspect paths,
    open SQLite, or create artifacts.
    """
    if not arguments or arguments[0] not in {"export", "import"}:
        raise TransferUsageError("unknown", "Expected an export or import command.")
    command = arguments[0]
    allowed = {
        "export": {"db", "project-dir", "export-dir"},
        "import": {"target-project-dir", "db", "import"},
    }[command]
    options: dict[str, str] = {}
    position = 1
    while position < len(arguments):
        token = arguments[position]
        if not token.startswith("--"):
            raise TransferUsageError(command, "Unexpected positional argument.")
        name = token[2:]
        if name not in allowed:
            raise TransferUsageError(command, "Unsupported command option.")
        if name in options:
            raise TransferUsageError(command, "Command options may not be repeated.")
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            raise TransferUsageError(command, "A command option is missing its value.")
        options[name] = arguments[position + 1]
        position += 2
    required = allowed - {"db"}
    if missing := required - set(options):
        raise TransferUsageError(
            command, f"Missing required option --{sorted(missing)[0]}."
        )
    database = (
        _absolute_option(options["db"], command, "db")
        if "db" in options
        else _default_database(command)
    )
    if command == "export":
        return TransferRequest(
            command=command,
            database=database,
            project_dir=_absolute_option(
                options["project-dir"], command, "project-dir"
            ),
            export_dir=_absolute_option(options["export-dir"], command, "export-dir"),
        )
    return TransferRequest(
        command=command,
        database=database,
        project_dir=_absolute_option(
            options["target-project-dir"], command, "target-project-dir"
        ),
        import_file=_absolute_option(options["import"], command, "import"),
    )


def _default_database(command: str) -> str:
    """Return the environment-selected transfer database without inspecting it.

    Parameters: ``command`` identifies the grammar being parsed. Returns the
    concrete absolute default path. Raises :class:`TransferUsageError` when no
    absolute XDG or HOME base is available; downstream validators retain file
    existence and regularity checks.
    """
    try:
        return select_default_database()
    except TargetError as error:
        raise TransferUsageError(command, "Default database path is unavailable.") from error


def export_sessions(
    database: str | Path, project_dir: str | Path, export_dir: str | Path
) -> ExportOutcome:
    """Write one private, importable archive for an explicitly selected project.

    Parameters: ``database`` is an existing regular SQLite file opened in
    ``mode=rw``; ``project_dir`` is an existing absolute directory used to
    resolve one project; and ``export_dir`` is an absolute output directory made
    private as mode ``0700``.  Returns canonical output location, resolved
    project ID, archive path, and session count.  Raises :class:`TransferError`
    for invalid paths, ambiguous projects, unsupported schemas, failed SQLite
    checks, or publication failure.  It reads the database in one transaction,
    writes only a temporary archive, and atomically publishes one mode-``0600``
    file without printing session data.
    """
    source_path = _existing_regular_path(database, "database")
    source_dir = _existing_directory(project_dir, "project directory")
    output_dir = _private_output_directory(export_dir)
    connection = _open_rw(source_path)
    temporary: Path | None = None
    try:
        connection.execute("BEGIN")
        _validate_database(connection)
        _validate_session_schema(connection, archive=False)
        session_directory_matches = _session_directory_matches(connection, source_dir)
        project_id, _worktree = _resolve_project(
            connection, source_dir, session_directory_matches
        )
        session_ids = _export_session_ids(
            connection, project_id, session_directory_matches
        )
        _prepare_session_selection(connection, session_ids)
        _validate_session_parents(connection, selected=True)
        _validate_transferred_foreign_keys(connection, selected=True)
        temporary = _new_temporary_file(output_dir)
        _write_archive(connection, temporary, output_dir, project_id)
        connection.rollback()
        import_file = output_dir / _archive_name()
        os.replace(temporary, import_file)
        temporary = None
        os.chmod(import_file, 0o600)
        _sync_file(import_file)
        _sync_directory(output_dir)
        return ExportOutcome(output_dir, project_id, import_file, len(session_ids))
    except TransferError:
        connection.rollback()
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        connection.rollback()
        raise TransferOperationalError("session export failed") from error
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_sessions(
    target_project_dir: str | Path, database: str | Path, import_file: str | Path
) -> ImportOutcome:
    """Atomically replace imported session rows in one explicit target database.

    Parameters: ``target_project_dir`` is an existing absolute target directory,
    ``database`` is an existing regular SQLite file opened in ``mode=rw``, and
    ``import_file`` is an existing regular private archive.  Returns the target
    project ID and count of replaced session rows.  Raises :class:`TransferError`
    for malformed archives, ambiguous projects, incompatible schemas, integrity
    failures, or SQLite errors.  The function reads the archive, remaps imported
    sessions to the target project/directory, clears ``workspace_id`` when that
    column exists, and commits all replacement rows together or rolls back.
    """
    target_dir = _existing_directory(target_project_dir, "target project directory")
    target_path = _existing_regular_path(database, "database")
    archive_path = _existing_regular_path(import_file, "import file")
    archive = _open_ro(archive_path)
    target: sqlite3.Connection | None = None
    try:
        session_ids, archive_signatures = _read_archive(archive)
        target = _open_rw(target_path)
        _validate_database(target)
        _validate_session_schema(target, archive=False)
        _validate_destination_compatibility(target, archive_signatures)
        session_directory_matches = _session_directory_matches(target, target_dir)
        target_project_id, worktree = _resolve_project(
            target, target_dir, session_directory_matches
        )
        path_value = _target_session_path(target_dir, worktree)
        target.execute("BEGIN IMMEDIATE")
        target.execute("PRAGMA defer_foreign_keys = ON")
        _prepare_session_selection(target, session_ids)
        _validate_import_ownership(target, target_project_id, target_dir)
        for table in CHILD_SESSION_TABLES:
            selector = SESSION_SELECTOR_COLUMNS[table]
            target.execute(
                f"DELETE FROM {_quote(table)} WHERE {_quote(selector)} "
                f"IN (SELECT id FROM temp.{_quote(_SELECTION_TABLE)})"
            )
        target.execute(
            f"DELETE FROM session WHERE id IN "
            f"(SELECT id FROM temp.{_quote(_SELECTION_TABLE)})"
        )
        for table in SESSION_TABLES:
            _copy_archive_rows(
                archive,
                target,
                table,
                target_project_id=target_project_id,
                target_directory=str(target_dir),
                target_path=path_value,
            )
        _validate_database(target)
        target.commit()
        return ImportOutcome(target_project_id, len(session_ids))
    except TransferError:
        if target is not None:
            target.rollback()
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        if target is not None:
            target.rollback()
        raise TransferOperationalError("session import failed") from error
    finally:
        if target is not None:
            target.close()
        archive.close()


def _absolute_option(value: str, command: str, name: str) -> str:
    """Return one syntactically absolute CLI path or raise a grammar refusal."""
    if "\x00" in value or not os.path.isabs(value):
        raise TransferUsageError(
            command, f"--{name} requires an absolute filesystem path."
        )
    return value


def _existing_regular_path(value: str | Path, label: str) -> Path:
    """Resolve one existing regular path without creating a fallback database."""
    path = Path(value)
    if "\x00" in os.fspath(path) or not path.is_absolute():
        raise TransferError(f"{label} must be an absolute regular file")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise TransferError(f"{label} must be an existing regular file") from error
    except OSError as error:
        raise TransferOperationalError(
            f"{label} must be an existing regular file"
        ) from error
    if not stat.S_ISREG(mode):
        raise TransferError(f"{label} must be an existing regular file")
    return path.resolve()


def _existing_directory(value: str | Path, label: str) -> Path:
    """Resolve one existing absolute directory for exact project matching."""
    path = Path(value)
    if "\x00" in os.fspath(path) or not path.is_absolute() or not path.is_dir():
        raise TransferError(f"{label} must be an existing absolute directory")
    return path.resolve()


def _private_output_directory(value: str | Path) -> Path:
    """Create and lock down one absolute archive output directory as mode 0700."""
    path = Path(value)
    if "\x00" in os.fspath(path) or not path.is_absolute():
        raise TransferError("export directory must be an absolute directory")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise TransferError("export directory must be a directory")
        os.chmod(path, 0o700)
    except OSError as error:
        raise TransferOperationalError(
            "private export directory could not be established"
        ) from error
    return path.resolve()


def _open_rw(path: Path) -> sqlite3.Connection:
    """Open one existing SQLite path in non-creating rw mode with foreign keys on."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise TransferOperationalError("SQLite foreign keys could not be enabled")
        return connection
    except (sqlite3.Error, TransferError) as error:
        if connection is not None:
            connection.close()
        raise TransferOperationalError(
            "database could not be opened in SQLite rw mode"
        ) from error


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open one existing archive read-only without allowing SQLite file creation."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        return connection
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise TransferOperationalError("import archive could not be opened") from error


def _validate_database(connection: sqlite3.Connection) -> None:
    """Require SQLite integrity and foreign-key checks before committing transfer work."""
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise TransferOperationalError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TransferOperationalError("database foreign key check failed")
    except sqlite3.Error as error:
        raise TransferOperationalError(
            "database integrity validation failed"
        ) from error


def _table_names(connection: sqlite3.Connection) -> dict[str, str | None]:
    """Return non-system table names and definitions without reading table rows."""
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if any(not isinstance(name, str) for name, _sql in rows):
        raise TransferError("database schema is invalid")
    return {name: sql for name, sql in rows}


def _canonical_session_table_name(name: object) -> str | None:
    """Return a known session table's canonical name from SQLite metadata.

    SQLite compares identifiers case-insensitively for ASCII letters, while its
    schema and pragma metadata preserve the spelling used in DDL.  ``None``
    identifies a valid but non-session table name.  Raises :class:`TransferError`
    for malformed metadata rather than treating it as unrelated state.
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        raise TransferError("database schema is invalid")
    canonical = name.translate(_ASCII_LOWERCASE)
    return canonical if canonical in SESSION_SELECTOR_COLUMNS else None


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Return ordered table column names while rejecting malformed SQLite metadata."""
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = tuple(row[1] for row in rows if len(row) > 1 and isinstance(row[1], str))
    if not columns or len(columns) != len(rows) or len(set(columns)) != len(columns):
        raise TransferError("database schema is invalid")
    return columns


def _validate_session_schema(connection: sqlite3.Connection, *, archive: bool) -> None:
    """Validate known session tables and refuse unknown session-linked persisted state."""
    tables = _table_names(connection)
    required = set(SESSION_TABLES)
    if archive:
        if set(tables) != required | {ARCHIVE_METADATA_TABLE}:
            raise TransferError("archive schema is invalid")
    elif not required <= set(tables):
        raise TransferError("database session schema is incomplete")
    for table in SESSION_TABLES:
        if tables.get(table) is None:
            raise TransferError("database session schema is invalid")
        columns = _columns(connection, table)
        required_columns = (
            {"id", "project_id", "directory"}
            if table == "session"
            else {SESSION_SELECTOR_COLUMNS[table]}
        )
        if not required_columns <= set(columns):
            raise TransferError("database session schema is incomplete")
    if not archive:
        _refuse_unknown_session_tables(connection, tables)
        _refuse_session_triggers(connection)


def _refuse_unknown_session_tables(
    connection: sqlite3.Connection, tables: dict[str, str | None]
) -> None:
    """Refuse unknown tables that store or reference session-owned state."""
    known = set(SESSION_TABLES) | {"project", "project_directory"}
    for table in set(tables) - known:
        if "session_id" in _columns(connection, table):
            raise TransferError("unknown session-linked table")
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quote(table)})"
        ).fetchall()
        for foreign_key in foreign_keys:
            if len(foreign_key) < 3:
                raise TransferError("database schema is invalid")
            if _canonical_session_table_name(foreign_key[2]) is not None:
                raise TransferError("unknown session-linked table")


def _refuse_session_triggers(connection: sqlite3.Connection) -> None:
    """Refuse triggers on imported tables because they could alter replacement rows."""
    rows = connection.execute(
        "SELECT tbl_name FROM sqlite_schema WHERE type = 'trigger'"
    ).fetchall()
    for row in rows:
        if len(row) != 1:
            raise TransferError("database schema is invalid")
        if _canonical_session_table_name(row[0]) is not None:
            raise TransferError("database session schema is incompatible")


def _resolve_project(
    connection: sqlite3.Connection,
    directory: Path,
    session_directory_matches: dict[tuple[object, object], bool],
) -> tuple[str, str | None]:
    """Resolve one project ID from worktree, project-directory, or session paths.

    The ``directory`` is already canonical and existing.  Returns the unique
    project ID plus its recorded worktree when present.  Raises
    :class:`TransferError` if no project matches or multiple project IDs match.
    A global project matches only through an exact recorded session directory.
    """
    tables = _table_names(connection)
    if "project" not in tables:
        raise TransferError("database project schema is incomplete")
    project_columns = _columns(connection, "project")
    if "id" not in project_columns:
        raise TransferError("database project schema is incomplete")
    worktrees: dict[str, str | None] = {}
    select = "id, worktree" if "worktree" in project_columns else "id, NULL"
    for project_id, worktree in connection.execute(f"SELECT {select} FROM project"):
        if isinstance(project_id, str):
            worktrees[project_id] = worktree if isinstance(worktree, str) else None
    matches: set[str] = {
        project_id
        for project_id, worktree in worktrees.items()
        if project_id != "global" and _same_directory(worktree, directory)
    }
    if "project_directory" in tables:
        columns = _columns(connection, "project_directory")
        if {"project_id", "directory"} <= set(columns):
            for project_id, recorded in connection.execute(
                "SELECT project_id, directory FROM project_directory"
            ):
                if (
                    isinstance(project_id, str)
                    and project_id != "global"
                    and _same_directory(recorded, directory)
                ):
                    matches.add(project_id)
    session_columns = _columns(connection, "session")
    global_match = False
    if {"project_id", "directory"} <= set(session_columns):
        for project_id, recorded in connection.execute(
            "SELECT project_id, directory FROM session"
        ):
            if (
                not isinstance(project_id, str)
                or not session_directory_matches[(project_id, recorded)]
            ):
                continue
            if project_id == "global":
                global_match = True
            else:
                matches.add(project_id)
    if not matches and global_match:
        matches.add("global")
    if len(matches) != 1:
        raise TransferError("project directory resolves ambiguously or not at all")
    project_id = next(iter(matches))
    return project_id, worktrees.get(project_id)


def _same_directory(recorded: object, expected: Path) -> bool:
    """Return whether one recorded absolute directory resolves to the expected path."""
    if not isinstance(recorded, str) or not os.path.isabs(recorded):
        return False
    try:
        return Path(recorded).resolve() == expected
    except OSError:
        return False


def _session_directory_matches(
    connection: sqlite3.Connection, expected: Path
) -> dict[tuple[object, object], bool]:
    """Cache canonical matches for distinct recorded session project/directories."""
    rows = connection.execute(
        "SELECT DISTINCT project_id, directory FROM session"
    ).fetchall()
    return {
        (project_id, directory): _same_directory(directory, expected)
        for project_id, directory in rows
    }


def _export_session_ids(
    connection: sqlite3.Connection,
    project_id: str,
    session_directory_matches: dict[tuple[object, object], bool],
) -> list[str]:
    """Select the resolved project's sessions plus only directory-scoped globals."""
    rows = connection.execute(
        "SELECT id, project_id, directory FROM session"
    ).fetchall()
    session_ids: list[str] = []
    for session_id, recorded_project_id, directory in rows:
        if not isinstance(session_id, str):
            raise TransferError("database session schema is invalid")
        if (
            recorded_project_id == project_id
            and (
                project_id != "global"
                or session_directory_matches[(recorded_project_id, directory)]
            )
        ) or (
            project_id != "global"
            and recorded_project_id == "global"
            and session_directory_matches[(recorded_project_id, directory)]
        ):
            session_ids.append(session_id)
    if len(set(session_ids)) != len(session_ids):
        raise TransferError("database session schema is invalid")
    return session_ids


def _prepare_session_selection(
    connection: sqlite3.Connection, session_ids: list[str]
) -> None:
    """Populate one connection-local table for set-based session operations."""
    connection.execute(
        f"CREATE TEMP TABLE {_quote(_SELECTION_TABLE)} (id TEXT PRIMARY KEY)"
    )
    connection.executemany(
        f"INSERT INTO temp.{_quote(_SELECTION_TABLE)} VALUES (?)",
        ((session_id,) for session_id in session_ids),
    )


def _validate_import_ownership(
    connection: sqlite3.Connection, target_project_id: str, target_directory: Path
) -> None:
    """Refuse imported IDs that would replace another target project's session."""
    rows = connection.execute(
        f"SELECT session.project_id, session.directory FROM session "
        f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected ON selected.id = session.id"
    ).fetchall()
    for project_id, directory in rows:
        if project_id == target_project_id and project_id != "global":
            continue
        if project_id == "global" and _same_directory(directory, target_directory):
            continue
        raise TransferError("imported session ID belongs to another project")


def _validate_session_parents(
    connection: sqlite3.Connection, *, selected: bool
) -> None:
    """Require every transferred session parent to remain in transfer scope."""
    if "parent_id" not in _columns(connection, "session"):
        return
    child_scope = (
        f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_child "
        "ON selected_child.id = child.id "
        if selected
        else ""
    )
    parent_scope = (
        f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_parent "
        "ON selected_parent.id = parent.id "
        if selected
        else ""
    )
    outside = connection.execute(
        f"SELECT 1 FROM session AS child {child_scope}"
        "WHERE child.parent_id IS NOT NULL AND NOT EXISTS ("
        f"SELECT 1 FROM session AS parent {parent_scope}"
        "WHERE parent.id = child.parent_id) LIMIT 1"
    ).fetchone()
    if outside is not None:
        raise TransferError("transferred session parent is outside scope")


def _validate_transferred_foreign_keys(
    connection: sqlite3.Connection, *, selected: bool
) -> None:
    """Require every transferred-table foreign-key parent to remain in scope.

    ``selected`` checks source rows against the pending archive selection; false
    checks every archive row. Foreign keys to non-transferred tables, such as
    ``session.project_id``, are intentionally outside transfer scope.
    """
    for child_table in SESSION_TABLES:
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quote(child_table)})"
        ).fetchall()
        grouped: dict[tuple[object, str], list[tuple[str, str]]] = {}
        for foreign_key in foreign_keys:
            if len(foreign_key) < 5:
                raise TransferError("database session schema is invalid")
            key_id, sequence, parent_table, child_column, parent_column = foreign_key[
                :5
            ]
            if (
                type(key_id) is not int
                or type(sequence) is not int
                or not isinstance(child_column, str)
                or not child_column
            ):
                raise TransferError("database session schema is invalid")
            parent_table = _canonical_session_table_name(parent_table)
            if parent_table is None:
                continue
            if not isinstance(parent_column, str) or not parent_column:
                raise TransferError("database session schema is invalid")
            grouped.setdefault((key_id, parent_table), []).append(
                (child_column, parent_column)
            )
        for (_key_id, parent_table), columns in grouped.items():
            child_selector = SESSION_SELECTOR_COLUMNS[child_table]
            parent_selector = SESSION_SELECTOR_COLUMNS[parent_table]
            child_scope = (
                f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_child "
                f"ON selected_child.id = child.{_quote(child_selector)} "
                if selected
                else ""
            )
            parent_scope = (
                f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected_parent "
                f"ON selected_parent.id = parent.{_quote(parent_selector)} "
                if selected
                else ""
            )
            populated = " AND ".join(
                f"child.{_quote(child_column)} IS NOT NULL"
                for child_column, _parent_column in columns
            )
            equal = " AND ".join(
                f"parent.{_quote(parent_column)} = child.{_quote(child_column)}"
                for child_column, parent_column in columns
            )
            outside = connection.execute(
                f"SELECT 1 FROM {_quote(child_table)} AS child {child_scope}"
                f"WHERE {populated} AND NOT EXISTS ("
                f"SELECT 1 FROM {_quote(parent_table)} AS parent {parent_scope}"
                f"WHERE {equal}) LIMIT 1"
            ).fetchone()
            if outside is not None:
                raise TransferError("transferred foreign key parent is outside scope")


def _write_archive(
    source: sqlite3.Connection,
    temporary: Path,
    export_dir: Path,
    project_id: str,
) -> None:
    """Populate one temporary SQLite archive with rows and scalar values unchanged."""
    archive = sqlite3.connect(f"{temporary.as_uri()}?mode=rw", uri=True)
    try:
        archive.execute("PRAGMA foreign_keys = OFF")
        archive.execute(
            f"CREATE TABLE {_quote(ARCHIVE_METADATA_TABLE)} "
            "(schema_version INTEGER NOT NULL, export_dir TEXT NOT NULL, "
            "source_project_id TEXT NOT NULL)"
        )
        archive.execute(
            f"INSERT INTO {_quote(ARCHIVE_METADATA_TABLE)} VALUES (?, ?, ?)",
            (ARCHIVE_SCHEMA_VERSION, str(export_dir), project_id),
        )
        definitions = _table_names(source)
        for table in SESSION_TABLES:
            definition = definitions.get(table)
            if definition is None:
                raise TransferError("database session schema is invalid")
            archive.execute(definition)
            columns = _columns(source, table)
            _copy_selected_rows(source, archive, table, columns)
        archive.commit()
    except sqlite3.Error as error:
        archive.rollback()
        raise TransferOperationalError(
            "session export archive could not be created"
        ) from error
    finally:
        archive.close()


def _copy_selected_rows(
    connection: sqlite3.Connection,
    archive: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    """Stream selected source rows into an archive in bounded batches."""
    predicate_column = SESSION_SELECTOR_COLUMNS[table]
    column_sql = ", ".join(f"source.{_quote(column)}" for column in columns)
    cursor = connection.execute(
        f"SELECT {column_sql} FROM {_quote(table)} AS source "
        f"JOIN temp.{_quote(_SELECTION_TABLE)} AS selected "
        f"ON selected.id = source.{_quote(predicate_column)}"
    )
    while rows := cursor.fetchmany(_COPY_BATCH_SIZE):
        _insert_rows(archive, table, rows)


def _copy_archive_rows(
    archive: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    *,
    target_project_id: str,
    target_directory: str,
    target_path: str | None,
) -> None:
    """Stream one archive table into the target with session remapping."""
    columns = _columns(target, table)
    cursor = archive.execute(f"SELECT * FROM {_quote(table)}")
    while rows := cursor.fetchmany(_COPY_BATCH_SIZE):
        if table == "session":
            rows = _remap_sessions(
                rows,
                columns,
                target_project_id,
                target_directory,
                target_path,
            )
        _insert_rows(target, table, rows)


def _insert_rows(
    connection: sqlite3.Connection, table: str, rows: list[tuple[object, ...]]
) -> None:
    """Insert dynamic full-row values into a compatible table without conversion."""
    if not rows:
        return
    placeholders = ", ".join("?" for _value in rows[0])
    connection.executemany(f"INSERT INTO {_quote(table)} VALUES ({placeholders})", rows)


def _read_archive(
    archive: sqlite3.Connection,
) -> tuple[list[str], dict[str, tuple[object, ...]]]:
    """Validate one closed archive and return bounded transfer metadata."""
    _validate_session_schema(archive, archive=True)
    objects = archive.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if any(
        object_type in {"view", "trigger"}
        or object_type == "index"
        and definition is not None
        or object_type not in {"table", "index"}
        for object_type, _name, definition in objects
    ):
        raise TransferError("archive schema is invalid")
    metadata_columns = _columns(archive, ARCHIVE_METADATA_TABLE)
    if metadata_columns != ("schema_version", "export_dir", "source_project_id"):
        raise TransferError("archive schema is invalid")
    metadata_rows = archive.execute(
        f"SELECT schema_version, export_dir, source_project_id "
        f"FROM {_quote(ARCHIVE_METADATA_TABLE)}"
    ).fetchall()
    if (
        len(metadata_rows) != 1
        or type(metadata_rows[0][0]) is not int
        or metadata_rows[0][0] != ARCHIVE_SCHEMA_VERSION
        or not isinstance(metadata_rows[0][1], str)
        or not os.path.isabs(metadata_rows[0][1])
        or not isinstance(metadata_rows[0][2], str)
    ):
        raise TransferError("archive schema is invalid")
    signatures: dict[str, tuple[object, ...]] = {}
    raw_session_ids = archive.execute("SELECT id FROM session").fetchall()
    session_ids = [row[0] for row in raw_session_ids if isinstance(row[0], str)]
    if len(session_ids) != len(raw_session_ids) or len(set(session_ids)) != len(
        session_ids
    ):
        raise TransferError("archive schema is invalid")
    for table in SESSION_TABLES:
        signatures[table] = _table_signature(archive, table)
        if table == "session":
            continue
        selector = SESSION_SELECTOR_COLUMNS[table]
        orphan = archive.execute(
            f"SELECT 1 FROM {_quote(table)} AS owned "
            f"LEFT JOIN session AS session "
            f"ON session.id = owned.{_quote(selector)} "
            "WHERE session.id IS NULL LIMIT 1"
        ).fetchone()
        if orphan is not None:
            raise TransferError("archive schema is invalid")
    _validate_session_parents(archive, selected=False)
    _validate_transferred_foreign_keys(archive, selected=False)
    return session_ids, signatures


def _table_signature(connection: sqlite3.Connection, table: str) -> tuple[object, ...]:
    """Return the full column and foreign-key compatibility signature for one table."""
    columns = tuple(
        connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    )
    foreign_keys = tuple(
        connection.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall()
    )
    return columns, foreign_keys


def _validate_destination_compatibility(
    target: sqlite3.Connection, archive_signatures: dict[str, tuple[object, ...]]
) -> None:
    """Require destination known tables to exactly match archive table layouts."""
    for table in SESSION_TABLES:
        if _table_signature(target, table) != archive_signatures[table]:
            raise TransferError("destination session schema is incompatible")


def _target_session_path(target_dir: Path, worktree: str | None) -> str | None:
    """Return the session path relative to a recorded absolute project worktree."""
    if worktree and os.path.isabs(worktree):
        relative = os.path.relpath(target_dir, Path(worktree).resolve())
        return "" if relative == "." else Path(relative).as_posix()
    return None


def _remap_sessions(
    rows: list[tuple[object, ...]],
    columns: tuple[str, ...],
    project_id: str,
    directory: str,
    path: str | None,
) -> list[tuple[object, ...]]:
    """Remap imported sessions while preserving every unrelated scalar/blob column."""
    project_index = columns.index("project_id")
    directory_index = columns.index("directory")
    workspace_index = (
        columns.index("workspace_id") if "workspace_id" in columns else None
    )
    path_index = columns.index("path") if "path" in columns else None
    remapped: list[tuple[object, ...]] = []
    for row in rows:
        mutable = list(row)
        mutable[project_index] = project_id
        mutable[directory_index] = directory
        if workspace_index is not None:
            mutable[workspace_index] = None
        if path_index is not None:
            mutable[path_index] = path
        remapped.append(tuple(mutable))
    return remapped


def _new_temporary_file(directory: Path) -> Path:
    """Create one mode-0600 temporary output file in the final archive directory."""
    descriptor, name = tempfile.mkstemp(
        prefix=".opencode-db-transfer-", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    path = Path(name)
    os.chmod(path, 0o600)
    return path


def _archive_name() -> str:
    """Return one non-content-bearing archive filename with collision-resistant entropy."""
    return f"{new_artifact_id('opencode-session')}.sqlite"


def _sync_file(path: Path) -> None:
    """Synchronize one published archive file before reporting successful export."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Synchronize archive directory metadata after atomic archive publication."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quote(identifier: str) -> str:
    """Quote one SQLite identifier obtained from a fixed protocol table set."""
    return '"' + identifier.replace('"', '""') + '"'
