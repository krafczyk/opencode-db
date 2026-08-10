"""Focused contract tests for project-scoped OpenCode session transfer."""

from __future__ import annotations

import os
import io
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db.transfer import TransferError, export_sessions, import_sessions
from opencode_db import cli

SIMPLE_SESSION_CHILD_TABLES = (
    "message",
    "todo",
    "session_message",
    "session_input",
    "session_context_epoch",
    "session_share",
)

PART_SESSION_TABLES = ("part",)

SESSION_ID_CHILD_TABLES = (*SIMPLE_SESSION_CHILD_TABLES, *PART_SESSION_TABLES)

EVENT_SESSION_TABLES = (
    "event_sequence",
    "event",
)

SESSION_TABLES = (*SESSION_ID_CHILD_TABLES, *EVENT_SESSION_TABLES)

SESSION_SELECTORS = {
    **{table: "session_id" for table in SESSION_TABLES},
    "event_sequence": "aggregate_id",
    "event": "aggregate_id",
}

_TEST_ROOT = Path("/tmp/opencode-db-v1")


@contextmanager
def _sqlite_connection(path: Path):
    """Yield one disposable SQLite connection, committing then closing it."""
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def _temporary_directory():
    """Yield a private disposable fixture directory beneath the approved test root."""
    _TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_TEST_ROOT, 0o700)
    if not _TEST_ROOT.is_dir() or stat.S_IMODE(_TEST_ROOT.stat().st_mode) != 0o700:
        raise RuntimeError("test root must be a private directory")
    with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory:
        yield Path(directory)


class SessionTransferTests(unittest.TestCase):
    """Prove transfer scope, schema gates, remapping, and replacement behavior."""

    def test_export_reports_private_canonical_artifact_and_complete_table_families(
        self,
    ) -> None:
        """Export only one project's sessions and every known child-table family."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            source_dir.mkdir()
            database = root / "source.db"
            self._create_database(database, source_dir, root / "other-project")
            requested_export = root / "exports" / "nested"

            outcome = export_sessions(database, source_dir, requested_export)

            self.assertEqual(outcome.source_project_id, "source")
            self.assertEqual(outcome.export_dir, requested_export.resolve())
            self.assertTrue(outcome.import_file.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(outcome.export_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(outcome.import_file).st_mode), 0o600)
            archive = sqlite3.connect(outcome.import_file)
            try:
                self.assertEqual(
                    archive.execute(
                        "SELECT export_dir, source_project_id "
                        "FROM _opencode_db_transfer_metadata"
                    ).fetchone(),
                    (str(requested_export.resolve()), "source"),
                )
                self.assertEqual(
                    archive.execute("SELECT COUNT(*) FROM session").fetchone(), (3,)
                )
                for table in SESSION_TABLES:
                    with self.subTest(table=table):
                        self.assertEqual(
                            archive.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone(),
                            (3,),
                        )
            finally:
                archive.close()

    def test_import_remaps_and_replaces_idempotently_without_touching_other_sessions(
        self,
    ) -> None:
        """Import all family rows atomically while preserving IDs and unrelated state."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_dir = root / "target-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir()
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(target, target_dir, other_dir, target=True)
            exported = export_sessions(source, source_dir, root / "exports")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "import",
                        "--target-project-dir",
                        str(target_dir),
                        "--db",
                        str(target),
                        "--import",
                        str(exported.import_file),
                    ]
                )
            with _sqlite_connection(target) as connection:
                sessions = connection.execute(
                    "SELECT id, project_id, directory, path, workspace_id, parent_id "
                    "FROM session WHERE id IN ('root', 'child', 'global-source') ORDER BY id"
                ).fetchall()
                unrelated = connection.execute(
                    "SELECT project_id, directory FROM session WHERE id = 'unrelated'"
                ).fetchone()
                self.assertEqual(
                    sessions,
                    [
                        ("child", "target", str(target_dir), "", None, "root"),
                        ("global-source", "target", str(target_dir), "", None, None),
                        ("root", "target", str(target_dir), "", None, None),
                    ],
                )
                self.assertEqual(unrelated, ("other", str(other_dir)))
                for table in SESSION_TABLES:
                    with self.subTest(table=table):
                        selector = SESSION_SELECTORS[table]
                        self.assertEqual(
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{table}" WHERE "{selector}" IN '
                                "('root', 'child', 'global-source')"
                            ).fetchone(),
                            (3,),
                        )
                        self.assertEqual(
                            connection.execute(
                                f'SELECT marker FROM "{table}" WHERE "{selector}" = \'root\''
                            ).fetchone(),
                            (b"source-root",),
                        )
            self.assertEqual(exit_code, 0)
            self.assertIn("target_project_id: target", output.getvalue())
            second = import_sessions(target_dir, target, exported.import_file)
            self.assertEqual(second.imported_sessions, 3)
            with _sqlite_connection(target) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM session").fetchone(), (4,)
                )
                for table in SESSION_TABLES:
                    with self.subTest(table=table):
                        self.assertEqual(
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone(),
                            (4,),
                        )

    def test_refuses_malformed_archive_and_unknown_linked_schemas_without_mutation(
        self,
    ) -> None:
        """Reject archive and destination schema extensions that could lose session state."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_dir = root / "target-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir()
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(target, target_dir, other_dir, target=True)
            exported = export_sessions(source, source_dir, root / "exports")
            malformed = root / "malformed.sqlite"
            with _sqlite_connection(malformed) as connection:
                connection.execute("CREATE TABLE not_an_archive (value TEXT)")
            before = target.read_bytes()

            with self.assertRaisesRegex(TransferError, "archive schema"):
                import_sessions(target_dir, target, malformed)
            self.assertEqual(target.read_bytes(), before)

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "import",
                        "--target-project-dir",
                        str(target_dir),
                        "--db",
                        str(target),
                        "--import",
                        str(malformed),
                    ]
                )
            self.assertEqual(exit_code, 4)
            self.assertIn("archive schema", stderr.getvalue())
            self.assertEqual(target.read_bytes(), before)

            with _sqlite_connection(target) as connection:
                connection.execute("CREATE TABLE extension_state (session_id TEXT)")
            with self.assertRaisesRegex(TransferError, "unknown session-linked table"):
                import_sessions(target_dir, target, exported.import_file)

    def test_refuses_unknown_source_session_linkage_and_invalid_paths(self) -> None:
        """Fail closed before writing an artifact for unknown state or bad explicit paths."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            source_dir.mkdir()
            database = root / "source.db"
            self._create_database(database, source_dir, root / "other-project")
            with _sqlite_connection(database) as connection:
                connection.execute(
                    "CREATE TABLE future_session_state (session_id TEXT)"
                )

            with self.assertRaisesRegex(TransferError, "unknown session-linked table"):
                export_sessions(database, source_dir, root / "exports")
            with self.assertRaisesRegex(TransferError, "absolute"):
                export_sessions(database, Path("relative"), root / "exports")
            with self.assertRaisesRegex(TransferError, "regular"):
                export_sessions(root / "missing.db", source_dir, root / "exports")

    def test_refuses_ambiguous_projects_and_incompatible_destination_columns(
        self,
    ) -> None:
        """Reject ambiguous directory ownership and target layouts before replacement."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_dir = root / "target-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir()
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(target, target_dir, other_dir, target=True)
            with _sqlite_connection(source) as connection:
                connection.execute(
                    "INSERT INTO project VALUES ('duplicate', ?)", (str(source_dir),)
                )
            with self.assertRaisesRegex(TransferError, "ambiguously"):
                export_sessions(source, source_dir, root / "exports")
            with _sqlite_connection(source) as connection:
                connection.execute("DELETE FROM project WHERE id = 'duplicate'")
            exported = export_sessions(source, source_dir, root / "exports")
            with _sqlite_connection(target) as connection:
                connection.execute("ALTER TABLE event ADD COLUMN future_state TEXT")
            before = target.read_bytes()
            with self.assertRaisesRegex(TransferError, "incompatible"):
                import_sessions(target_dir, target, exported.import_file)
            self.assertEqual(target.read_bytes(), before)

    def test_global_project_resolves_only_from_the_exact_session_directory(
        self,
    ) -> None:
        """Allow non-Git global sessions without including a different directory."""
        with _temporary_directory() as root:
            source_dir = root / "global-source"
            other_dir = root / "global-other"
            source_dir.mkdir()
            other_dir.mkdir()
            database = root / "global.db"
            self._create_database(database, root / "registered", root / "other-project")
            with _sqlite_connection(database) as connection:
                connection.execute(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "global-exact",
                        "global",
                        str(source_dir),
                        None,
                        None,
                        None,
                        b"exact",
                    ),
                )
                connection.execute(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "global-other",
                        "global",
                        str(other_dir),
                        None,
                        None,
                        None,
                        b"other",
                    ),
                )
                for table in SIMPLE_SESSION_CHILD_TABLES:
                    connection.executemany(
                        f'INSERT INTO "{table}" VALUES (?, ?, ?)',
                        [
                            (f"{table}-global-exact", "global-exact", b"exact"),
                            (f"{table}-global-other", "global-other", b"other"),
                        ],
                    )
                connection.executemany(
                    "INSERT INTO part VALUES (?, ?, ?, ?)",
                    [
                        (
                            "part-global-exact",
                            "global-exact",
                            "message-global-exact",
                            b"exact",
                        ),
                        (
                            "part-global-other",
                            "global-other",
                            "message-global-other",
                            b"other",
                        ),
                    ],
                )
                connection.executemany(
                    "INSERT INTO event_sequence VALUES (?, ?)",
                    [("global-exact", b"exact"), ("global-other", b"other")],
                )
                connection.executemany(
                    "INSERT INTO event VALUES (?, ?, ?)",
                    [
                        ("event-global-exact", "global-exact", b"exact"),
                        ("event-global-other", "global-other", b"other"),
                    ],
                )

            outcome = export_sessions(database, source_dir, root / "exports")

            self.assertEqual(outcome.source_project_id, "global")
            with _sqlite_connection(outcome.import_file) as archive:
                self.assertEqual(
                    archive.execute("SELECT id FROM session").fetchall(),
                    [("global-exact",)],
                )
            before = self._database_state(database)
            with self.assertRaisesRegex(TransferError, "belongs to another project"):
                import_sessions(other_dir, database, outcome.import_file)
            self.assertEqual(self._database_state(database), before)

    def test_export_cli_reports_canonical_directory_and_resolved_project_id(
        self,
    ) -> None:
        """Report only export identity metadata and never session bodies to stdout."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            source_dir.mkdir()
            database = root / "source.db"
            self._create_database(database, source_dir, root / "other-project")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "export",
                        "--db",
                        str(database),
                        "--project-dir",
                        str(source_dir),
                        "--export-dir",
                        str(root / "exports"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(
                f"export_dir: {(root / 'exports').resolve()}", output.getvalue()
            )
            self.assertIn("source_project_id: source", output.getvalue())
            self.assertNotIn("source-root", output.getvalue())

    def test_import_refuses_session_ids_owned_by_another_project_without_mutation(
        self,
    ) -> None:
        """Keep all target state unchanged when an imported ID belongs elsewhere."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_dir = root / "target-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir()
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(target, target_dir, other_dir, target=True)
            exported = export_sessions(source, source_dir, root / "exports")
            with _sqlite_connection(target) as connection:
                connection.execute(
                    "UPDATE session SET project_id = 'other', directory = ? WHERE id = 'root'",
                    (str(other_dir),),
                )
            before = self._database_state(target)

            with self.assertRaisesRegex(TransferError, "belongs to another project"):
                import_sessions(target_dir, target, exported.import_file)

            self.assertEqual(self._database_state(target), before)
            self.assert_foreign_keys_clean(target)

    def test_import_rolls_back_after_child_insert_conflict(self) -> None:
        """Restore every target row when a child insert fails after replacement begins."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_dir = root / "target-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir()
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(target, target_dir, other_dir, target=True)
            exported = export_sessions(source, source_dir, root / "exports")
            with _sqlite_connection(target) as connection:
                connection.execute(
                    "UPDATE part SET id = 'part-stale-root' WHERE id = 'part-root'"
                )
                connection.execute(
                    "INSERT INTO part VALUES "
                    "('part-root', 'unrelated', 'message-unrelated', ?)",
                    (b"unrelated-conflict",),
                )
            before = self._database_state(target)

            with self.assertRaisesRegex(TransferError, "session import failed"):
                import_sessions(target_dir, target, exported.import_file)

            self.assertEqual(self._database_state(target), before)
            self.assert_foreign_keys_clean(target)

    def test_export_refuses_cross_session_part_message_parent(self) -> None:
        """Refuse selected parts whose message parent belongs to an unselected session."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            source_dir.mkdir()
            source = root / "source.db"
            self._create_database(source, source_dir, root / "other-project")
            with _sqlite_connection(source) as connection:
                connection.execute(
                    "UPDATE part SET message_id = 'message-unrelated' WHERE session_id = 'root'"
                )

            with self.assertRaisesRegex(TransferError, "foreign key parent"):
                export_sessions(source, source_dir, root / "exports")

    def test_export_refuses_parent_session_outside_project_scope(self) -> None:
        """Preserve parent trees even though OpenCode does not declare that foreign key."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            source_dir.mkdir()
            source = root / "source.db"
            self._create_database(source, source_dir, root / "other-project")
            with _sqlite_connection(source) as connection:
                connection.execute(
                    "UPDATE session SET parent_id = 'unrelated' WHERE id = 'child'"
                )

            with self.assertRaisesRegex(TransferError, "session parent"):
                export_sessions(source, source_dir, root / "exports")

    def test_import_uses_a_posix_path_relative_to_a_nested_target_project_directory(
        self,
    ) -> None:
        """Store a nonempty POSIX session path when the target lies below its worktree."""
        with _temporary_directory() as root:
            source_dir = root / "source-project"
            target_worktree = root / "target-worktree"
            target_dir = target_worktree / "nested-project"
            other_dir = root / "other-project"
            source_dir.mkdir()
            target_dir.mkdir(parents=True)
            source = root / "source.db"
            target = root / "target.db"
            self._create_database(source, source_dir, other_dir)
            self._create_database(
                target, target_dir, other_dir, target=True, worktree=target_worktree
            )
            exported = export_sessions(source, source_dir, root / "exports")

            import_sessions(target_dir, target, exported.import_file)

            with _sqlite_connection(target) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT DISTINCT path FROM session "
                        "WHERE id IN ('root', 'child', 'global-source')"
                    ).fetchall(),
                    [("nested-project",)],
                )

    @staticmethod
    def _create_database(
        database: Path,
        project_dir: Path,
        other_dir: Path,
        *,
        target: bool = False,
        worktree: Path | None = None,
    ) -> None:
        """Create a disposable complete session schema with scoped fixture state."""
        other_dir.mkdir(exist_ok=True)
        with _sqlite_connection(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE project_directory (project_id TEXT, directory TEXT, "
                "FOREIGN KEY(project_id) REFERENCES project(id))"
            )
            connection.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                "directory TEXT, path TEXT, workspace_id TEXT, "
                "parent_id TEXT, marker BLOB NOT NULL)"
            )
            for table in SIMPLE_SESSION_CHILD_TABLES:
                connection.execute(
                    f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, '
                    "marker BLOB NOT NULL, FOREIGN KEY(session_id) REFERENCES session(id))"
                )
            connection.execute(
                "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "message_id TEXT NOT NULL, marker BLOB NOT NULL, "
                "FOREIGN KEY(session_id) REFERENCES session(id), "
                "FOREIGN KEY(message_id) REFERENCES message(id))"
            )
            connection.execute(
                "CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, marker BLOB NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE event (id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, "
                "marker BLOB NOT NULL, FOREIGN KEY(aggregate_id) "
                "REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE)"
            )
            project_id = "target" if target else "source"
            project_worktree = worktree or project_dir
            connection.executemany(
                "INSERT INTO project VALUES (?, ?)",
                [
                    (project_id, str(project_worktree)),
                    ("other", str(other_dir)),
                    ("global", "/global"),
                ],
            )
            connection.execute(
                "INSERT INTO project_directory VALUES (?, ?)",
                (project_id, str(project_dir)),
            )
            rows = [
                (
                    "root",
                    project_id,
                    str(project_dir),
                    None,
                    "source-workspace",
                    None,
                    b"source-root",
                ),
                (
                    "child",
                    project_id,
                    str(project_dir),
                    None,
                    "source-workspace",
                    "root",
                    b"source-child",
                ),
                (
                    "global-source",
                    "global",
                    str(project_dir),
                    None,
                    "source-workspace",
                    None,
                    b"source-global",
                ),
                (
                    "unrelated",
                    "other",
                    str(other_dir),
                    None,
                    "other-workspace",
                    None,
                    b"other",
                ),
            ]
            if target:
                rows[0] = (
                    "root",
                    "target",
                    str(project_dir),
                    None,
                    "stale",
                    None,
                    b"stale-root",
                )
                rows[1] = (
                    "child",
                    "target",
                    str(project_dir),
                    None,
                    "stale",
                    "root",
                    b"stale-child",
                )
                rows[2] = (
                    "global-source",
                    "global",
                    str(project_dir),
                    None,
                    "stale",
                    None,
                    b"stale-global",
                )
            connection.executemany(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
            child_rows = [
                ("root", b"source-root" if not target else b"stale-root"),
                ("child", b"source-child" if not target else b"stale-child"),
                ("global-source", b"source-global" if not target else b"stale-global"),
                ("unrelated", b"other"),
            ]
            for table in SIMPLE_SESSION_CHILD_TABLES:
                connection.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?)',
                    [
                        (f"{table}-{session_id}", session_id, marker)
                        for session_id, marker in child_rows
                    ],
                )
            connection.executemany(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                [
                    (f"part-{session_id}", session_id, f"message-{session_id}", marker)
                    for session_id, marker in child_rows
                ],
            )
            connection.executemany(
                "INSERT INTO event_sequence VALUES (?, ?)", child_rows
            )
            connection.executemany(
                "INSERT INTO event VALUES (?, ?, ?)",
                [
                    (f"event-{session_id}", session_id, marker)
                    for session_id, marker in child_rows
                ],
            )

    @staticmethod
    def _database_state(database: Path) -> dict[str, list[tuple[object, ...]]]:
        """Return every fixture row in a deterministic order for rollback assertions."""
        tables = ("project", "project_directory", "session", *SESSION_TABLES)
        with _sqlite_connection(database) as connection:
            return {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY 1'
                ).fetchall()
                for table in tables
            }

    def assert_foreign_keys_clean(self, database: Path) -> None:
        """Assert a fixture remains internally valid after a refused import."""
        with _sqlite_connection(database) as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )


if __name__ == "__main__":
    unittest.main()
