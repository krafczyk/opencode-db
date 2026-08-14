"""Read-only sibling move planning contracts."""

from __future__ import annotations

import json
import os
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import selectors
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import move, move_git
from opencode_db.move import (
    MoveError,
    MoveOperationalError,
    MoveRequest,
    apply_sibling_move,
    plan_sibling_move,
)


_TEST_ROOT = Path("/tmp/opencode-db-v1")


@contextmanager
def _temporary_directory():
    """Yield one private disposable test directory beneath the approved root."""
    _TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_TEST_ROOT, 0o700)
    if stat.S_IMODE(_TEST_ROOT.stat().st_mode) != 0o700:
        raise RuntimeError("test root must be private")
    with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory:
        yield Path(directory)


class MovePlanningTests(unittest.TestCase):
    """Prove planning captures a complete, read-only sibling move decision."""

    def test_complete_locations_produce_stable_mappings_without_row_writes(self) -> None:
        """Capture every supported category and preserve database rows during planning."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            before = self._rows(database)
            filesystem_before = self._tree(source_parent) + self._tree(target_parent)

            reviewed = plan_sibling_move(
                MoveRequest(database, "project", str(target_parent / "main"))
            )

            self.assertEqual([mapping.source for mapping in reviewed.mappings], [
                str(source_parent / "directory"),
                str(source_parent / "main"),
                str(source_parent / "sandbox"),
                str(source_parent / "session"),
                str(source_parent / "workspace"),
            ])
            self.assertEqual(
                next(mapping for mapping in reviewed.mappings if mapping.source.endswith("/main")).categories,
                ("project.worktree",),
            )
            self.assertEqual(before, self._rows(database))
            self.assertEqual(filesystem_before, self._tree(source_parent) + self._tree(target_parent))

    def test_duplicate_locations_deduplicate_validation_but_keep_memberships(self) -> None:
        """Retain every owning category when one sibling path appears in many rows."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root, duplicate=True)

            reviewed = plan_sibling_move(
                MoveRequest(database, "project", str(target_parent / "main"))
            )

            main = next(mapping for mapping in reviewed.mappings if mapping.source.endswith("/main"))
            self.assertEqual(
                main.categories,
                (
                    "project.worktree",
                    "project_directory.directory",
                    "session.directory",
                    "workspace.directory",
                ),
            )
            self.assertEqual(len(reviewed.captured_state.rows), 4)
            self.assertEqual(len(reviewed.mappings), 2)

    def test_target_main_basename_mismatch_refuses_without_writes(self) -> None:
        """Reject a target family whose declared main checkout has another basename."""
        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            before = self._rows(database)

            with self.assertRaisesRegex(MoveError, "target basename"):
                plan_sibling_move(
                    MoveRequest(database, "project", str(target_parent / "other"))
                )

            self.assertEqual(before, self._rows(database))

    def test_null_workspace_and_snapshot_transaction_remain_in_captured_state(self) -> None:
        """Keep null workspace membership and one pre-commit snapshot despite a later writer."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode = WAL")
            original = move._validate_schema

            def write_after_schema(connection: sqlite3.Connection):
                fingerprint = original(connection)
                with closing(sqlite3.connect(database)) as writer, writer:
                    writer.execute(
                        "UPDATE session SET directory = ? WHERE id = 'session'",
                        (str(source_parent / "main"),),
                    )
                return fingerprint

            with mock.patch.object(move, "_validate_schema", side_effect=write_after_schema):
                reviewed = plan_sibling_move(
                    MoveRequest(database, "project", str(target_parent / "main"))
                )

            self.assertIn(
                move.CapturedRow("workspace", ("null",), None), reviewed.captured_state.rows
            )
            self.assertIn(
                move.CapturedRow("session", ("session",), str(source_parent / "session")),
                reviewed.captured_state.rows,
            )

    def test_schema_and_dynamic_value_refusals_preserve_database_rows(self) -> None:
        """Reject malformed schema, generated paths, triggers, and BLOB path values before planning."""
        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            before = self._rows(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TRIGGER refuse BEFORE UPDATE ON project BEGIN SELECT 1; END")
            with self.assertRaisesRegex(MoveError, "triggers"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            self.assertEqual(before, self._rows(database))

        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE session SET directory = ?", (sqlite3.Binary(b"/bad"),))
            with self.assertRaisesRegex(MoveError, "absolute path"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE extra_directory (project_id TEXT, directory TEXT)")
            with self.assertRaisesRegex(MoveError, "unknown project directory"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        for statement, message in (
            ("DROP TABLE workspace", "schema is incomplete"),
            ("CREATE UNIQUE INDEX workspace_directory_unique ON workspace(directory)", "unfamiliar unique"),
            ("CREATE UNIQUE INDEX project_worktree_unique ON project(worktree)", "unfamiliar unique"),
            ("CREATE TABLE directory_ref (directory TEXT REFERENCES workspace(directory))", "inbound directory"),
            ("ALTER TABLE workspace ADD COLUMN generated TEXT GENERATED ALWAYS AS (directory) VIRTUAL", "generated columns"),
            ("ALTER TABLE workspace ADD COLUMN alternate_project TEXT REFERENCES project(id)", "unfamiliar foreign keys"),
        ):
            with self.subTest(statement=statement), _temporary_directory() as root:
                _source_parent, target_parent, database = self._fixture(root)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(statement)
                with self.assertRaisesRegex(MoveError, message):
                    plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE project SET sandboxes = '{'")
            with self.assertRaisesRegex(MoveError, "sandbox JSON is malformed"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

    def test_sandbox_and_configurable_capture_limits_refuse_at_boundaries(self) -> None:
        """Refuse every bounded schema, row, location, sandbox, scalar, and capture excess."""
        cases = (
            ("MAX_SCHEMA_OBJECTS", 4, "schema object"),
            ("MAX_SELECTED_ROWS", 4, "selected row"),
            ("MAX_LOCATIONS", 4, "distinct location"),
            ("MAX_SANDBOX_ENTRIES", 0, "sandbox JSON"),
            ("MAX_SANDBOX_BYTES", 2, "sandbox JSON"),
            ("MAX_CAPTURE_BYTES", 1, "captured scalar"),
            ("MAX_SCHEMA_FINGERPRINT_BYTES", 1, "schema fingerprint"),
        )
        for constant, limit, message in cases:
            with self.subTest(constant=constant), _temporary_directory() as root:
                _source_parent, target_parent, database = self._fixture(root)
                if constant == "MAX_SCHEMA_OBJECTS":
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute("CREATE TABLE unrelated (id TEXT)")
                with mock.patch.object(move, constant, limit):
                    with self.assertRaisesRegex(MoveError, message):
                        plan_sibling_move(
                            MoveRequest(database, "project", str(target_parent / "main"))
                        )

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            long_value = "/" + "x" * move.MAX_VALUE_BYTES
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE project SET worktree = ?", (long_value,))
            with self.assertRaisesRegex(MoveError, "value limit"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            self.assertTrue((source_parent / "main").is_dir())

    def test_topology_noop_and_missing_path_refusals_name_safe_categories(self) -> None:
        """Reject nested, external, same-parent, and absent paths before Git validation."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            nested = source_parent / "main" / "nested"
            nested.mkdir()
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE session SET directory = ?", (str(nested),))
            with self.assertRaisesRegex(MoveError, "sibling layout.*session.directory"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            with self.assertRaisesRegex(MoveError, "same parent"):
                plan_sibling_move(MoveRequest(database, "project", str(source_parent / "main")))
            shutil.rmtree(target_parent / "sandbox")
            with self.assertRaisesRegex(MoveError, r"target directory is missing.*project\.sandbox"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            shutil.rmtree(source_parent / "directory")
            with self.assertRaisesRegex(MoveError, r"source directory is missing.*project_directory\.directory"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

    def test_git_state_identity_branch_and_head_correspondence(self) -> None:
        """Accept root fallback and detached pairs while rejecting every Git correspondence dimension."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            subprocess.run(["git", "-C", str(source_parent / "main"), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(target_parent / "main"), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            main = next(item for item in reviewed.mappings if item.source.endswith("/main"))
            self.assertEqual(main.source_git.checkout_state, "detached")
            self.assertTrue(main.source_git.project_identity.startswith("root:"))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            subprocess.run(["git", "-C", str(target_parent / "main"), "checkout", "-qb", "other"], check=True, stdout=subprocess.DEVNULL)
            with self.assertRaisesRegex(MoveError, "branch mismatch"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            target = target_parent / "main"
            (target / "changed").write_text("changed", encoding="ascii")
            subprocess.run(["git", "-C", str(target), "add", "changed"], check=True)
            subprocess.run(["git", "-C", str(target), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "changed"], check=True)
            with self.assertRaisesRegex(MoveError, "HEAD mismatch"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            subprocess.run(["git", "-C", str(source_parent / "main"), "remote", "add", "origin", "https://token@example.invalid/project.git?private#fragment"], check=True)
            subprocess.run(["git", "-C", str(target_parent / "main"), "remote", "add", "origin", "https://example.invalid/other.git"], check=True)
            with self.assertRaisesRegex(MoveError, "project identity mismatch") as caught:
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            self.assertNotIn("token", str(caught.exception))
            self.assertNotIn("project.git", str(caught.exception))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            for checkout in (source_parent / "main", target_parent / "main"):
                subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "https://token@example.invalid/project.git?private#fragment"], check=True)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            self.assertNotIn("token", repr(reviewed))
            self.assertNotIn("private", repr(reviewed))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            subprocess.run(["git", "-C", str(target_parent / "main"), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            with self.assertRaisesRegex(MoveError, "checkout state mismatch"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

    def test_non_repository_and_bounded_git_probe_failures_are_distinct_and_reaped(self) -> None:
        """Classify unavailable, nonzero, malformed, capped, timed-out, and interrupted local Git probes."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            shutil.rmtree(target_parent / "directory" / ".git")
            with self.assertRaisesRegex(MoveError, "repository root probe failed"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

        with _temporary_directory() as root:
            script = self._fake_git(root)
            environment = {"PATH": str(script.parent), "MOVE_FAKE_GIT": "nonzero"}
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(MoveError, "probe failed"):
                    move._git_output("/unused", "source", ("x",), "probe")
            for mode, error in (("malformed", MoveError), ("large", MoveOperationalError), ("sleep", MoveOperationalError)):
                with self.subTest(mode=mode), mock.patch.dict(os.environ, {"PATH": str(script.parent), "MOVE_FAKE_GIT": mode}, clear=False):
                    with self.assertRaises(error):
                        if mode == "malformed":
                            move._git_evidence("/unused", "source")
                        else:
                            move._git_output("/unused", "source", ("x",), "probe")
            with mock.patch("opencode_db.move_git.subprocess.Popen", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(MoveOperationalError, "unavailable"):
                    move._git_output("/unused", "source", ("x",), "probe")

            pid_file = root / "child.pid"
            real_selector = selectors.DefaultSelector
            class InterruptingSelector:
                """Provide the real selector registration surface but interrupt its first wait."""

                def __init__(self) -> None:
                    self._selector = real_selector()

                def register(self, *args: object) -> object:
                    return self._selector.register(*args)

                def get_map(self) -> object:
                    return self._selector.get_map()

                def select(self, _timeout: float) -> object:
                    time.sleep(0.05)
                    raise KeyboardInterrupt

                def close(self) -> None:
                    self._selector.close()

            with mock.patch.dict(os.environ, {"PATH": str(script.parent), "MOVE_FAKE_GIT": "pid-sleep", "MOVE_FAKE_GIT_PID": str(pid_file)}, clear=False):
                with mock.patch.object(move_git.selectors, "DefaultSelector", InterruptingSelector):
                    with self.assertRaisesRegex(MoveOperationalError, "interrupted"):
                        move._git_output("/unused", "source", ("x",), "probe")
            child_pid = int(pid_file.read_text(encoding="ascii"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_apply_updates_only_selected_structured_locations_atomically(self) -> None:
        """Apply every selected location while retaining protected rows and filesystem contents."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(
                    """
                    ALTER TABLE project ADD COLUMN time_updated INTEGER;
                    ALTER TABLE session ADD COLUMN path TEXT;
                    ALTER TABLE workspace ADD COLUMN data BLOB;
                    CREATE TABLE message (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
                    """
                )
                connection.execute("UPDATE project SET time_updated = 7 WHERE id = 'project'")
                connection.execute("UPDATE session SET path = 'relative/session' WHERE id = 'session'")
                connection.execute("UPDATE workspace SET data = ? WHERE id = 'workspace'", (b"opaque",))
                connection.execute("INSERT INTO message VALUES (?, ?)", ("message", str(source_parent).encode()))
                connection.execute(
                    "INSERT INTO project (id, worktree, sandboxes, time_updated) VALUES (?, ?, ?, ?)",
                    ("unrelated", "/unrelated/main", "[]", 11),
                )
            filesystem_before = self._tree(source_parent) + self._tree(target_parent)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

            apply_sibling_move(reviewed)

            with closing(sqlite3.connect(database)) as connection, connection:
                self.assertEqual(
                    connection.execute("SELECT worktree, sandboxes, time_updated FROM project WHERE id = 'project'").fetchone(),
                    (str(target_parent / "main"), json.dumps([str(target_parent / "sandbox")]), 7),
                )
                self.assertEqual(
                    connection.execute("SELECT directory, type, strategy, time_created FROM project_directory").fetchone(),
                    (str(target_parent / "directory"), "root", None, 1),
                )
                self.assertEqual(
                    connection.execute("SELECT directory, path FROM session WHERE id = 'session'").fetchone(),
                    (str(target_parent / "session"), "relative/session"),
                )
                self.assertEqual(
                    connection.execute("SELECT directory, data FROM workspace WHERE id = 'workspace'").fetchone(),
                    (str(target_parent / "workspace"), b"opaque"),
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM workspace WHERE id = 'null'").fetchone(), (None,))
                self.assertEqual(
                    connection.execute("SELECT payload FROM message").fetchone(), (str(source_parent).encode(),),
                )
                self.assertEqual(
                    connection.execute("SELECT worktree, sandboxes, time_updated FROM project WHERE id = 'unrelated'").fetchone(),
                    ("/unrelated/main", "[]", 11),
                )
            self.assertEqual(filesystem_before, self._tree(source_parent) + self._tree(target_parent))

    def test_apply_rejects_every_reviewed_database_and_filesystem_drift(self) -> None:
        """Reject changed selected rows, schema, directory evidence, and Git evidence before writes."""
        mutations = (
            ("worktree", lambda connection, source, _target: connection.execute("UPDATE project SET worktree = ?", (str(source / "sandbox"),))),
            ("sandboxes", lambda connection, source, _target: connection.execute("UPDATE project SET sandboxes = ?", (json.dumps([str(source / "main")]),))),
            ("project directory", lambda connection, source, _target: connection.execute("UPDATE project_directory SET directory = ?", (str(source / "main"),))),
            ("session", lambda connection, source, _target: connection.execute("UPDATE session SET directory = ?", (str(source / "main"),))),
            ("workspace", lambda connection, source, _target: connection.execute("UPDATE workspace SET directory = ? WHERE id = 'workspace'", (str(source / "main"),))),
            ("project directory payload", lambda connection, _source, _target: connection.execute("UPDATE project_directory SET strategy = 'changed'")),
            ("membership", lambda connection, source, _target: connection.execute("DELETE FROM workspace WHERE id = 'null'")),
            ("schema", lambda connection, _source, _target: connection.execute("CREATE TABLE unrelated (id TEXT)")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), _temporary_directory() as root:
                source_parent, target_parent, database = self._fixture(root)
                reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
                before = self._structured_rows(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    mutate(connection, source_parent, target_parent)
                changed = self._structured_rows(database)
                with self.assertRaisesRegex(MoveError, "preview is stale"):
                    apply_sibling_move(reviewed)
                self.assertEqual(changed, self._structured_rows(database))
                if label != "schema":
                    self.assertNotEqual(before, changed)

        for label, mutate in (
            ("directory", lambda source, target: shutil.rmtree(target / "sandbox")),
            ("Git", lambda source, target: subprocess.run(["git", "-C", str(target / "main"), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)),
        ):
            with self.subTest(label=label), _temporary_directory() as root:
                source_parent, target_parent, database = self._fixture(root)
                reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
                before = self._structured_rows(database)
                mutate(source_parent, target_parent)
                with self.assertRaises(MoveError):
                    apply_sibling_move(reviewed)
                self.assertEqual(before, self._structured_rows(database))

    def test_apply_updates_duplicate_owners_without_touching_null_workspace(self) -> None:
        """Rewrite every owner of a duplicate location while preserving a null workspace row."""
        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root, duplicate=True)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

            apply_sibling_move(reviewed)

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT directory FROM project_directory").fetchone(),
                    (str(target_parent / "main"),),
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM session").fetchone(),
                    (str(target_parent / "main"),),
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM workspace WHERE id = 'workspace'").fetchone(),
                    (str(target_parent / "main"),),
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM workspace WHERE id = 'null'").fetchone(),
                    (None,),
                )

    def test_apply_requires_vacant_project_directory_key_and_rolls_back_each_group(self) -> None:
        """Refuse a late occupied composite key and restore all tables after injected failures."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            before = self._structured_rows(database)
            original = move._apply_project_directory_locations

            def occupy_then_apply(
                connection: sqlite3.Connection,
                state: move.CapturedState,
                mappings: tuple[move.MoveMapping, ...],
                deadline: float,
            ) -> None:
                connection.execute(
                    "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
                    ("project", str(target_parent / "directory"), "conflict", "manual", 2),
                )
                original(connection, state, mappings, deadline)

            with mock.patch.object(move, "_apply_project_directory_locations", side_effect=occupy_then_apply):
                with self.assertRaisesRegex(MoveError, "target key is occupied"):
                    apply_sibling_move(reviewed)
            self.assertEqual(before, self._structured_rows(database))

        for group in ("project", "project_directory", "session", "workspace", "pre_commit"):
            with self.subTest(group=group), _temporary_directory() as root:
                source_parent, target_parent, database = self._fixture(root)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("CREATE TABLE message (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                    connection.execute("INSERT INTO message VALUES (?, ?)", ("sentinel", str(source_parent).encode()))
                reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
                before = self._structured_rows(database)
                filesystem_before = self._tree(source_parent) + self._tree(target_parent)

                def fail_after(observed: str) -> None:
                    if observed == group:
                        raise MoveOperationalError("injected move failure")

                with mock.patch.object(move, "_after_move_update_group", side_effect=fail_after):
                    with self.assertRaisesRegex(MoveOperationalError, "injected move failure"):
                        apply_sibling_move(reviewed)
                self.assertEqual(before, self._structured_rows(database))
                self.assertEqual(filesystem_before, self._tree(source_parent) + self._tree(target_parent))

    def test_apply_checks_foreign_keys_preserves_wal_and_refuses_stale_reapply(self) -> None:
        """Use committed WAL state, reject pre-existing FK failures, and never apply a plan twice."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone(), ("wal",))
                connection.execute("CREATE TABLE history (id TEXT PRIMARY KEY, payload BLOB)")
                connection.execute("INSERT INTO history VALUES (?, ?)", ("wal", str(source_parent).encode()))
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            apply_sibling_move(reviewed)
            with closing(sqlite3.connect(database)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
                self.assertEqual(connection.execute("SELECT payload FROM history").fetchone(), (str(source_parent).encode(),))
            with self.assertRaisesRegex(MoveError, "preview is stale"):
                apply_sibling_move(reviewed)

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            before = self._structured_rows(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("INSERT INTO workspace VALUES (?, ?, ?)", ("broken", "missing", str(source_parent / "main")))
            with self.assertRaisesRegex(MoveError, "foreign key check"):
                apply_sibling_move(reviewed)
            after = self._structured_rows(database)
            self.assertEqual(
                before,
                tuple(row for row in after if row[1] != "broken"),
            )

    def test_apply_times_out_for_a_competing_writer_without_partial_mutation(self) -> None:
        """Bound writer-lock waits and leave all selected locations untouched on contention."""
        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            before = self._structured_rows(database)
            writer = sqlite3.connect(database, isolation_level=None)
            try:
                writer.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with self.assertRaisesRegex(MoveOperationalError, "application could not complete"):
                    apply_sibling_move(reviewed)
                self.assertGreaterEqual(time.monotonic() - started, move.SQLITE_BUSY_TIMEOUT_MS / 1000)
            finally:
                writer.rollback()
                writer.close()
            self.assertEqual(before, self._structured_rows(database))

    def test_schema_refuses_project_sandbox_constraints_and_cascading_parent_reference(self) -> None:
        """Reject every rewritten project parent key before any unknown-table mutation."""
        for statement, message in (
            ("CREATE UNIQUE INDEX project_sandboxes_unique ON project(sandboxes)", "unfamiliar unique"),
            (
                "CREATE TABLE project_parent_ref ("
                "value TEXT REFERENCES project(worktree) ON UPDATE CASCADE)",
                "inbound directory",
            ),
        ):
            with self.subTest(statement=statement), _temporary_directory() as root:
                _source_parent, target_parent, database = self._fixture(root)
                before = self._structured_rows(database)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(statement)
                with self.assertRaisesRegex(MoveError, message):
                    plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
                self.assertEqual(before, self._structured_rows(database))

    def test_copied_linked_worktree_admin_metadata_refuses_during_plan_and_apply(self) -> None:
        """Reject copied linked-worktree metadata that remains in the source Git family."""
        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            source_main = source_parent / "main"
            target_main = target_parent / "main"
            linked = root / "linked"
            subprocess.run(["git", "-C", str(source_main), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(target_main), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_main)))
            before = self._structured_rows(database)
            subprocess.run(
                ["git", "-C", str(source_main), "worktree", "add", "--detach", str(linked), "HEAD"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            shutil.rmtree(target_main)
            shutil.copytree(linked, target_main)
            with self.assertRaisesRegex(MoveError, "administrative metadata points"):
                apply_sibling_move(reviewed)
            self.assertEqual(before, self._structured_rows(database))

        with _temporary_directory() as root:
            source_parent, target_parent, database = self._fixture(root)
            source_main = source_parent / "main"
            linked = root / "linked"
            subprocess.run(["git", "-C", str(source_main), "checkout", "--detach"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-C", str(source_main), "worktree", "add", "--detach", str(linked), "HEAD"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            shutil.rmtree(target_parent / "main")
            shutil.copytree(linked, target_parent / "main")
            with self.assertRaisesRegex(MoveError, "administrative metadata points"):
                plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

    def test_writer_deadline_rolls_back_late_update_and_progress_failure_remains_success(self) -> None:
        """Bound the whole writer transaction and keep optional progress observational."""
        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))
            before = self._structured_rows(database)

            def delay_after_project(group: str) -> None:
                if group == "project":
                    time.sleep(0.02)

            with mock.patch.object(move, "WRITER_TRANSACTION_TIMEOUT_SECONDS", 0.01):
                with mock.patch.object(move, "_after_move_update_group", side_effect=delay_after_project):
                    with self.assertRaisesRegex(MoveOperationalError, "application timed out"):
                        apply_sibling_move(reviewed)
            self.assertEqual(before, self._structured_rows(database))

        with _temporary_directory() as root:
            _source_parent, target_parent, database = self._fixture(root)
            reviewed = plan_sibling_move(MoveRequest(database, "project", str(target_parent / "main")))

            def failing_progress(_phase: str, _completed: int | None, _total: int | None, _complete: bool) -> None:
                raise RuntimeError("observer failed")

            apply_sibling_move(reviewed, progress=failing_progress)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT worktree FROM project WHERE id = 'project'").fetchone(),
                    (str(target_parent / "main"),),
                )

    def test_cleanup_failures_are_bounded_and_preserve_the_original_error(self) -> None:
        """Report unconfirmed rollback and Git reaping without leaking raw exceptions."""
        connection = mock.Mock()
        connection.in_transaction = True
        connection.rollback.side_effect = sqlite3.OperationalError("rollback failure")
        original = MoveError("selected failure")
        with self.assertRaisesRegex(MoveOperationalError, "rollback could not be confirmed") as caught:
            move._cleanup_connection(connection, "application", original)
        self.assertIs(caught.exception.__cause__, original)
        connection.close.assert_called_once_with()

        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("git", 0.01)
        with self.assertRaisesRegex(MoveOperationalError, "cleanup could not reap"):
            move_git._reap_process(process, MoveOperationalError, 0.01)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0.01)

    @staticmethod
    def _fake_git(root: Path) -> Path:
        """Create a disposable local Git stand-in for subprocess-boundary tests."""
        script = root / "git"
        script.write_text(
            "#!/usr/bin/python3\n"
            "import os, sys, time\n"
            "mode = os.environ['MOVE_FAKE_GIT']\n"
            "if mode == 'nonzero': sys.exit(2)\n"
            "if mode == 'malformed': sys.stdout.write('broken'); sys.exit(0)\n"
            "if mode == 'large': sys.stdout.buffer.write(b'x' * 65537); sys.stdout.flush(); time.sleep(10)\n"
            "if mode == 'sleep': time.sleep(10)\n"
            "if mode == 'pid-sleep': open(os.environ['MOVE_FAKE_GIT_PID'], 'w').write(str(os.getpid())); time.sleep(10)\n",
            encoding="ascii",
        )
        script.chmod(0o700)
        return script

    @staticmethod
    def _fixture(
        root: Path, *, duplicate: bool = False
    ) -> tuple[Path, Path, Path]:
        """Create copied flat Git families and one minimal supported SQLite database."""
        source_parent = root / "source"
        target_parent = root / "target"
        source_parent.mkdir()
        target_parent.mkdir()
        names = ("main", "sandbox", "directory", "session", "workspace")
        for name in names:
            checkout = source_parent / name
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            (checkout / "marker").write_text(name, encoding="ascii")
            subprocess.run(["git", "-C", str(checkout), "add", "marker"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(checkout), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "initial",
                ],
                check=True,
            )
            shutil.copytree(checkout, target_parent / name)
        database = root / "opencode.db"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE project (
                    id TEXT PRIMARY KEY,
                    worktree TEXT NOT NULL,
                    sandboxes TEXT NOT NULL
                );
                CREATE TABLE project_directory (
                    project_id TEXT NOT NULL,
                    directory TEXT NOT NULL,
                    type TEXT,
                    strategy TEXT,
                    time_created INTEGER NOT NULL,
                    PRIMARY KEY (project_id, directory),
                    FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
                );
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    directory TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
                );
                CREATE TABLE workspace (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    directory TEXT,
                    FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
                );
                """
            )
            main = source_parent / "main"
            connection.execute(
                "INSERT INTO project VALUES (?, ?, ?)",
                ("project", str(main), json.dumps([str(source_parent / "sandbox")])),
            )
            connection.execute(
                "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
                ("project", str(main if duplicate else source_parent / "directory"), "root", None, 1),
            )
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?)",
                ("session", "project", str(main if duplicate else source_parent / "session")),
            )
            connection.execute(
                "INSERT INTO workspace VALUES (?, ?, ?)",
                ("workspace", "project", str(main if duplicate else source_parent / "workspace")),
            )
            connection.execute("INSERT INTO workspace VALUES (?, ?, NULL)", ("null", "project"))
            connection.commit()
        finally:
            connection.close()
        return source_parent, target_parent, database

    @staticmethod
    def _rows(database: Path) -> tuple[tuple[object, ...], ...]:
        """Return all location rows in stable order to prove planning did not write them."""
        connection = sqlite3.connect(database)
        try:
            return tuple(
                connection.execute(
                    "SELECT worktree, sandboxes FROM project UNION ALL "
                    "SELECT directory, project_id FROM project_directory UNION ALL "
                    "SELECT directory, project_id FROM session UNION ALL "
                    "SELECT directory, project_id FROM workspace ORDER BY 1, 2"
                ).fetchall()
            )
        finally:
            connection.close()

    @staticmethod
    def _structured_rows(database: Path) -> tuple[tuple[object, ...], ...]:
        """Return all known structured rows and identities in stable table-local order."""
        connection = sqlite3.connect(database)
        try:
            return tuple(
                ("project", *row)
                for row in connection.execute("SELECT id, worktree, sandboxes FROM project ORDER BY id")
            ) + tuple(
                ("project_directory", *row)
                for row in connection.execute(
                    "SELECT project_id, directory, type, strategy, time_created "
                    "FROM project_directory ORDER BY project_id, directory"
                )
            ) + tuple(
                ("session", *row)
                for row in connection.execute("SELECT id, project_id, directory FROM session ORDER BY id")
            ) + tuple(
                ("workspace", *row)
                for row in connection.execute("SELECT id, project_id, directory FROM workspace ORDER BY id")
            )
        finally:
            connection.close()

    @staticmethod
    def _tree(directory: Path) -> tuple[tuple[str, bytes], ...]:
        """Return a stable fixture content snapshot to prove planning made no file writes."""
        return tuple(
            sorted(
                (str(path.relative_to(directory)), path.read_bytes())
                for path in directory.rglob("*")
                if path.is_file()
            )
        )


if __name__ == "__main__":
    unittest.main()
