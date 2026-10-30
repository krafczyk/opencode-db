"""Verify actionable prune error context without leaking raw SQLite content."""

from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
import errno
import io
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli, prune
from opencode_db.prune_diagnostics import describe_prune_failure
from opencode_db.target import StorageSpace
import test_prune as fixtures

_ROOT = Path("/tmp/opencode-db-v1")


def _sqlite_failure(code):
    """Return a coded SQLite failure with deliberately private raw attributes."""
    error = sqlite3.OperationalError("secret transcript SQL SELECT token FROM private_table")
    error.sqlite_errorcode = code
    error.sqlite_errorname = "PRIVATE_TOKEN_DO_NOT_PRINT"
    return error


class PruneDiagnosticsTests(unittest.TestCase):
    """Use only generated complete schemas and deterministic failure injection."""

    def _fixture(self, root):
        database = root / "opencode.db"
        fixtures.SessionPruneTests._create_database(database)
        fixtures.SessionPruneTests._insert_sessions(database)
        return database

    def _run(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_extended_sqlite_categories_are_resolved_without_raw_error_attributes(self):
        """Expose numeric/known symbolic reasons, never arbitrary messages or names."""
        for code, label in (
            (sqlite3.SQLITE_IOERR_FSYNC, "SQLITE_IOERR_FSYNC"),
            (sqlite3.SQLITE_READONLY_DIRECTORY, "SQLITE_READONLY_DIRECTORY"),
            (sqlite3.SQLITE_CANTOPEN, "SQLITE_CANTOPEN"),
            (sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY, "SQLITE_CONSTRAINT_FOREIGNKEY"),
            (sqlite3.SQLITE_CORRUPT, "SQLITE_CORRUPT"),
            (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED"),
            (sqlite3.SQLITE_NOTADB, "SQLITE_NOTADB"),
        ):
            with self.subTest(code=code):
                error = prune.PruneOperationalError("database vacuum failed", phase="vacuum")
                error.__cause__ = _sqlite_failure(code)
                text = describe_prune_failure(error, "/tmp/opencode-db-v1/test.db", "/tmp/opencode-db-v1/work")
                self.assertIn(label + f" ({code})", text)
                self.assertIn("phase: vacuum", text)
                self.assertIn("SQLite temporary directory:", text)
                self.assertIn("hint:", text)
                for secret in ("secret", "SELECT", "private_table", "PRIVATE_TOKEN"):
                    self.assertNotIn(secret, text)

    def test_actual_writer_contention_reports_lock_phase_and_preserves_rows(self):
        """An actual SQLite BUSY error survives writer-open and application wrappers."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database = self._fixture(Path(directory))
            before = fixtures.SessionPruneTests._session_ids(database)
            open_writer = prune._open_prune_connection
            def bounded(path):
                connection = open_writer(path)
                connection.execute("PRAGMA busy_timeout = 1")
                return connection
            with closing(sqlite3.connect(database)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                with patch.object(prune, "_open_prune_connection", side_effect=bounded):
                    code, _, error = self._run(["prune", "--db", str(database), "--oldest", "1", "--yes"])
                blocker.rollback()
            self.assertEqual(code, 5, error)
            self.assertIn("phase: application / acquiring writer lock", error)
            self.assertIn("SQLITE_BUSY (5)", error)
            self.assertIn("busy-wait limit: 10s", error)
            self.assertNotIn("time budget expired", error)
            self.assertEqual(fixtures.SessionPruneTests._session_ids(database), before)

    def test_delete_failure_names_only_the_fixed_known_table_and_rolls_back(self):
        """A constraint failure identifies its deletion step without exposing SQL or row values."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database = self._fixture(Path(directory))
            opened = prune._open_prune_connection
            class FailingDelete:
                def __init__(self, connection):
                    self.connection = connection
                def __getattr__(self, name):
                    return getattr(self.connection, name)
                def execute(self, statement, *args):
                    if statement.startswith("DELETE FROM"):
                        raise _sqlite_failure(sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY)
                    return self.connection.execute(statement, *args)
            with patch.object(prune, "_open_prune_connection", side_effect=lambda path: FailingDelete(opened(path))):
                code, _, error = self._run(["prune", "--db", str(database), "--oldest", "1", "--yes"])
            self.assertEqual(code, 5, error)
            self.assertIn("phase: application / deleting session-owned rows", error)
            self.assertIn("step: deleting known table " + prune.CHILD_SESSION_TABLES[0], error)
            self.assertIn("SQLITE_CONSTRAINT_FOREIGNKEY", error)
            self.assertNotIn("SELECT", error)
            self.assertEqual(len(fixtures.SessionPruneTests._session_ids(database)), 4)

    def test_database_location_uses_the_resolved_target_not_a_symlink_parent(self):
        """Capacity/location context belongs to the actual database filesystem."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            real, aliases = root / "real", root / "aliases"
            real.mkdir()
            aliases.mkdir()
            database = self._fixture(real)
            link = aliases / "alias.db"
            link.symlink_to(database)
            with patch.object(prune, "_vacuum", side_effect=_sqlite_failure(sqlite3.SQLITE_IOERR_FSYNC)):
                code, _, error = self._run(["prune", "--db", str(link), "--vacuum-only", "--yes"])
            self.assertEqual(code, 5, error)
            self.assertIn('database: "' + str(database) + '"', error)
            self.assertNotIn(str(link), error)

    def test_planning_failure_identifies_read_only_phase_and_sqlite_health_step(self):
        """An unreadable SQLite file reports where read-only planning failed."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database = Path(directory) / "not-a-database.db"
            database.write_bytes(b"not SQLite")
            code, _, error = self._run(["prune", "--db", str(database), "--vacuum-only", "--yes"])
            self.assertEqual(code, 5, error)
            self.assertIn("phase: planning / validation and selection", error)
            self.assertIn("step: SQLite quick integrity check", error)
            self.assertIn("SQLITE_NOTADB (26)", error)
            self.assertEqual(database.read_bytes(), b"not SQLite")

    def test_postcommit_vacuum_error_keeps_deletion_warning_and_extended_cause(self):
        """Detailed failure output cannot invite repeating an already committed selector."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database = self._fixture(root)
            work = root / "work"
            work.mkdir(mode=0o700)
            with patch.object(prune, "_vacuum", side_effect=_sqlite_failure(sqlite3.SQLITE_IOERR_FSYNC)):
                code, output, error = self._run([
                    "prune", "--db", str(database), "--oldest", "1", "--vacuum", "--work-dir", str(work), "--yes",
                ])
            self.assertEqual(code, 5, error)
            self.assertIn("pruned_sessions: 1", output)
            self.assertIn("do not repeat the prune request", error)
            self.assertIn("--vacuum-only", error)
            self.assertIn("phase: vacuum / physical compaction", error)
            self.assertIn("SQLITE_IOERR_FSYNC", error)
            self.assertIn(str(work), error)
            self.assertNotIn("secret", error)
            self.assertEqual(len(fixtures.SessionPruneTests._session_ids(database)), 3)

    def test_commit_busy_budget_expiry_is_not_lost_to_millisecond_rounding(self):
        """SQLite's finite COMMIT busy wait may expire just before the float deadline."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database = self._fixture(Path(directory))
            reviewed = prune.plan_prune(prune.PruneRequest(str(database), oldest="1", timeout_seconds=30))
            opened = prune._open_prune_connection
            class BusyCommit:
                def __init__(self, connection):
                    self.connection = connection
                def __getattr__(self, name):
                    return getattr(self.connection, name)
                def commit(self):
                    raise _sqlite_failure(sqlite3.SQLITE_BUSY)
            with patch.object(prune, "_open_prune_connection", side_effect=lambda path: BusyCommit(opened(path))):
                with self.assertRaises(prune.PruneOperationalError) as raised:
                    prune.apply_prune_plan(reviewed)
            text = describe_prune_failure(raised.exception, str(database))
            self.assertIn("prune application timed out", text)
            self.assertIn("phase: application / committing deletions", text)
            self.assertIn("SQLITE_BUSY", text)
            self.assertIn("30s", text)
            self.assertEqual(len(fixtures.SessionPruneTests._session_ids(database)), 4)

    def test_vacuum_only_timeout_names_its_budget_without_a_deletion_warning(self):
        """Report the vacuum budget and a cancellation cause without claiming a prune committed."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database = self._fixture(Path(directory))
            request = prune.PruneRequest(str(database), vacuum_only=True, timeout_seconds=1)
            reviewed = prune.plan_prune(request)
            with patch.object(prune, "_install_prune_deadline"), patch.object(
                prune, "_vacuum", side_effect=_sqlite_failure(sqlite3.SQLITE_INTERRUPT)
            ), patch.object(prune.time, "monotonic", side_effect=(0.0, 0.0, 0.0, 0.0, 2.0)):
                with self.assertRaises(prune.PruneOperationalError) as raised:
                    prune.apply_prune_plan(reviewed)
            text = describe_prune_failure(raised.exception, str(database))
            self.assertIn("database vacuum timed out", text)
            self.assertIn("configured phase timeout: 1s", text)
            self.assertIn("SQLITE_INTERRUPT", text)
            self.assertIn("increase --timeout-seconds", text)
            self.assertNotIn("do not repeat", text)

    def test_os_permissions_and_storage_errors_are_redacted_and_actionable(self):
        """Use errno and current capacity instead of raw filename/exception details."""
        for number, label in ((errno.EACCES, "EACCES"), (errno.EROFS, "EROFS"), (errno.ENOSPC, "ENOSPC"), (errno.EDQUOT, "EDQUOT")):
            with self.subTest(number=number):
                failure = prune.PruneOperationalError("database could not be inspected", phase="path")
                failure.__cause__ = OSError(number, "secret OS message", "/private/token-file")
                with patch("opencode_db.target.storage_space", return_value=StorageSpace(0, 0)), patch(
                    "opencode_db.prune_diagnostics.storage_space", return_value=StorageSpace(0, 0)
                ):
                    text = describe_prune_failure(failure, "/tmp/opencode-db-v1/test.db")
                self.assertIn(label, text)
                self.assertIn("bytes available=0", text)
                self.assertIn("inodes available=0", text)
                self.assertNotIn("secret", text)
                self.assertNotIn("token-file", text)

    def test_health_failure_uses_safe_inner_reason_after_vacuum_wrapper(self):
        """A failed post-vacuum check is more specific than the generic vacuum wrapper."""
        failure = prune.PruneOperationalError("database vacuum failed", phase="vacuum", step="vacuum_validation")
        failure.__cause__ = prune.PruneOperationalError("database foreign key check failed", step="foreign_key_check")
        text = describe_prune_failure(failure, "/tmp/opencode-db-v1/test.db")
        self.assertIn("step: SQLite foreign-key check", text)
        self.assertIn("database failed its foreign-key check", text)

    def test_additional_cleanup_error_does_not_mask_the_primary_vacuum_cause(self):
        """Keep both the original execution cause and cleanup's independently coded failure."""
        committed = prune.PruneCommittedError(
            "sessions were pruned but vacuum failed; do not repeat the prune request; rerun with --vacuum-only",
            prune.PruneOutcome(1, None, None, None),
        )
        committed.phase = "vacuum"
        committed.__cause__ = _sqlite_failure(sqlite3.SQLITE_IOERR_FSYNC)
        class CleanupFailure:
            in_transaction = False
            def set_progress_handler(self, *args):
                pass
            def close(self):
                raise _sqlite_failure(sqlite3.SQLITE_READONLY)
        with self.assertRaises(prune.PruneCommittedError) as raised:
            prune._cleanup_prune_connection(CleanupFailure(), "application", committed, committed.outcome)
        text = describe_prune_failure(raised.exception, "/tmp/opencode-db-v1/test.db")
        self.assertIn("SQLITE_IOERR_FSYNC", text)
        self.assertIn("additional cleanup failure: SQLITE_READONLY", text)
        self.assertIn("do not repeat", text)
        self.assertNotIn("secret", text)

    def test_generic_sqlite_failure_without_a_numeric_code_remains_honest(self):
        """Do not guess a cause by parsing potentially private SQLite message text."""
        failure = prune.PruneOperationalError("database vacuum failed", phase="vacuum")
        failure.__cause__ = sqlite3.OperationalError("private SQL and transcript")
        text = describe_prune_failure(failure, "/tmp/opencode-db-v1/test.db")
        self.assertIn("no structured SQLite/OS cause", text)
        self.assertNotIn("private SQL", text)

    def test_diagnostics_are_bounded_and_escape_operator_path_control_bytes(self):
        """Very long paths/cyclic causes cannot make diagnostics unbounded or inject lines."""
        failure = prune.PruneOperationalError("database vacuum failed", phase="vacuum")
        failure.__cause__ = failure
        failure.timeout_seconds = 10**10000
        text = describe_prune_failure(failure, "/tmp/\n" + "x" * 10000)
        self.assertLessEqual(len(text), 8192)
        self.assertNotIn('database: "/tmp/\n', text)
        self.assertIn("\\n", text)


if __name__ == "__main__":
    unittest.main()
