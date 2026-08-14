"""Closed SQLite schema inspection for sibling move planning and application.

This internal module only reads SQLite metadata. It rejects storage shapes whose
constraints or references could make the move's fixed location updates unsafe.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


def validate_schema(
    connection: sqlite3.Connection,
    *,
    move_error: type[Exception],
    quote: Callable[[str], str],
    max_schema_objects: int,
    max_fingerprint_bytes: int,
    max_scalar_bytes: int,
    check_deadline: Callable[[], None],
) -> tuple[tuple[str, str, str, str | None], ...]:
    """Return a bounded fingerprint only for the closed supported move schema."""
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name LIMIT ?",
        (max_schema_objects + 1,),
    )
    fingerprint: list[tuple[str, str, str, str | None]] = []
    fingerprint_bytes = 0
    for row in objects:
        check_deadline()
        if len(fingerprint) >= max_schema_objects:
            raise move_error("move schema object limit exceeded")
        if (
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:3])
            or row[3] is not None and not isinstance(row[3], str)
        ):
            raise move_error("move schema is malformed")
        for identifier in row[:3]:
            _identifier(identifier, "schema identifier", move_error, max_scalar_bytes)
        fingerprint_bytes += sum(_fingerprint_scalar_size(value, move_error, max_scalar_bytes) for value in row)
        if fingerprint_bytes > max_fingerprint_bytes:
            raise move_error("move schema fingerprint limit exceeded")
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
        raise move_error("move schema is incomplete")
    primary_keys = {
        "project": ("id",),
        "project_directory": ("project_id", "directory"),
        "session": ("id",),
        "workspace": ("id",),
    }
    for table, columns in required.items():
        check_deadline()
        metadata = _table_columns(connection, table, move_error, quote, max_schema_objects, check_deadline)
        for name, declared_type, required_not_null in columns:
            column = metadata.get(name)
            if column is None or column[0] != declared_type or (required_not_null and not column[1] and not column[2]):
                raise move_error("move schema is incompatible")
        if tuple(
            name for name, metadata_value in sorted(metadata.items(), key=lambda item: item[1][2]) if metadata_value[2]
        ) != primary_keys[table]:
            raise move_error("move schema primary key is incompatible")
        if any(hidden for _type, _not_null, _position, hidden in metadata.values()):
            raise move_error("move schema has generated columns")
        _refuse_location_indexes(connection, table, move_error, quote, max_schema_objects, check_deadline)
        _require_location_foreign_keys(connection, table, move_error, quote, max_schema_objects, check_deadline)
    if any(object_type == "trigger" and table in required for object_type, _name, table, _sql in fingerprint):
        raise move_error("move schema has side-effecting triggers")
    _refuse_inbound_location_references(connection, tables, move_error, quote, max_schema_objects, check_deadline)
    for table in tables - set(required):
        check_deadline()
        columns = _table_columns(connection, table, move_error, quote, max_schema_objects, check_deadline)
        if {"project_id", "directory"} <= set(columns):
            raise move_error("move schema has unknown project directory state")
    return tuple(fingerprint)


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
    move_error: type[Exception],
    quote: Callable[[str], str],
    maximum: int,
    check_deadline: Callable[[], None],
) -> dict[str, tuple[str, bool, int, int]]:
    """Return bounded closed column metadata while refusing generated fields."""
    result: dict[str, tuple[str, bool, int, int]] = {}
    for row in connection.execute(f"PRAGMA table_xinfo({quote(table)})"):
        check_deadline()
        if len(result) >= maximum:
            raise move_error("move schema column limit exceeded")
        if len(row) < 7 or not isinstance(row[1], str) or not isinstance(row[2], str) or type(row[3]) is not int or type(row[5]) is not int or type(row[6]) is not int:
            raise move_error("move schema is malformed")
        if row[1] in result:
            raise move_error("move schema is malformed")
        result[row[1]] = (row[2].upper(), bool(row[3]), row[5], row[6])
    if not result:
        raise move_error("move schema is malformed")
    return result


def _refuse_location_indexes(
    connection: sqlite3.Connection,
    table: str,
    move_error: type[Exception],
    quote: Callable[[str], str],
    maximum: int,
    check_deadline: Callable[[], None],
) -> None:
    """Reject unique indexes involving any location column rewritten by the move."""
    index_count = 0
    for index in connection.execute(f"PRAGMA index_list({quote(table)})"):
        check_deadline()
        index_count += 1
        if index_count > maximum:
            raise move_error("move schema index limit exceeded")
        if len(index) < 4 or not isinstance(index[1], str) or type(index[2]) is not int or not isinstance(index[3], str):
            raise move_error("move schema is malformed")
        if not index[2]:
            continue
        names: list[str] = []
        for column in connection.execute(f"PRAGMA index_info({quote(index[1])})"):
            check_deadline()
            if len(names) >= maximum or len(column) <= 2 or not isinstance(column[2], str):
                raise move_error("move schema is malformed")
            names.append(column[2])
        expected_primary = table == "project_directory" and index[3] == "pk" and tuple(names) == ("project_id", "directory")
        rewritten = {"worktree", "sandboxes"} if table == "project" else {"directory"}
        if rewritten.intersection(names) and not expected_primary:
            raise move_error("move schema has unfamiliar unique location index")


def _require_location_foreign_keys(
    connection: sqlite3.Connection,
    table: str,
    move_error: type[Exception],
    quote: Callable[[str], str],
    maximum: int,
    check_deadline: Callable[[], None],
) -> None:
    """Require the current closed outbound project relationship for each location table."""
    expected = () if table == "project" else ((0, 0, "project", "project_id", "id", "NO ACTION", "CASCADE", "NONE"),)
    observed: list[tuple[object, ...]] = []
    for row in connection.execute(f"PRAGMA foreign_key_list({quote(table)})"):
        check_deadline()
        if len(observed) >= maximum:
            raise move_error("move schema foreign key limit exceeded")
        if len(row) != 8 or type(row[0]) is not int or type(row[1]) is not int or not all(isinstance(row[index], str) for index in range(2, 8)):
            raise move_error("move schema is malformed")
        observed.append(tuple(row))
    if tuple(observed) != expected:
        raise move_error("move schema has unfamiliar foreign keys")


def _refuse_inbound_location_references(
    connection: sqlite3.Connection,
    tables: set[str],
    move_error: type[Exception],
    quote: Callable[[str], str],
    maximum: int,
    check_deadline: Callable[[], None],
) -> None:
    """Refuse foreign keys targeting any parent column rewritten by the move."""
    affected = {
        "project": {"worktree", "sandboxes"},
        "project_directory": {"directory"},
        "session": {"directory"},
        "workspace": {"directory"},
    }
    for table in tables:
        foreign_key_count = 0
        for row in connection.execute(f"PRAGMA foreign_key_list({quote(table)})"):
            check_deadline()
            foreign_key_count += 1
            if foreign_key_count > maximum:
                raise move_error("move schema foreign key limit exceeded")
            if len(row) < 5 or not isinstance(row[2], str) or not isinstance(row[4], str):
                raise move_error("move schema is malformed")
            if row[2] in affected and row[4] in affected[row[2]]:
                raise move_error("move schema has inbound directory references")


def _identifier(value: object, label: str, move_error: type[Exception], maximum: int) -> str:
    """Require a bounded nonempty schema identifier without SQLite coercion."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise move_error(f"move {label} is malformed")
    if len(value.encode("utf-8", "surrogatepass")) > maximum:
        raise move_error(f"move {label} value limit exceeded")
    return value


def _fingerprint_scalar_size(value: object, move_error: type[Exception], maximum: int) -> int:
    """Return one bounded retained fingerprint scalar charge."""
    if value is None:
        return 0
    if not isinstance(value, str):
        raise move_error("move captured scalar is malformed")
    size = len(value.encode("utf-8", "surrogatepass"))
    if size > maximum:
        raise move_error("move schema fingerprint scalar limit exceeded")
    return size
