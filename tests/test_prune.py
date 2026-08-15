"""Focused active-database session pruning and logical-size contracts."""

from __future__ import annotations

import io
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli
import opencode_db.prune as prune_module
from opencode_db.model import EXIT_OPERATIONAL_FAILURE, EXIT_PRECONDITION_REFUSED
from opencode_db.prune import (
    PruneRequest,
    PruneError,
    PruneOperationalError,
    apply_prune_plan,
    plan_prune,
    prune_sessions,
    session_logical_sizes,
)


_TEST_ROOT = Path("/tmp/opencode-db-v1")
_CHILD_TABLES = (
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


@contextmanager
def _connection(database: Path):
    """Yield one disposable SQLite connection and always close it."""
    connection = sqlite3.connect(database)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


class SessionPruneTests(unittest.TestCase):
    """Prove parse boundaries, selection policies, estimates, and atomic mutation."""

    def test_parse_requires_one_positive_selector_and_bounded_target_size(self) -> None:
        """Reject absent, conflicting, malformed, zero, and negative prune selectors."""
        valid = cli.parse_command(
            [
                "prune",
                "--db",
                "/tmp/opencode.db",
                "--oldest",
                "2",
                "--estimate-size",
                "--yes",
            ]
        )
        self.assertEqual(valid.command, "prune")
        self.assertEqual(valid.oldest, "2")
        self.assertTrue(valid.estimate_size)
        self.assertTrue(valid.yes)
        for arguments in (
            ["prune", "--db", "/tmp/opencode.db"],
            ["prune", "--db", "/tmp/opencode.db", "--oldest", "1", "--keep-newest", "1"],
            ["prune", "--db", "/tmp/opencode.db", "--oldest", "0"],
            ["prune", "--db", "/tmp/opencode.db", "--oldest", "-1"],
            ["prune", "--db", "/tmp/opencode.db", "--oldest", "1h"],
            ["prune", "--db", "/tmp/opencode.db", "--target-size", "0B"],
            ["prune", "--db", "/tmp/opencode.db", "--target-size", "1KB"],
            ["prune", "--db", "/tmp/opencode.db", "--target-size", "1.5MiB"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(cli.CliUsageError):
                    cli.parse_command(arguments)

    def test_cli_refuses_detached_prune_without_mutating(self) -> None:
        """Require interactive authorization before crossing the prune mutation boundary."""
        cases = (
            ("detached stdout", _ReadTrackingTty("y\n"), io.StringIO()),
            ("detached stdin", _DetachedStream("y\n"), _TtyStream()),
            ("stdin isatty error", _OSErrorTty("y\n"), _TtyStream()),
        )
        for name, stdin, stdout in cases:
            with self.subTest(name=name), patch.object(
                cli, "prune_sessions"
            ) as prune:
                stderr = io.StringIO()
                with patch.object(sys, "stdin", stdin), patch.object(
                    sys, "stdout", stdout
                ), patch.object(sys, "stderr", stderr):
                    exit_code = cli.main(
                        ["prune", "--db", "/tmp/opencode.db", "--oldest", "1"]
                    )

                self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
                self.assertEqual(stdin.readline_count, 0)
                self.assertIn("terminal", stderr.getvalue())
                prune.assert_not_called()

    def test_cli_requires_exact_confirmation_and_yes_bypasses_prompt(self) -> None:
        """Mutate only after exact terminal confirmation or an explicit yes flag."""
        outcome = prune_module.PruneOutcome(1, None, None)
        for reply in ("n\n", "Y\n", " y\n", "y \n", "y", "", "\n"):
            with self.subTest(reply=reply), patch.object(
                cli, "prune_sessions", return_value=outcome
            ) as prune:
                stdin = _TtyStream(reply)
                stdout = _TtyStream()
                stderr = _TtyStream()
                with patch.object(sys, "stdin", stdin), patch.object(
                    sys, "stdout", stdout
                ), patch.object(sys, "stderr", stderr):
                    exit_code = cli.main(
                        ["prune", "--db", "/tmp/opencode.db", "--oldest", "1"]
                    )
                self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
                self.assertIn("cancelled", stderr.getvalue())
                prune.assert_not_called()

        with patch.object(cli, "prune_sessions", return_value=outcome) as prune:
            stdin = _InterruptingTty()
            stdout = _TtyStream()
            stderr = _TtyStream()
            with patch.object(sys, "stdin", stdin), patch.object(
                sys, "stdout", stdout
            ), patch.object(sys, "stderr", stderr):
                exit_code = cli.main(
                    ["prune", "--db", "/tmp/opencode.db", "--oldest", "1"]
                )
            self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
            self.assertIn("cancelled", stderr.getvalue())
            prune.assert_not_called()

        for reply in ("y\n", "y\r", "y\r\n"):
            with self.subTest(reply=reply), patch.object(
                cli, "prune_sessions", return_value=outcome
            ) as prune:
                stdin = _TtyStream(reply)
                stdout = _TtyStream()
                stderr = _TtyStream()
                with patch.object(sys, "stdin", stdin), patch.object(
                    sys, "stdout", stdout
                ), patch.object(sys, "stderr", stderr):
                    exit_code = cli.main(
                        ["prune", "--db", "/tmp/opencode.db", "--oldest", "1"]
                    )
                self.assertEqual(exit_code, 0)
                self.assertIn("Prune matching sessions? [y/N] ", stdout.getvalue())
                prune.assert_called_once()

        with patch.object(cli, "prune_sessions", return_value=outcome) as prune:
            code, stdout, stderr = self._run(
                [
                    "prune",
                    "--db",
                    "/tmp/opencode.db",
                    "--oldest",
                    "1",
                    "--yes",
                ]
            )
            self.assertEqual(code, 0)
            self.assertNotIn("[y/N]", stdout)
            self.assertEqual(stderr, "")
            prune.assert_called_once()

    def test_count_time_and_target_policies_preserve_unrelated_projects(self) -> None:
        """Delete only selected project rows using deterministic count, time, and size policies."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)

            outcome = prune_sessions(database, oldest="1", project_id="a")
            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertIsNone(outcome.deleted_logical_bytes)
            self.assertEqual(self._session_ids(database), ["a-2", "a-3", "b-1"])

            outcome = prune_sessions(database, keep_newest="1d", project_id="a", now_ms=3 * 86_400_000 + 1)
            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a-3", "b-1"])

            self._insert_session(database, "a-4", "a", 4 * 86_400_000, b"x" * 80)
            with _connection(database) as connection:
                newest_size = session_logical_sizes(connection, session_id="a-4")["a-4"]
            outcome = prune_sessions(
                database, target_size=f"{newest_size}B", project_id="a"
            )
            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a-4", "b-1"])

    def test_reviewed_plan_is_read_only_and_applies_the_exact_previewed_selection(self) -> None:
        """Plan from a read snapshot, hide IDs in its repr, and apply that exact evidence."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            before = database.read_bytes()
            with _connection(database) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()

            reviewed = plan_prune(
                PruneRequest(str(database), project_id="a", oldest="1"), now_ms=4 * 86_400_000
            )

            self.assertEqual(reviewed.preview.sessions_to_prune, 1)
            self.assertEqual(reviewed.preview.sessions_to_keep, 2)
            self.assertEqual(reviewed.preview.oldest_surviving_session_updated, 2 * 86_400_000)
            self.assertEqual(database.read_bytes(), before)
            with _connection(database) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), journal_mode)
            self.assertNotIn("a-1", repr(reviewed))

            outcome = apply_prune_plan(reviewed)

            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a-2", "a-3", "b-1"])

    def test_stale_reviewed_plan_refuses_before_any_deletion(self) -> None:
        """Refuse a changed selection under the writer lock and retain all rows."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            reviewed = plan_prune(PruneRequest(str(database), project_id="a", oldest="1"))
            self._insert_session(database, "a-0", "a", 0, b"new-after-preview")

            with self.assertRaisesRegex(PruneError, "preview is stale.*rerun"):
                apply_prune_plan(reviewed)

            self.assertEqual(self._session_ids(database), ["a-0", "a-1", "a-2", "a-3", "b-1"])

    def test_direct_zero_selection_without_vacuum_does_not_open_writable_sqlite(self) -> None:
        """Return a no-op from reviewed read evidence without entering the writer phase."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with patch("opencode_db.prune._open_prune_connection") as open_writable:
                outcome = prune_sessions(database, oldest="1", project_id="a")

            self.assertEqual(outcome.deleted_sessions, 0)
            open_writable.assert_not_called()

    def test_read_only_plan_observes_committed_wal_rows_without_changing_main_or_wal(self) -> None:
        """Read the committed WAL snapshot without checkpointing or opening SQLite rw."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            writer = sqlite3.connect(database)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode = WAL").fetchone(), ("wal",))
                writer.execute(
                    "INSERT INTO session VALUES ('wal-only', 'a', '/work/wal', 9, ?)",
                    (b"wal-only-payload",),
                )
                writer.commit()
                wal = Path(f"{database}-wal")
                main_before = database.read_bytes()
                wal_before = wal.read_bytes()

                reviewed = plan_prune(PruneRequest(str(database), oldest="1"))

                self.assertEqual(reviewed.preview.sessions_to_prune, 1)
                self.assertEqual(reviewed.preview.sessions_to_keep, 0)
                self.assertEqual(database.read_bytes(), main_before)
                self.assertEqual(wal.read_bytes(), wal_before)
            finally:
                writer.close()

    def test_project_scope_ignores_unrelated_drift_without_estimates_but_binds_displayed_total(self) -> None:
        """Avoid false scoped drift unless a requested database-wide projection changed."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            reviewed = plan_prune(PruneRequest(str(database), project_id="a", oldest="1"))
            with _connection(database) as connection:
                connection.execute("UPDATE session SET payload = ? WHERE id = 'b-1'", (b"changed",))

            outcome = apply_prune_plan(reviewed)

            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a-2", "a-3", "b-1"])

            reviewed = plan_prune(
                PruneRequest(str(database), project_id="a", oldest="1", estimate_size=True)
            )
            with _connection(database) as connection:
                connection.execute("UPDATE session SET payload = ? WHERE id = 'b-1'", (b"changed-again",))

            with self.assertRaisesRegex(PruneError, "preview is stale.*rerun"):
                apply_prune_plan(reviewed)

    def test_target_size_binds_selection_without_false_drift_from_undisplayed_sizes(self) -> None:
        """Keep target-size selection authoritative without displaying or comparing its size map."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            with _connection(database) as connection:
                newest_size = session_logical_sizes(connection, session_id="a-3")["a-3"]
            reviewed = plan_prune(
                PruneRequest(str(database), project_id="a", target_size=f"{newest_size}B")
            )
            with _connection(database) as connection:
                connection.execute("UPDATE session SET payload = ? WHERE id = 'a-3'", (b"x",))

            outcome = apply_prune_plan(reviewed)

            self.assertEqual(outcome.deleted_sessions, 2)
            self.assertEqual(self._session_ids(database), ["a-3", "b-1"])

    def test_planning_caps_and_deadline_refuse_without_persisted_values(self) -> None:
        """Enforce candidate and identifier bounds plus deadline before any writable phase."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_session(database, "one", "a", 1, b"one")
            self._insert_session(database, "two", "a", 2, b"two")
            request = PruneRequest(str(database), oldest="1")

            with patch.object(prune_module, "_MAX_PRUNE_CANDIDATES", 1):
                with self.assertRaisesRegex(PruneOperationalError, "candidate evidence") as error:
                    plan_prune(request)
            self.assertNotIn("one", str(error.exception))

            with patch.object(prune_module, "_MAX_PRUNE_CANDIDATES", 2):
                self.assertEqual(plan_prune(request).preview.sessions_to_prune, 1)

            with patch.object(prune_module, "_MAX_PRUNE_SESSION_ID_BYTES", 2):
                with self.assertRaisesRegex(PruneOperationalError, "candidate evidence") as error:
                    plan_prune(request)
            self.assertNotIn("one", str(error.exception))
            with patch.object(prune_module, "_MAX_PRUNE_SESSION_ID_BYTES", 3):
                self.assertEqual(plan_prune(request).preview.sessions_to_prune, 1)

            with patch.object(prune_module, "_MAX_PRUNE_EVIDENCE_BYTES", 21):
                with self.assertRaisesRegex(PruneOperationalError, "candidate evidence"):
                    plan_prune(request)
            with patch.object(prune_module, "_MAX_PRUNE_EVIDENCE_BYTES", 22):
                self.assertEqual(plan_prune(request).preview.sessions_to_prune, 1)

            with patch.object(prune_module.time, "monotonic", side_effect=(0.0, 11.0)):
                with self.assertRaisesRegex(PruneOperationalError, "timed out"):
                    plan_prune(request)

    def test_writer_revalidation_reuses_caps_and_deadline_then_rolls_back(self) -> None:
        """Bound application work after locking and retain rows after a revalidation refusal."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_session(database, "one", "a", 1, b"one")
            self._insert_session(database, "two", "a", 2, b"two")
            reviewed = plan_prune(PruneRequest(str(database), oldest="1"))

            with patch.object(prune_module, "_MAX_PRUNE_CANDIDATES", 1):
                with self.assertRaisesRegex(PruneOperationalError, "candidate evidence"):
                    apply_prune_plan(reviewed)
            self.assertEqual(self._session_ids(database), ["one", "two"])

            with patch.object(prune_module.time, "monotonic", side_effect=(0.0, 11.0)):
                with self.assertRaisesRegex(PruneOperationalError, "timed out"):
                    apply_prune_plan(reviewed)
            self.assertEqual(self._session_ids(database), ["one", "two"])

    def test_keep_newest_count_uses_newest_first_id_tie_break(self) -> None:
        """Retain exactly the requested newest count and deterministically break ties."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            for session_id, updated in (("z", 10), ("a", 10), ("new", 11)):
                self._insert_session(database, session_id, "a", updated, b"x")

            outcome = prune_sessions(database, keep_newest="2", project_id="a")

            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a", "new"])

    def test_oldest_time_uses_the_oldest_matching_window_and_tie_break(self) -> None:
        """Delete the inclusive age window starting at the oldest matching timestamp."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            for session_id, updated in (("z", 10), ("a", 10), ("later", 11), ("new", 86_400_011)):
                self._insert_session(database, session_id, "a", updated, b"x")

            count_outcome = prune_sessions(database, oldest="1")
            self.assertEqual(count_outcome.deleted_sessions, 1)
            self.assertEqual(self._session_ids(database), ["a", "later", "new"])
            outcome = prune_sessions(database, oldest="1d")

            self.assertEqual(outcome.deleted_sessions, 2)
            self.assertEqual(self._session_ids(database), ["new"])

    def test_duplicate_session_ids_are_refused_globally_before_project_selection(self) -> None:
        """Refuse scoped pruning when an unscoped duplicate could share its delete ID."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database, session_id_primary_key=False)
            with _connection(database) as connection:
                connection.executemany(
                    "INSERT INTO session (id, project_id, directory, time_updated, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        ("shared", "a", "/work/a", 1, b"a"),
                        ("shared", "b", "/work/b", 2, b"b"),
                    ),
                )
            before = database.read_bytes()

            with self.assertRaisesRegex(PruneError, "session IDs are not globally unique"):
                prune_sessions(database, oldest="1", project_id="a")

            self.assertEqual(database.read_bytes(), before)
            with _connection(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM session").fetchone(), (2,))

    def test_known_foreign_keys_cannot_cross_the_prune_selection_boundary(self) -> None:
        """Refuse composite known-table links in either ownership direction before cascades run."""
        for child_selected in (True, False):
            with self.subTest(child_selected=child_selected), self._temporary_directory() as root:
                database = root / "opencode.db"
                self._create_database(database, part_message_foreign_key=True)
                with _connection(database) as connection:
                    connection.executemany(
                        "INSERT INTO session (id, project_id, directory, time_updated, payload) VALUES (?, 'a', ?, ?, ?)",
                        (("old", "/work/old", 1, b"old"), ("new", "/work/new", 2, b"new")),
                    )
                    message_session = "new" if child_selected else "old"
                    part_session = "old" if child_selected else "new"
                    connection.execute(
                        "INSERT INTO message (id, session_id, payload) VALUES ('message', ?, ?)",
                        (message_session, b"message"),
                    )
                    connection.execute(
                        "INSERT INTO part (id, session_id, payload, message_id, message_owner) VALUES ('part', ?, ?, 'message', ?)",
                        (part_session, b"part", message_session),
                    )
                before = database.read_bytes()

                with self.assertRaisesRegex(PruneError, "foreign key crosses prune selection"):
                    prune_sessions(database, oldest="1")

                self.assertEqual(database.read_bytes(), before)
                self.assertEqual(self._session_ids(database), ["new", "old"])
                with _connection(database) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM part").fetchone(), (1,))

    def test_estimates_include_all_known_child_rows_and_no_match_is_a_no_op(self) -> None:
        """Report logical byte estimates without printing persisted session payloads."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_session(database, "old", "a", 1, b"secret-payload")
            with _connection(database) as connection:
                connection.execute("INSERT INTO project VALUES ('empty', 'Empty')")
            before = database.read_bytes()
            no_match = prune_sessions(database, oldest="1", project_id="empty")
            self.assertEqual(no_match.deleted_sessions, 0)
            self.assertEqual(database.read_bytes(), before)
            with patch(
                "opencode_db.prune._open_prune_connection",
                wraps=prune_module._open_prune_connection,
            ) as open_connection:
                vacuumed = prune_sessions(database, oldest="1", project_id="empty", vacuum=True)
            self.assertEqual(vacuumed.deleted_sessions, 0)
            self.assertEqual(vacuumed.physical_database_bytes, database.stat().st_size)
            self.assertEqual(open_connection.call_count, 1)
            outcome = prune_sessions(database, oldest="1", estimate_size=True)

            self.assertGreater(outcome.deleted_logical_bytes, len("secret-payload"))
            self.assertGreater(outcome.logical_database_bytes, 0)
            code, stdout, stderr = self._run(
                [
                    "prune",
                    "--db",
                    str(database),
                    "--oldest",
                    "1",
                    "--estimate-size",
                    "--yes",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("estimated_logical_bytes_deleted: 0", stdout)
            self.assertNotIn("secret-payload", stdout)

    def test_target_size_retains_all_at_the_exact_limit_and_deletes_all_below_one_session(self) -> None:
        """Keep all sessions at their complete total and select all once the newest cannot fit."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            with _connection(database) as connection:
                total = sum(session_logical_sizes(connection).values())

            retained = prune_sessions(database, target_size=f"{total}B")
            self.assertEqual(retained.deleted_sessions, 0)
            deleted = prune_sessions(database, target_size="1B")
            self.assertEqual(deleted.deleted_sessions, 4)
            self.assertEqual(self._session_ids(database), [])

    def test_unknown_session_linkage_and_foreign_key_failure_roll_back(self) -> None:
        """Refuse unknown ownership and retain all rows when post-delete checks fail."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            before = database.read_bytes()
            with _connection(database) as connection:
                connection.execute("CREATE TABLE future_state (session_id TEXT)")
            with self.assertRaisesRegex(PruneError, "unknown session-linked table"):
                prune_sessions(database, oldest="1")
            self.assertEqual(self._session_ids(database), ["a-1", "a-2", "a-3", "b-1"])

            database.unlink()
            self._create_database(database, parent_foreign_key=True)
            self._insert_session(database, "parent", "a", 1, b"p")
            self._insert_session(database, "child", "a", 2, b"c", parent_id="parent")
            before = database.read_bytes()
            with self.assertRaises(PruneError):
                prune_sessions(database, oldest="1")
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(self._session_ids(database), ["child", "parent"])

    def test_case_variant_session_trigger_and_foreign_key_are_refused_before_mutation(self) -> None:
        """Reject SQLite case-variant session dependencies before deleting any rows."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            with _connection(database) as connection:
                connection.execute(
                    "CREATE TRIGGER session_audit AFTER DELETE ON Session BEGIN SELECT 1; END"
                )
            before = database.read_bytes()

            with self.assertRaisesRegex(PruneError, "session schema is incompatible"):
                prune_sessions(database, oldest="1")
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(self._session_ids(database), ["a-1", "a-2", "a-3", "b-1"])

            database.unlink()
            self._create_database(database)
            self._insert_sessions(database)
            with _connection(database) as connection:
                connection.execute(
                    "CREATE TABLE future_state (session_id TEXT REFERENCES Session(id))"
                )
            before = database.read_bytes()

            with self.assertRaisesRegex(PruneError, "unknown session-linked table"):
                prune_sessions(database, oldest="1")
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(self._session_ids(database), ["a-1", "a-2", "a-3", "b-1"])

    def test_project_filter_requires_an_exact_existing_project_and_vacuum_is_explicit(self) -> None:
        """Reject missing project IDs and compact only after successful explicit pruning."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            with self.assertRaisesRegex(PruneError, "project ID was not found"):
                prune_sessions(database, oldest="1", project_id="missing")

            outcome = prune_sessions(database, oldest="1", project_id="a", vacuum=True)
            self.assertEqual(outcome.deleted_sessions, 1)
            self.assertIsNotNone(outcome.physical_database_bytes)
            self.assertEqual(outcome.physical_database_bytes, database.stat().st_size)

    def test_post_commit_vacuum_failure_reports_the_committed_prune(self) -> None:
        """Expose committed deletion and warn against repeating a failed vacuum request."""
        with self._temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            self._insert_sessions(database)
            with patch(
                "opencode_db.prune._vacuum",
                side_effect=PruneOperationalError("database vacuum failed"),
            ):
                code, stdout, stderr = self._run(
                    [
                        "prune",
                        "--db",
                        str(database),
                        "--project-id",
                        "a",
                        "--oldest",
                        "1",
                        "--vacuum",
                        "--yes",
                    ]
                )

            self.assertEqual(code, EXIT_OPERATIONAL_FAILURE)
            self.assertIn("pruned_sessions: 1", stdout)
            self.assertIn("do not repeat the prune request", stderr)
            self.assertEqual(self._session_ids(database), ["a-2", "a-3", "b-1"])

    @staticmethod
    @contextmanager
    def _temporary_directory():
        """Return a private temporary-directory context under the approved root."""
        _TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_TEST_ROOT, 0o700)
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory:
            yield Path(directory)

    @staticmethod
    def _create_database(
        database: Path,
        *,
        parent_foreign_key: bool = False,
        session_id_primary_key: bool = True,
        part_message_foreign_key: bool = False,
    ) -> None:
        """Create a complete disposable known-session schema."""
        parent = ", parent_id TEXT REFERENCES session(id)" if parent_foreign_key else ""
        session_id = "id TEXT PRIMARY KEY" if session_id_primary_key else "id TEXT"
        session_reference = " REFERENCES session(id)" if session_id_primary_key else ""
        message_unique = ", UNIQUE(id, session_id)" if part_message_foreign_key else ""
        part_foreign_key = (
            ", message_id TEXT, message_owner TEXT, "
            "FOREIGN KEY (message_id, message_owner) REFERENCES Message(id, session_id) ON DELETE CASCADE"
            if part_message_foreign_key
            else ""
        )
        with _connection(database) as connection:
            connection.executescript(
                f"""
                PRAGMA foreign_keys = ON;
                CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE session ({session_id}, project_id TEXT NOT NULL REFERENCES project(id), directory TEXT NOT NULL, time_updated INTEGER NOT NULL, payload BLOB{parent});
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB{message_unique});
                CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB{part_foreign_key});
                CREATE TABLE todo (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE session_input (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE session_context_epoch (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE session_share (id TEXT PRIMARY KEY, session_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE event_sequence (id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL{session_reference}, payload BLOB);
                CREATE TABLE event (id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL{session_reference}, payload BLOB);
                INSERT INTO project VALUES ('a', 'A');
                INSERT INTO project VALUES ('b', 'B');
                """
            )

    @staticmethod
    def _insert_sessions(database: Path) -> None:
        """Populate two project scopes with deterministically ranked sessions."""
        for session_id, project_id, updated in (
            ("a-1", "a", 86_400_000),
            ("a-2", "a", 2 * 86_400_000),
            ("a-3", "a", 3 * 86_400_000),
            ("b-1", "b", 86_400_000),
        ):
            SessionPruneTests._insert_session(database, session_id, project_id, updated, b"body")

    @staticmethod
    def _insert_session(
        database: Path, session_id: str, project_id: str, updated: int, payload: bytes, *, parent_id: str | None = None
    ) -> None:
        """Insert one session and one payload row into every known child family."""
        connection = sqlite3.connect(database)
        try:
            columns = "id, project_id, directory, time_updated, payload" + (", parent_id" if parent_id is not None else "")
            values: tuple[object, ...] = (session_id, project_id, "/work/project", updated, payload) + ((parent_id,) if parent_id is not None else ())
            connection.execute(f"INSERT INTO session ({columns}) VALUES ({', '.join('?' for _ in values)})", values)
            for table in _CHILD_TABLES:
                connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (f"{table}-{session_id}", session_id, payload))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _session_ids(database: Path) -> list[str]:
        """Read only fixture IDs in deterministic order for assertions."""
        with _connection(database) as connection:
            return [row[0] for row in connection.execute("SELECT id FROM session ORDER BY id")]

    @staticmethod
    def _run(arguments: list[str]) -> tuple[int, str, str]:
        """Run the public CLI and capture its bounded human output."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()


class _TtyStream(io.StringIO):
    """Provide an in-memory stream that reports terminal capability."""

    def isatty(self) -> bool:
        """Return true so prune confirmation paths can be exercised."""
        return True


class _ReadTrackingTty(_TtyStream):
    """Record attempts to read confirmation input from one terminal stream."""

    def __init__(self, initial_value: str = "") -> None:
        """Initialize a terminal stream with no recorded reads."""
        super().__init__(initial_value)
        self.readline_count = 0

    def readline(self, *args: object, **kwargs: object) -> str:
        """Count and perform one confirmation read."""
        self.readline_count += 1
        return super().readline(*args, **kwargs)


class _DetachedStream(_ReadTrackingTty):
    """Provide a readable stream that reports no terminal capability."""

    def isatty(self) -> bool:
        """Return false to model redirected stdin."""
        return False


class _OSErrorTty(_ReadTrackingTty):
    """Model a stream whose terminal capability cannot be queried."""

    def isatty(self) -> bool:
        """Raise the supported terminal-probe failure."""
        raise OSError("terminal probe failed")


class _InterruptingTty(_TtyStream):
    """Model an operator interrupt at the prune confirmation prompt."""

    def readline(self) -> str:
        """Interrupt instead of returning authorization input."""
        raise KeyboardInterrupt


if __name__ == "__main__":
    unittest.main()
