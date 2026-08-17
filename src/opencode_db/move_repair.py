"""Plan and apply fail-closed repairs for recontaminated sibling moves.

The repair domain handles a project whose primary worktree already points at a
completed sibling-move target but whose retained source checkout family was
subsequently registered again by OpenCode. It validates an explicit source and
target family, previews immutable drop/rebase actions, and applies only those
structured location changes in one bounded SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

from . import move
from .move import (
    CapturedRow,
    CapturedState,
    GitEvidence,
    MoveError,
    MoveOperationalError,
)


@dataclass(frozen=True)
class MoveRepairRequest:
    """Select one completed sibling move and its obsolete source family.

    ``database`` is an absolute existing SQLite path, ``project_id`` selects one
    project, and ``source_project_dir`` plus ``target_project_dir`` identify the
    old and current main worktrees with the same basename. Constructing this
    value performs no filesystem, Git, or SQLite access.
    """

    database: str | Path
    project_id: str
    source_project_dir: str
    target_project_dir: str


@dataclass(frozen=True)
class RepairMembership:
    """Describe one exact structured owner and its reviewed repair action.

    ``category`` names a supported location field, ``row_identity`` binds the
    original owner, and ``action`` is ``drop`` for a compatible duplicate or
    ``rebase`` when the owner must move to the target path. The value is
    immutable and has no side effects.
    """

    category: str
    row_identity: tuple[str, ...]
    action: str


@dataclass(frozen=True)
class RepairMapping:
    """Bind one obsolete source location to its validated target counterpart.

    ``memberships`` contains every exact structured owner of ``source`` and its
    action. Git evidence proves source and target are roots of the same project;
    branch and HEAD differences are retained as freshness evidence but are
    allowed because the current target may have advanced after the move.
    """

    source: str
    target: str
    memberships: tuple[RepairMembership, ...]
    source_git: GitEvidence
    target_git: GitEvidence


@dataclass(frozen=True)
class ReviewedMoveRepairPlan:
    """Hold one immutable move-repair preview for later atomic application.

    ``captured_state`` binds every selected structured row and the complete
    schema fingerprint. ``mappings`` is ordered by source path and includes all
    reviewed actions. The plan authorizes no mutation until passed unchanged to
    :func:`apply_move_repair`.
    """

    request: MoveRepairRequest
    captured_state: CapturedState
    mappings: tuple[RepairMapping, ...]


def plan_move_repair(request: MoveRepairRequest) -> ReviewedMoveRepairPlan:
    """Build a read-only repair plan for one completed sibling move.

    Parameters: ``request`` identifies an existing database, selected project,
    retained source main checkout, and current target main checkout. Returns an
    immutable plan only when the selected primary already equals the target,
    every structured location belongs to the source or target sibling family,
    duplicate target rows are semantically compatible, and each source/target
    checkout pair has the same Git project identity. Raises :class:`MoveError`
    for malformed, ambiguous, conflicting, or ineligible state and
    :class:`MoveOperationalError` for bounded local read failures. Planning
    opens SQLite read-only and never changes database or filesystem content.
    """
    database = move._existing_database(request.database)
    project_id = move._identifier(request.project_id, "project ID")
    source_main = move._absolute_value(request.source_project_dir, "repair source project directory")
    target_main = move._absolute_value(request.target_project_dir, "repair target project directory")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, isolation_level=None)
        connection.execute("BEGIN")
        state = _capture_state(connection, project_id)
        mappings = _build_repair_mappings(
            state,
            source_main,
            target_main,
            deadline=time.monotonic() + move.REVALIDATION_TIMEOUT_SECONDS,
        )
        return ReviewedMoveRepairPlan(request, state, mappings)
    except MoveError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise MoveOperationalError("move repair planning could not complete") from error
    finally:
        move._cleanup_connection(connection, "repair planning", sys.exception())


def apply_move_repair(
    reviewed: ReviewedMoveRepairPlan,
    *,
    application_timeout_seconds: float | None = None,
) -> None:
    """Atomically apply one previously reviewed sibling-move repair.

    Parameters: ``reviewed`` is a plan returned by :func:`plan_move_repair` and
    ``application_timeout_seconds`` optionally replaces the finite positive
    writer deadline. Returns ``None`` only after committing the exact reviewed
    drops and rebases. Raises :class:`MoveError` if selected rows, schema,
    directory evidence, Git evidence, or target conflicts changed, and
    :class:`MoveOperationalError` for bounded SQLite or filesystem failures.
    The function updates only the selected project's sandbox JSON,
    project-directory rows, session directories, and non-null workspace
    directories; it never modifies checkout contents or free-form history.
    """
    if not isinstance(reviewed, ReviewedMoveRepairPlan):
        raise MoveError("move repair reviewed plan is malformed")
    timeout = (
        move.WRITER_TRANSACTION_TIMEOUT_SECONDS
        if application_timeout_seconds is None
        else application_timeout_seconds
    )
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= move.MAX_WRITER_TRANSACTION_TIMEOUT_SECONDS
    ):
        raise MoveError("move repair application timeout is outside supported finite bounds")
    database = move._existing_database(reviewed.request.database)
    connection: sqlite3.Connection | None = None
    deadline: float | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {move.SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise MoveOperationalError("move repair SQLite foreign keys could not be enabled")
        connection.execute("BEGIN IMMEDIATE")
        deadline = time.monotonic() + timeout
        move._install_transaction_deadline(connection, deadline)
        move._validate_database_health(connection, deadline)
        current = _capture_state(connection, reviewed.captured_state.project_id, deadline=deadline)
        if current != reviewed.captured_state:
            raise MoveError("move repair preview is stale: selected database state changed")
        mappings = _build_repair_mappings(
            current,
            move._absolute_value(
                reviewed.request.source_project_dir, "repair source project directory"
            ),
            move._absolute_value(
                reviewed.request.target_project_dir, "repair target project directory"
            ),
            deadline=deadline,
        )
        if mappings != reviewed.mappings:
            raise MoveError("move repair preview is stale: directory or Git evidence changed")
        expected = _expected_state(current, mappings)
        _apply_project_sandboxes(connection, current, expected, deadline)
        _apply_project_directories(connection, current, mappings, deadline)
        _apply_row_directories(connection, current, mappings, "session", deadline)
        _apply_row_directories(connection, current, mappings, "workspace", deadline)
        observed = _capture_selected_state(
            connection,
            current.project_id,
            current.schema_fingerprint,
            deadline=deadline,
        )
        if observed != expected:
            raise MoveError("move repair post-update validation failed")
        move._validate_database_health(connection, deadline)
        move._check_transaction_deadline(deadline)
        connection.commit()
    except MoveError:
        raise
    except sqlite3.Error as error:
        if deadline is not None and time.monotonic() >= deadline:
            raise MoveOperationalError("move repair application timed out") from error
        raise MoveOperationalError("move repair application could not complete") from error
    except (OSError, ValueError, TypeError) as error:
        raise MoveOperationalError("move repair application could not complete") from error
    finally:
        move._cleanup_connection(connection, "repair application", sys.exception())


def _capture_state(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    deadline: float | None = None,
) -> CapturedState:
    """Capture the complete supported selected state and schema fingerprint."""
    fingerprint = move._validate_schema(connection, deadline=deadline)
    return _capture_selected_state(connection, project_id, fingerprint, deadline=deadline)


def _capture_selected_state(
    connection: sqlite3.Connection,
    project_id: str,
    fingerprint: tuple[tuple[str, str, str, str | None], ...],
    *,
    deadline: float | None = None,
) -> CapturedState:
    """Capture selected rows while reusing an already validated schema fingerprint."""
    project, rows = move._capture_project(connection, project_id, deadline=deadline)
    return CapturedState(fingerprint, project_id, project["worktree"], project["sandboxes"], rows)


def _build_repair_mappings(
    state: CapturedState,
    source_main: str,
    target_main: str,
    *,
    deadline: float,
) -> tuple[RepairMapping, ...]:
    """Validate a closed source/target topology and derive every repair action."""
    if state.worktree != target_main:
        raise MoveError("move repair target is not the selected project's current worktree")
    if os.path.basename(source_main) != os.path.basename(target_main):
        raise MoveError("move repair source and target basenames differ")
    source_parent = os.path.dirname(source_main)
    target_parent = os.path.dirname(target_main)
    if not source_parent or not target_parent or source_parent == target_parent:
        raise MoveError("move repair source and target families are invalid")

    sandboxes = json.loads(state.sandboxes)
    if len(sandboxes) != len(set(sandboxes)):
        raise MoveError("move repair sandbox list contains duplicates")
    target_sandboxes = {path for path in sandboxes if os.path.dirname(path) == target_parent}
    project_directories = {
        row.directory: row for row in state.rows if row.table == "project_directory" and row.directory is not None
    }
    memberships: dict[str, list[RepairMembership]] = {}

    _require_family_path(state.worktree, source_parent, target_parent, "project.worktree")
    for index, sandbox in enumerate(sandboxes):
        _check_deadline(deadline)
        family = _require_family_path(sandbox, source_parent, target_parent, "project.sandbox")
        if family != "source":
            continue
        target = os.path.join(target_parent, os.path.basename(sandbox))
        action = "drop" if target == state.worktree or target in target_sandboxes else "rebase"
        memberships.setdefault(sandbox, []).append(
            RepairMembership("project.sandbox", (state.project_id, str(index)), action)
        )

    for row in state.rows:
        _check_deadline(deadline)
        if row.directory is None:
            continue
        category = move._ROW_LOCATION_CATEGORIES[row.table]
        family = _require_family_path(row.directory, source_parent, target_parent, category)
        if family != "source":
            continue
        target = os.path.join(target_parent, os.path.basename(row.directory))
        action = "rebase"
        if row.table == "project_directory":
            target_row = project_directories.get(target)
            if target_row is not None:
                if row.payload[:2] != target_row.payload[:2]:
                    raise MoveError("move repair project-directory row has a conflicting target")
                action = "drop"
        memberships.setdefault(row.directory, []).append(
            RepairMembership(category, row.identity, action)
        )

    if not memberships:
        raise MoveError("move repair found no source-family locations")
    mappings: list[RepairMapping] = []
    for source in sorted(memberships):
        _check_deadline(deadline)
        target = os.path.join(target_parent, os.path.basename(source))
        owned = tuple(
            sorted(memberships[source], key=lambda item: (item.category, item.row_identity, item.action))
        )
        move._require_directory(source, "repair source", tuple())
        move._require_directory(target, "repair target", tuple())
        source_git = move._git_evidence(source, "repair source")
        _check_deadline(deadline)
        target_git = move._git_evidence(target, "repair target")
        if source_git.project_identity != target_git.project_identity:
            raise MoveError("move repair Git project identity mismatch")
        mappings.append(RepairMapping(source, target, owned, source_git, target_git))
    return tuple(mappings)


def _require_family_path(
    path: str,
    source_parent: str,
    target_parent: str,
    category: str,
) -> str:
    """Classify one immediate sibling path or refuse a third/nested family."""
    parent = os.path.dirname(path)
    if not os.path.basename(path) or parent not in {source_parent, target_parent}:
        raise MoveError(
            f"move repair location is outside source and target families: category={category}"
        )
    return "source" if parent == source_parent else "target"


def _expected_state(
    state: CapturedState,
    mappings: tuple[RepairMapping, ...],
) -> CapturedState:
    """Return the exact post-repair state derived from reviewed actions."""
    actions = {
        (membership.category, membership.row_identity): (membership.action, mapping.target)
        for mapping in mappings
        for membership in mapping.memberships
    }
    sandbox_actions = {
        key: action for key, action in actions.items() if key[0] == "project.sandbox"
    }
    sandboxes_text = state.sandboxes
    if sandbox_actions:
        sandboxes: list[str] = []
        for index, sandbox in enumerate(json.loads(state.sandboxes)):
            action = sandbox_actions.get(("project.sandbox", (state.project_id, str(index))))
            if action is None:
                sandboxes.append(sandbox)
            elif action[0] == "rebase":
                sandboxes.append(action[1])
        if len(sandboxes) != len(set(sandboxes)):
            raise MoveError("move repair would produce duplicate sandboxes")
        sandboxes_text = json.dumps(sandboxes)

    rows: list[CapturedRow] = []
    for row in state.rows:
        category = move._ROW_LOCATION_CATEGORIES[row.table]
        action = actions.get((category, row.identity))
        if action is None:
            rows.append(row)
            continue
        if action[0] == "drop":
            continue
        identity = (
            (row.identity[0], action[1])
            if row.table == "project_directory"
            else row.identity
        )
        rows.append(CapturedRow(row.table, identity, action[1], row.payload))
    order = {"project_directory": 0, "session": 1, "workspace": 2}
    rows.sort(key=lambda row: (order[row.table], row.identity))
    return CapturedState(
        state.schema_fingerprint,
        state.project_id,
        state.worktree,
        sandboxes_text,
        tuple(rows),
    )


def _apply_project_sandboxes(
    connection: sqlite3.Connection,
    state: CapturedState,
    expected: CapturedState,
    deadline: float,
) -> None:
    """Replace only the selected project's exact reviewed sandbox JSON."""
    if expected.sandboxes == state.sandboxes:
        return
    result = connection.execute(
        "UPDATE project SET sandboxes = ? WHERE id = ? AND worktree = ? AND sandboxes = ?",
        (expected.sandboxes, state.project_id, state.worktree, state.sandboxes),
    )
    if result.rowcount != 1:
        raise MoveError("move repair project update affected an unexpected row count")
    move._check_transaction_deadline(deadline)


def _apply_project_directories(
    connection: sqlite3.Connection,
    state: CapturedState,
    mappings: tuple[RepairMapping, ...],
    deadline: float,
) -> None:
    """Drop compatible duplicate keys or rebase vacant project-directory keys."""
    actions = {
        membership.row_identity: (membership.action, mapping.target)
        for mapping in mappings
        for membership in mapping.memberships
        if membership.category == "project_directory.directory"
    }
    for row in state.rows:
        if row.table != "project_directory" or row.identity not in actions:
            continue
        move._check_transaction_deadline(deadline)
        action, target = actions[row.identity]
        statement = (
            "DELETE FROM project_directory WHERE project_id = ? AND directory = ? "
            "AND type IS ? AND strategy IS ? AND time_created IS ?"
            if action == "drop"
            else "UPDATE project_directory SET directory = ? WHERE project_id = ? AND directory = ? "
            "AND type IS ? AND strategy IS ? AND time_created IS ?"
        )
        parameters = (
            (*row.identity, *row.payload)
            if action == "drop"
            else (target, *row.identity, *row.payload)
        )
        result = connection.execute(statement, parameters)
        if result.rowcount != 1:
            raise MoveError("move repair project-directory update affected an unexpected row count")


def _apply_row_directories(
    connection: sqlite3.Connection,
    state: CapturedState,
    mappings: tuple[RepairMapping, ...],
    table: str,
    deadline: float,
) -> None:
    """Rebase exact reviewed session or workspace directory rows."""
    category = move._ROW_LOCATION_CATEGORIES[table]
    targets = {
        membership.row_identity: mapping.target
        for mapping in mappings
        for membership in mapping.memberships
        if membership.category == category
    }
    for row in state.rows:
        if row.table != table or row.identity not in targets or row.directory is None:
            continue
        move._check_transaction_deadline(deadline)
        result = connection.execute(
            f'UPDATE "{table}" SET directory = ? WHERE id = ? AND project_id = ? AND directory = ?',
            (targets[row.identity], row.identity[0], state.project_id, row.directory),
        )
        if result.rowcount != 1:
            raise MoveError(f"move repair {table} update affected an unexpected row count")


def _check_deadline(deadline: float) -> None:
    """Fail operationally when planning or application exceeds its shared deadline."""
    if time.monotonic() >= deadline:
        raise MoveOperationalError("move repair validation timed out")
