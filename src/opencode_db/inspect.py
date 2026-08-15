"""Render bounded, read-only OpenCode project and session inspection views.

The public functions in this module open only an existing selected SQLite file
through ``mode=ro``. They validate only the table columns needed for the view,
never migrate or checkpoint the database, and expose transcript text only for
the explicitly selected session.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import stat

from .target import TargetError, select_default_database
from .prune import (
    PruneError,
    project_logical_bytes,
    project_logical_sizes,
    session_logical_sizes,
    validate_session_schema,
)


class InspectionError(RuntimeError):
    """Report a bounded safety refusal without exposing database row contents.

    Parameters: ``message`` is a fixed public classification. The exception is
    used for invalid selections, schemas, and persisted values; construction and
    handling do not alter the selected database.
    """

    def __init__(self, message: str) -> None:
        """Store one fixed, bounded public inspection diagnostic."""
        super().__init__(message[:256])


class InspectionOperationalError(InspectionError):
    """Report a bounded failure while opening or reading a selected database.

    This subtype separates SQLite and filesystem failures from inspection safety
    refusals. It does not contain SQLite exception text or private row values.
    """


@dataclass(frozen=True)
class InspectionRequest:
    """Represent one grammar-validated read-only inspection selection.

    ``command`` is one of the public inspection commands. ``database`` is
    an optional syntactically absolute explicit path; its default resolution and
    existing-file validation occur only when executing the request. Exactly one
    matching identifier field is present for detail commands.
    ``estimate_project_size`` enables project logical-size estimates only for
    ``list-projects`` and ``show-project``; ``estimate_session_size`` enables
    per-session logical-size estimates only for ``show-session`` and
    ``list-sessions``. Both flags default to ``False``. Any enabled estimate
    requires the complete known session schema. The value has no filesystem or
    SQLite side effects.
    """

    command: str
    database: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    estimate_project_size: bool = False
    estimate_session_size: bool = False


@dataclass(frozen=True)
class _SessionRows:
    """Hold one validated session's transcript families for consistent rendering."""

    v2: list[dict[str, object]]
    legacy: list[dict[str, object]]
    inputs: list[dict[str, object]]


_SESSION_METADATA_FIELDS = (
    "project_id",
    "workspace_id",
    "parent_id",
    "slug",
    "directory",
    "path",
    "title",
    "version",
    "agent",
    "cost",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "time_created",
    "time_updated",
    "time_compacting",
    "time_archived",
)


def resolve_database_path(explicit: str | None) -> Path:
    """Return one canonical existing regular inspection database path.

    Parameters: ``explicit`` is an absolute path supplied by the operator or
    ``None`` for the documented XDG/HOME default. Returns a resolved absolute
    regular file. Raises :class:`InspectionError` when an environment base or
    selection is unavailable or unsafe, and :class:`InspectionOperationalError`
    for an otherwise valid path that cannot be inspected. No file is created.
    """
    if explicit is None:
        try:
            selected = Path(select_default_database())
        except TargetError as error:
            raise InspectionError("default database path is unavailable") from error
    else:
        selected = Path(explicit)
    if "\x00" in os.fspath(selected) or not selected.is_absolute():
        raise InspectionError("database must be an absolute regular file")
    try:
        mode = selected.stat().st_mode
    except FileNotFoundError as error:
        raise InspectionError("database must be an existing regular file") from error
    except OSError as error:
        raise InspectionOperationalError("database could not be inspected") from error
    if not stat.S_ISREG(mode):
        raise InspectionError("database must be an existing regular file")
    try:
        return selected.resolve()
    except OSError as error:
        raise InspectionOperationalError("database could not be inspected") from error


def inspect_database(
    database: str | Path | None,
    command: str,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    estimate_project_size: bool = False,
    estimate_session_size: bool = False,
) -> str:
    """Render one selected inspection view from an existing read-only database.

    Parameters: ``database`` is an existing absolute regular file, ``command``
    is ``list-projects``, ``show-project``, ``show-session``, or
    ``list-sessions``; detail commands require their exact identifier. Optional
    estimate flags require the complete currently known session schema. Returns stable human-readable
    sections ending in a newline. Raises :class:`InspectionError` for malformed
    schemas/data or ambiguous/missing exact rows and
    :class:`InspectionOperationalError` for SQLite read failures. The function
    opens SQLite only with ``mode=ro`` and does not mutate, checkpoint, migrate,
    inspect processes, or invoke OpenCode.
    """
    path = resolve_database_path(None if database is None else os.fspath(database))
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        tables = _tables(connection)
        _require_table(tables, "project")
        _require_columns(connection, "project", {"id"})
        _validate_database(connection)
        project_sizes: dict[str, int] = {}
        session_sizes: dict[str, int] = {}
        if estimate_project_size or estimate_session_size:
            validate_session_schema(connection)
        if estimate_project_size:
            if command == "show-project" and project_id is not None:
                project_sizes[project_id] = project_logical_bytes(
                    connection, project_id
                )
            else:
                project_sizes = project_logical_sizes(connection)
        if estimate_session_size:
            session_sizes = session_logical_sizes(
                connection,
                project_id=project_id if command == "list-sessions" else None,
                session_id=session_id if command == "show-session" else None,
            )
        if command == "list-projects":
            lines = [f"database: {path}"]
            project_rows = _project_rows(connection)
            for row in project_rows:
                lines.extend(("", "project:"))
                lines.extend(
                    _render_project(
                        connection,
                        row,
                        include_sessions=False,
                        estimated_project_bytes=project_sizes.get(_string(row, "id")),
                    )
                )
            return "\n".join(lines) + "\n"
        if command == "show-project" and project_id is not None:
            project = _one_project(connection, project_id)
            lines = [f"database: {path}", "", "project:"]
            lines.extend(
                _render_project(
                    connection,
                    project,
                    include_sessions=True,
                    estimated_project_bytes=project_sizes.get(project_id),
                )
            )
            return "\n".join(lines) + "\n"
        if command == "show-session" and session_id is not None:
            session = _one_session(connection, session_id)
            project = _one_project(connection, _string(session, "project_id"))
            transcript_rows = _session_rows(connection, _string(session, "id"))
            lines = [f"database: {path}", "", "session:"]
            lines.extend(_render_session_metadata(session))
            lines.extend(("", "project:"))
            lines.extend(_render_project(connection, project, include_sessions=False))
            lines.extend(("", "counts:"))
            lines.extend(_render_row_counts(transcript_rows))
            if estimate_session_size:
                lines.append(
                    f"estimated_session_logical_bytes: {session_sizes[_string(session, 'id')]}"
                )
            lines.extend(
                _render_transcripts(
                    connection, _string(session, "id"), transcript_rows
                )
            )
            return "\n".join(lines) + "\n"
        if command == "list-sessions":
            _require_table(tables, "session")
            _require_columns(connection, "session", {"id", "project_id", "time_updated"})
            if project_id is not None:
                _one_project(connection, project_id)
            sessions = _session_summary_rows(connection, project_id)
            lines = [f"database: {path}"]
            for session in sessions:
                session_id_value = _string(session, "id")
                lines.extend(("", "session:"))
                lines.extend(_render_session_metadata(session))
                if estimate_session_size:
                    lines.append(
                        f"estimated_session_logical_bytes: {session_sizes[session_id_value]}"
                    )
            return "\n".join(lines) + "\n"
        raise InspectionError("inspection command is invalid")
    except PruneError as error:
        raise InspectionError(str(error)) from error
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise InspectionOperationalError("database inspection failed") from error
    finally:
        if connection is not None:
            connection.close()


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if any(len(row) != 1 or not isinstance(row[0], str) for row in rows):
        raise InspectionError("inspection schema is malformed")
    return {row[0] for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = tuple(row[1] for row in rows if len(row) > 1 and isinstance(row[1], str))
    if not columns or len(columns) != len(rows) or len(columns) != len(set(columns)):
        raise InspectionError("inspection schema is malformed")
    return columns


def _require_table(tables: set[str], table: str) -> None:
    if table not in tables:
        raise InspectionError("inspection schema is incomplete")


def _require_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> tuple[str, ...]:
    columns = _columns(connection, table)
    if not required <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    return columns


def _validate_database(connection: sqlite3.Connection) -> None:
    """Refuse a database whose integrity or declared foreign keys are invalid."""
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise InspectionError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InspectionError("database foreign key check failed")


def _project_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    columns = _columns(connection, "project")
    rows = _rows(connection, "project", columns, "id")
    project_ids = [_string(row, "id") for row in rows]
    if len(project_ids) != len(set(project_ids)):
        raise InspectionError("inspection schema is malformed")
    return rows


def _one_project(connection: sqlite3.Connection, project_id: str) -> dict[str, object]:
    columns = _columns(connection, "project")
    rows = _rows(connection, "project", columns, "id", "WHERE id = ?", (project_id,))
    if not rows:
        raise InspectionError("project ID was not found")
    if len(rows) != 1:
        raise InspectionError("project ID is ambiguous")
    _string(rows[0], "id")
    return rows[0]


def _one_session(connection: sqlite3.Connection, session_id: str) -> dict[str, object]:
    _require_table(_tables(connection), "session")
    columns = _require_columns(connection, "session", {"id", "project_id"})
    rows = _rows(connection, "session", columns, "id", "WHERE id = ?", (session_id,))
    if not rows:
        raise InspectionError("session ID was not found")
    if len(rows) != 1:
        raise InspectionError("session ID is ambiguous")
    _string(rows[0], "id")
    _string(rows[0], "project_id")
    return rows[0]


def _rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    order: str,
    where: str = "",
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    selected = ", ".join(_quote(column) for column in columns)
    records = connection.execute(
        f"SELECT {selected} FROM {_quote(table)} {where} ORDER BY {_quote(order)}", parameters
    ).fetchall()
    if any(len(record) != len(columns) for record in records):
        raise InspectionError("inspection data is malformed")
    return [dict(zip(columns, record, strict=True)) for record in records]


def _render_project(
    connection: sqlite3.Connection,
    project: dict[str, object],
    *,
    include_sessions: bool,
    estimated_project_bytes: int | None = None,
) -> list[str]:
    project_id = _string(project, "id")
    lines = [f"project_id: {_display(project_id)}"]
    if estimated_project_bytes is not None:
        lines.append(
            f"estimated_project_logical_bytes: {estimated_project_bytes}"
        )
    for name in ("worktree", "vcs", "name", "time_created", "time_updated", "time_initialized"):
        if name in project:
            value = _absolute(project, name) if name == "worktree" else _safe_scalar(project[name])
            lines.append(f"{name}: {_display(value)}")
    if "sandboxes" in project:
        lines.extend(_render_sandboxes(project["sandboxes"]))
    lines.extend(_render_project_directories(connection, project_id))
    lines.extend(_render_workspace_directories(connection, project_id))
    lines.extend(_render_session_directories(connection, project_id))
    if include_sessions:
        lines.extend(_render_project_sessions(connection, project_id))
    return lines


def _render_sandboxes(value: object) -> list[str]:
    if not isinstance(value, str):
        raise InspectionError("inspection data is malformed")
    decoded = _json_object_or_array(value)
    if not isinstance(decoded, list) or not all(isinstance(item, str) and os.path.isabs(item) for item in decoded):
        raise InspectionError("inspection data is malformed")
    return [f"sandbox: {_display(item)}" for item in sorted(decoded)]


def _render_project_directories(connection: sqlite3.Connection, project_id: str) -> list[str]:
    if "project_directory" not in _tables(connection):
        return []
    columns = _columns(connection, "project_directory")
    if not {"project_id", "directory"} <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    selected = tuple(name for name in ("project_id", "directory", "type", "strategy") if name in columns)
    rows = _rows(connection, "project_directory", selected, "directory", "WHERE project_id = ?", (project_id,))
    output: list[str] = []
    for row in rows:
        if _string(row, "project_id") != project_id:
            raise InspectionError("inspection data is malformed")
        directory = _absolute(row, "directory")
        values = [directory]
        values.extend(_safe_scalar(row[name]) for name in ("type", "strategy") if name in row)
        output.append("project_directory: " + " | ".join(_display(value) for value in values))
    return output


def _render_workspace_directories(connection: sqlite3.Connection, project_id: str) -> list[str]:
    if "workspace" not in _tables(connection):
        return []
    columns = _columns(connection, "workspace")
    if not {"project_id", "directory"} <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    rows = _rows(
        connection,
        "workspace",
        ("project_id", "directory"),
        "directory",
        "WHERE project_id = ?",
        (project_id,),
    )
    counts: dict[str, int] = {}
    for row in rows:
        if _string(row, "project_id") != project_id:
            raise InspectionError("inspection data is malformed")
        directory = _safe_scalar(row["directory"])
        if directory != "none" and not os.path.isabs(directory):
            raise InspectionError("inspection data is malformed")
        counts[directory] = counts.get(directory, 0) + 1
    return [f"workspace_directory: {_display(directory)} | {count}" for directory, count in sorted(counts.items())]


def _render_session_directories(connection: sqlite3.Connection, project_id: str) -> list[str]:
    if "session" not in _tables(connection):
        return []
    columns = _columns(connection, "session")
    if not {"id", "project_id", "directory"} <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    rows = _rows(connection, "session", ("id", "project_id", "directory"), "id", "WHERE project_id = ?", (project_id,))
    counts: dict[str, int] = {}
    for row in rows:
        _string(row, "id")
        if _string(row, "project_id") != project_id:
            raise InspectionError("inspection data is malformed")
        directory = _absolute(row, "directory")
        counts[directory] = counts.get(directory, 0) + 1
    return [f"session_directory: {_display(directory)} | {count}" for directory, count in sorted(counts.items())]


def _render_project_sessions(connection: sqlite3.Connection, project_id: str) -> list[str]:
    if "session" not in _tables(connection):
        return []
    columns = _require_columns(connection, "session", {"id", "project_id"})
    rows = _rows(connection, "session", columns, "id", "WHERE project_id = ?", (project_id,))
    lines: list[str] = []
    for row in rows:
        if _string(row, "project_id") != project_id:
            raise InspectionError("inspection data is malformed")
        metadata = _render_session_metadata(row)
        lines.extend(("", "session:"))
        lines.append(metadata[0])
        lines.extend(_render_counts(connection, _string(row, "id")))
        lines.extend(metadata[1:])
    return lines


def _session_summary_rows(
    connection: sqlite3.Connection, project_id: str | None
) -> list[dict[str, object]]:
    """Return deterministic newest-first session metadata for a list-only view."""
    available = set(
        _require_columns(connection, "session", {"id", "project_id", "time_updated"})
    )
    columns = ("id",) + tuple(
        name for name in _SESSION_METADATA_FIELDS if name in available
    )
    where = "WHERE project_id = ?" if project_id is not None else ""
    parameters: tuple[object, ...] = (project_id,) if project_id is not None else ()
    selected = ", ".join(_quote(column) for column in columns)
    records = connection.execute(
        f"SELECT {selected} FROM session {where} ORDER BY time_updated DESC, id ASC", parameters
    ).fetchall()
    if any(len(record) != len(columns) for record in records):
        raise InspectionError("inspection data is malformed")
    rows = [dict(zip(columns, record, strict=True)) for record in records]
    for row in rows:
        _string(row, "id")
        _string(row, "project_id")
        if type(row["time_updated"]) is not int:
            raise InspectionError("inspection data is malformed")
    return rows


def _render_session_metadata(session: dict[str, object]) -> list[str]:
    lines = [f"session_id: {_display(_string(session, 'id'))}"]
    for name in _SESSION_METADATA_FIELDS:
        if name in session:
            value = _absolute(session, name) if name == "directory" else _safe_scalar(session[name])
            lines.append(f"{name}: {_display(value)}")
    return lines


def _render_counts(connection: sqlite3.Connection, session_id: str) -> list[str]:
    return _render_row_counts(_session_rows(connection, session_id))


def _session_rows(connection: sqlite3.Connection, session_id: str) -> _SessionRows:
    """Load and validate each transcript family once for one exact session."""
    rows = _SessionRows(
        v2=_v2_rows(connection, session_id),
        legacy=_legacy_rows(connection, session_id),
        inputs=_input_rows(connection, session_id),
    )
    _validate_promoted_inputs(rows)
    return rows


def _validate_promoted_inputs(rows: _SessionRows) -> None:
    """Require each promoted inbox row to match its visible V2 user projection."""
    messages = {_string(row, "id"): row for row in rows.v2}
    for input_row in rows.inputs:
        promoted = input_row["promoted_seq"]
        if promoted is None:
            continue
        message = messages.get(_string(input_row, "id"))
        if (
            message is None
            or message["type"] != "user"
            or message["seq"] != promoted
        ):
            raise InspectionError("inspection data is malformed")
        prompt = _json_dict(input_row["prompt"])
        data = _json_dict(message["data"])
        if any(prompt.get(name) != data.get(name) for name in ("text", "files", "agents")):
            raise InspectionError("inspection data is malformed")


def _render_row_counts(rows: _SessionRows) -> list[str]:
    return [
        f"v2_turns: {sum(row['type'] in {'user', 'assistant'} for row in rows.v2)}",
        f"legacy_turns: {sum(_json_dict(row['data']).get('role') in {'user', 'assistant'} for row in rows.legacy)}",
        f"pending_inputs: {sum(row['promoted_seq'] is None for row in rows.inputs)}",
    ]


def _v2_rows(connection: sqlite3.Connection, session_id: str) -> list[dict[str, object]]:
    if "session_message" not in _tables(connection):
        return []
    columns = _columns(connection, "session_message")
    required = {"id", "session_id", "type", "seq", "data"}
    if not required <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    rows = _rows(connection, "session_message", tuple(sorted(required)), "seq", "WHERE session_id = ?", (session_id,))
    previous: int | None = None
    ids: set[str] = set()
    for row in rows:
        if _string(row, "session_id") != session_id or not isinstance(row["type"], str) or type(row["seq"]) is not int:
            raise InspectionError("inspection data is malformed")
        if previous is not None and row["seq"] <= previous:
            raise InspectionError("inspection data is malformed")
        previous = row["seq"]
        message_id = _string(row, "id")
        if message_id in ids:
            raise InspectionError("inspection data is malformed")
        ids.add(message_id)
        if not isinstance(row["data"], str):
            raise InspectionError("inspection data is malformed")
    return rows


def _legacy_rows(connection: sqlite3.Connection, session_id: str) -> list[dict[str, object]]:
    if "message" not in _tables(connection):
        return []
    columns = _columns(connection, "message")
    required = {"id", "session_id", "time_created", "data"}
    if not required <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    selected = tuple(sorted(required))
    query = f"SELECT {', '.join(_quote(name) for name in selected)} FROM message WHERE session_id = ? ORDER BY time_created, id"
    records = connection.execute(query, (session_id,)).fetchall()
    rows = [dict(zip(selected, record, strict=True)) for record in records]
    for row in rows:
        if _string(row, "session_id") != session_id or type(row["time_created"]) is not int:
            raise InspectionError("inspection data is malformed")
        _string(row, "id")
        _json_dict(row.get("data"))
    return rows


def _input_rows(connection: sqlite3.Connection, session_id: str) -> list[dict[str, object]]:
    if "session_input" not in _tables(connection):
        return []
    columns = _columns(connection, "session_input")
    required = {"id", "session_id", "prompt", "promoted_seq"}
    if not required <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    if "admitted_seq" in columns:
        order = "admitted_seq"
    elif "seq" in columns:
        order = "seq"
    else:
        order = "id"
    selected = tuple(sorted(required | {order}))
    rows = _rows(connection, "session_input", selected, order, "WHERE session_id = ?", (session_id,))
    for row in rows:
        if _string(row, "session_id") != session_id:
            raise InspectionError("inspection data is malformed")
        _string(row, "id")
        if order != "id" and type(row[order]) is not int:
            raise InspectionError("inspection data is malformed")
        if row["promoted_seq"] is not None and type(row["promoted_seq"]) is not int:
            raise InspectionError("inspection data is malformed")
        _prompt_text(row["prompt"])
    return rows


def _render_transcripts(
    connection: sqlite3.Connection, session_id: str, rows: _SessionRows
) -> list[str]:
    lines: list[str] = []
    if rows.v2:
        lines.extend(("", "v2_transcript:"))
        for row in rows.v2:
            lines.extend(_render_v2_record(_string(row, "type"), _json_dict(row["data"])))
    parts = _legacy_parts(connection, session_id)
    if rows.legacy:
        lines.extend(("", "legacy_transcript:"))
        for message in rows.legacy:
            data = _json_dict(message["data"])
            role = data.get("role")
            if role in {"user", "assistant"}:
                lines.append(f"{role}:")
                lines.extend(_render_legacy_parts(parts.get(_string(message, "id"), [])))
            elif not isinstance(role, str):
                raise InspectionError("inspection data is malformed")
            else:
                lines.append(f"legacy_{_display(role)}: present")
    pending = [row for row in rows.inputs if row["promoted_seq"] is None]
    if pending:
        lines.extend(("", "pending_inputs:"))
        lines.extend(f"pending_input: {_display(_prompt_text(row['prompt']))}" for row in pending)
    return lines


def _render_v2_record(record_type: str, data: dict[str, object]) -> list[str]:
    if record_type in {"user", "system"}:
        return [f"{record_type}: {_display(_text(data, 'text'))}"]
    if record_type == "assistant":
        content = data.get("content")
        if not isinstance(content, list):
            raise InspectionError("inspection data is malformed")
        lines: list[str] = []
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise InspectionError("inspection data is malformed")
            if item["type"] == "text":
                lines.append(f"assistant: {_display(_text(item, 'text'))}")
            elif item["type"] == "reasoning":
                lines.append("reasoning: present")
            elif item["type"] == "tool":
                state = item.get("state")
                if not isinstance(state, dict) or not isinstance(state.get("status"), str):
                    raise InspectionError("inspection data is malformed")
                lines.append(f"tool: {_display(_string(item, 'name'))} | {_display(state['status'])}")
            else:
                lines.append(f"assistant_{_display(item['type'])}: present")
        return lines or ["assistant: present"]
    if record_type == "shell":
        if not isinstance(data.get("command"), str) or not isinstance(data.get("output"), str):
            raise InspectionError("inspection data is malformed")
        return ["shell: present"]
    if record_type == "compaction":
        if not isinstance(data.get("reason"), str) or not isinstance(data.get("summary"), str):
            raise InspectionError("inspection data is malformed")
        return ["compaction: present"]
    if record_type in {"synthetic", "agent-switched", "model-switched"}:
        return [f"{record_type}: present"]
    return [f"v2_{_display(record_type)}: present"]


def _legacy_parts(
    connection: sqlite3.Connection, session_id: str
) -> dict[str, list[dict[str, object]]]:
    tables = _tables(connection)
    if "part" not in tables:
        if "message" in tables:
            raise InspectionError("inspection schema is incomplete")
        return {}
    if "message" not in tables:
        raise InspectionError("inspection schema is incomplete")
    columns = _columns(connection, "part")
    required = {"id", "message_id", "session_id", "time_created", "data"}
    if not required <= set(columns):
        raise InspectionError("inspection schema is incomplete")
    selected = tuple(sorted(required))
    selected_sql = ", ".join(f"part.{_quote(name)}" for name in selected)
    records = connection.execute(
        f"SELECT {selected_sql}, parent.session_id "
        "FROM part AS part LEFT JOIN message AS parent ON parent.id = part.message_id "
        "WHERE part.session_id = ? OR parent.session_id = ? "
        "ORDER BY part.message_id, part.id",
        (session_id, session_id),
    ).fetchall()
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        if len(record) != len(selected) + 1:
            raise InspectionError("inspection data is malformed")
        row = dict(zip(selected, record[:-1], strict=True))
        message_id = _string(row, "message_id")
        if (
            _string(row, "session_id") != session_id
            or record[-1] != session_id
            or type(row["time_created"]) is not int
        ):
            raise InspectionError("inspection data is malformed")
        _string(row, "id")
        _json_dict(row["data"])
        grouped.setdefault(message_id, []).append(row)
    return grouped


def _render_legacy_parts(rows: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        data = _json_dict(row["data"])
        part_type = data.get("type")
        if not isinstance(part_type, str):
            raise InspectionError("inspection data is malformed")
        if part_type == "text":
            lines.append(f"text: {_display(_text(data, 'text'))}")
        elif part_type == "tool":
            lines.append(f"tool: {_display(_string(data, 'tool'))}")
        elif part_type == "reasoning":
            lines.append("reasoning: present")
        else:
            lines.append(f"legacy_{_display(part_type)}: present")
    return lines


def _json_object_or_array(value: str) -> object:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise InspectionError("inspection data is malformed") from error


def _json_dict(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise InspectionError("inspection data is malformed")
    decoded = _json_object_or_array(value)
    if not isinstance(decoded, dict):
        raise InspectionError("inspection data is malformed")
    return decoded


def _prompt_text(value: object) -> str:
    return _text(_json_dict(value), "text")


def _text(row: dict[str, object], name: str) -> str:
    """Return a required textual transcript field, including an empty string."""
    value = row.get(name)
    if not isinstance(value, str):
        raise InspectionError("inspection data is malformed")
    return value


def _string(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise InspectionError("inspection data is malformed")
    return value


def _absolute(row: dict[str, object], name: str) -> str:
    value = _string(row, name)
    if not os.path.isabs(value):
        raise InspectionError("inspection data is malformed")
    return value


def _safe_scalar(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise InspectionError("inspection data is malformed")
        return str(value)
    raise InspectionError("inspection data is malformed")


def _display(value: object) -> str:
    scalar = _safe_scalar(value)
    output: list[str] = []
    for character in scalar:
        codepoint = ord(character)
        if character == "\\":
            output.append("\\\\")
        elif character == "\r":
            output.append("\\r")
        elif character == "\n":
            output.append("\\n")
        elif character == "\t":
            output.append("\\t")
        elif codepoint < 32 or codepoint == 127 or 128 <= codepoint <= 159:
            output.append(f"\\x{codepoint:02x}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            output.append(f"\\u{codepoint:04x}")
        else:
            output.append(character)
    return "".join(output)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
