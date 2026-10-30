"""Exercise work-root placement, recovery, and actionable capacity diagnostics."""

from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
import errno
import io
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli, install
from opencode_db.artifacts import capture_source_set, prune_backup, select_candidate
from opencode_db.cleanup import clean_snapshot
from opencode_db.prune import PruneCommittedError, PruneOutcome
from opencode_db.target import StorageSpace, TargetEnvironment, TargetError, require_capacity, storage_failure
from sqlite_fixtures import create_abrupt_wal_database


class StorageWorkTests(unittest.TestCase):
    """Use generated disposable databases; never inspect operator state."""

    def _run(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_bare_cleanup_explains_the_two_step_wal_repair(self):
        """The guessed command offers the actual workflow without database I/O."""
        with patch.object(cli, "capture_source_set", side_effect=AssertionError):
            code, output, error = self._run(["cleanup"])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("WAL repair", output)
        self.assertIn("cleanup preview", output)
        self.assertIn("cleanup install", output)
        self.assertIn("Only install replaces", output)

    def test_cli_work_root_moves_heavy_artifacts_and_preserves_followups(self):
        """Preview/install/prune use the exact external store and local staging."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            active, work = root / "active", root / "work space's"
            active.mkdir()
            work.mkdir(mode=0o700)
            database = active / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            original = {p: p.read_bytes() for p in active.iterdir()}
            args = ["--database", str(database), "--work-dir", str(work), "--json"]
            code, output, error = self._run(["cleanup", "preview", *args])
            self.assertEqual((code, error), (0, ""), output)
            preview = json.loads(output)
            self.assertFalse((active / ".opencode-db").exists())
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)
            snapshot = next((work / ".opencode-db").glob("*/snapshots/*"))
            self.assertTrue((snapshot / (preview["candidate_id"] + ".sqlite")).exists())
            self.assertEqual(list((work / "scratch").iterdir()), [])
            followup = shlex.split(preview["next_actions"][0])[1:]
            self.assertEqual(followup[followup.index("--work-dir") + 1], str(work))
            with patch.object(install, "_move", wraps=install._move) as moves:
                code, output, error = self._run([*followup, "--json"])
            for call in moves.call_args_list:
                for path in call.args:
                    self.assertTrue(path.is_relative_to(active))
            self.assertEqual((code, error), (0, ""), output)
            installed = json.loads(output)
            self.assertEqual(installed["status"], "installed")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM entries").fetchall(), [("committed-value",)])
            code, output, error = self._run([
                "cleanup", "prune-backup", *args, "--snapshot", preview["snapshot_id"],
            ])
            self.assertEqual((code, error), (0, ""), output)
            self.assertFalse(snapshot.exists())
            self.assertEqual(list((active / ".opencode-db").glob("*/snapshots/*")), [])

    def test_external_store_survives_interrupted_install_resume_and_rollback(self):
        """Recovery locates external evidence even while the active file is absent."""
        for recovery in ("resume", "rollback"):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
                root = Path(directory)
                active, work = root / "active", root / "work"
                active.mkdir()
                work.mkdir(mode=0o700)
                database = active / "opencode.db"
                create_abrupt_wal_database(database, committed=True)
                original = {p.name: p.read_bytes() for p in active.iterdir()}
                scratch = str(work / "scratch")
                capture = capture_source_set(str(database), work_dir=str(work), scratch_dir=scratch)
                outcome = clean_snapshot(capture.snapshot_dir, scratch_dir=scratch)
                selected = select_candidate(str(database), outcome.candidate_id, work_dir=str(work))
                def interrupt(_):
                    raise OSError("interrupted")
                result = install.install_candidate(
                    str(database), selected, work_dir=str(work), scratch_dir=scratch,
                    hooks=install.InstallHooks(after_original_retained=interrupt),
                )
                self.assertEqual(result.state, "install_incomplete")
                self.assertFalse(database.exists())
                code, output, error = self._run([
                    "cleanup", "status", "--database", str(database), "--work-dir", str(work),
                    "--operation", result.operation_id, "--json",
                ])
                self.assertEqual(error, "")
                self.assertTrue(json.loads(output)["next_actions"])
                action = json.loads(output)["next_actions"][0 if recovery == "resume" else 1]
                code, output, error = self._run([*shlex.split(action)[1:], "--json"])
                self.assertEqual((code, error), (0, ""), output)
                if recovery == "rollback":
                    for name, data in original.items():
                        self.assertEqual((active / name).read_bytes(), data)
                self.assertEqual(install.install_status(str(database), result.operation_id, work_dir=str(work)).state,
                                 "installed" if recovery == "resume" else "rolled_back")
                prune_backup(str(database), capture.snapshot_id, work_dir=str(work))
                self.assertFalse(capture.snapshot_dir.exists())

    def test_capacity_refusal_identifies_bytes_inodes_or_both(self):
        """Each preflight names the insufficient resource and required counts."""
        for available, resource in ((StorageSpace(99, 10), "disk space"),
                                    (StorageSpace(100, 2), "inodes"),
                                    (StorageSpace(99, 2), "disk space and inodes")):
            with self.subTest(resource=resource):
                environment = TargetEnvironment(lambda: "", lambda _: available)
                with self.assertRaises(TargetError) as raised:
                    require_capacity(Path("/tmp/opencode-db-v1"), 100, 3, environment=environment)
                message = raised.exception.diagnostic
                self.assertIn(f"Insufficient {resource} at", message)
                self.assertIn("/tmp/opencode-db-v1", message)
                self.assertIn("bytes required=100", message)
                self.assertIn("inodes required=3", message)

    def test_capture_capacity_diagnostic_reaches_human_and_json_output(self):
        """Do not discard capacity observations at the capture/result boundary."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            database = root / "test.db"
            database.write_bytes(b"fixture")
            environment = TargetEnvironment(lambda: "", lambda _: StorageSpace(0, 50))
            with patch("opencode_db.target.default_environment", return_value=environment):
                for machine in (False, True):
                    code, output, error = self._run(["cleanup", "preview", "--database", str(database), *(["--json"] if machine else [])])
                    message = json.loads(output)["diagnostics"][0]["message"] if machine else error
                    self.assertEqual(code, 4)
                    self.assertIn("Insufficient disk space at", message)
                    self.assertIn("bytes required=", message)
                    self.assertIn("inodes required=", message)
                    self.assertIn(str(root), message)
            self.assertFalse((root / ".opencode-db").exists())

    def test_runtime_storage_errors_are_evidence_based_and_redacted(self):
        """ENOSPC is ambiguous; current observations must not invent its cause."""
        for error in (OSError(errno.ENOSPC, "private-token"), OSError(errno.EDQUOT, "private-token")):
            with patch("opencode_db.target.storage_space", return_value=StorageSpace(12, 5)):
                message = storage_failure(error, Path("/tmp/opencode-db-v1"))
            self.assertIn("bytes available=12", message)
            self.assertIn("inodes available=5", message)
            self.assertNotIn("private-token", message)
        full = sqlite3.OperationalError("private SQL and transcript")
        full.sqlite_errorcode = sqlite3.SQLITE_FULL
        wrapped = RuntimeError("safe wrapper")
        wrapped.__cause__ = full
        with patch("opencode_db.target.storage_space", return_value=StorageSpace(12, 0)):
            message = storage_failure(wrapped, Path("/tmp/opencode-db-v1"))
        self.assertIn(": inodes;", message)
        self.assertNotIn("transcript", message)

    def test_scratch_and_install_capacity_messages_name_the_actual_location(self):
        """Scratch admission and final staging do not collapse to generic failures."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            database = root / "test.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE entries(value TEXT)")
            scratch = root / "scratch"
            environment = TargetEnvironment(
                lambda: f"36 25 0:32 / {root} rw - ext4 /dev/test rw\n",
                lambda path: StorageSpace(10**9, 100),
            )
            capture = capture_source_set(str(database), scratch_dir=str(scratch), environment=environment)
            poor_scratch = TargetEnvironment(environment.mountinfo_reader, lambda _: StorageSpace(10**9, 0))
            refused = clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch), environment=poor_scratch)
            self.assertEqual(refused.diagnostic_code, "scratch_capacity")
            self.assertIn("Insufficient inodes", refused.diagnostic_message)
            self.assertIn(str(scratch), refused.diagnostic_message)
            # Use a fresh snapshot because terminal preview state is immutable evidence.
            capture = capture_source_set(str(database), scratch_dir=str(scratch), environment=environment)
            outcome = clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch), environment=environment)
            selected = select_candidate(str(database), outcome.candidate_id)
            poor_target = TargetEnvironment(
                environment.mountinfo_reader,
                lambda path: StorageSpace(0, 100) if path == root else StorageSpace(10**9, 100),
            )
            refused = install.install_candidate(str(database), selected, scratch_dir=str(scratch), environment=poor_target)
            self.assertEqual(refused.code, "storage_capacity")
            self.assertIn("Insufficient disk space", refused.diagnostic_message)
            self.assertIn(str(root), refused.diagnostic_message)

    def test_remote_retained_root_works_with_explicit_local_scratch(self):
        """Only SQLite scratch needs local mount admission, not retained bytes."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            work, scratch = root / "remote", root / "local"
            work.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            database = root / "test.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE entries(value TEXT)")
            environment = TargetEnvironment(
                lambda: f"1 0 0:1 / / rw - ext4 /dev/test rw\n2 1 0:2 / {work} rw - nfs server:/work rw\n",
                lambda _: StorageSpace(10**9, 100),
            )
            with patch("opencode_db.target.default_environment", return_value=environment):
                code, output, error = self._run([
                    "cleanup", "preview", "--database", str(database), "--work-dir", str(work),
                    "--scratch-dir", str(scratch), "--json",
                ])
            self.assertEqual((code, error), (0, ""), output)
            followup = shlex.split(json.loads(output)["next_actions"][0])
            self.assertEqual(followup[followup.index("--scratch-dir") + 1], str(scratch))

    def test_postcommit_storage_diagnostic_keeps_the_prune_retry_warning(self):
        """A full filesystem after deletion still warns against repeating deletion."""
        full = sqlite3.OperationalError("private SQL")
        full.sqlite_errorcode = sqlite3.SQLITE_FULL
        failure = PruneCommittedError(
            "sessions were pruned but vacuum failed; do not repeat the prune request; rerun with --vacuum-only",
            PruneOutcome(1, None, None, None),
        )
        failure.__cause__ = full
        reviewed = SimpleNamespace(
            request=SimpleNamespace(vacuum_only=False, estimate_size=False),
            preview=SimpleNamespace(sessions_to_prune=1, sessions_to_keep=0,
                                    oldest_surviving_session_updated=None),
        )
        with patch.object(cli, "plan_prune", return_value=reviewed), patch.object(
            cli, "apply_prune_plan", side_effect=failure
        ), patch("opencode_db.target.storage_space", return_value=StorageSpace(0, 50)):
            code, output, error = self._run([
                "prune", "--db", "/tmp/opencode-db-v1/test.db", "--oldest", "1", "--vacuum", "--yes",
            ])
        self.assertEqual(code, 5)
        self.assertIn("pruned_sessions: 1", output)
        self.assertIn("do not repeat", error)
        self.assertIn("--vacuum-only", error)
        self.assertIn("bytes available=0", error)
        self.assertNotIn("private SQL", error)

    def test_unmeasurable_capacity_is_not_claimed_as_exhaustion(self):
        """A statvfs failure remains distinct from observed low space/inodes."""
        def unavailable(_):
            raise OSError("private details")
        environment = TargetEnvironment(lambda: "", unavailable)
        with self.assertRaises(TargetError) as raised:
            require_capacity(Path("/tmp/opencode-db-v1"), 100, 3, environment=environment)
        self.assertIn("Cannot measure storage capacity", raised.exception.diagnostic)
        self.assertNotIn("Insufficient", raised.exception.diagnostic)
        self.assertNotIn("private details", raised.exception.diagnostic)

    def test_noncleanup_work_root_sets_sqlite_temporary_location(self):
        """Common work-dir parsing sets and restores the CLI SQLite environment."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            observed = []
            def inspect(*args, **kwargs):
                observed.append(os.environ.get("SQLITE_TMPDIR"))
                return "fixture\n"
            before = os.environ.get("SQLITE_TMPDIR")
            with patch.object(cli, "inspect_database", side_effect=inspect):
                code, output, error = self._run(["list-projects", "--db", "/missing/test.db", "--work-dir", directory])
            self.assertEqual((code, output, error), (0, "fixture\n", ""))
            self.assertEqual(observed, [directory])
            self.assertEqual(os.environ.get("SQLITE_TMPDIR"), before)

    def test_default_scratch_is_resolved_before_setting_the_temporary_environment(self):
        """TMPDIR selection must not append opencode-db again during execution."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            database = Path(directory) / "test.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE entries(value TEXT)")
            with patch.dict(os.environ, {"TMPDIR": directory}):
                code, output, error = self._run(["cleanup", "preview", "--database", str(database), "--json"])
            self.assertEqual((code, error), (0, ""), output)
            scratch = Path(directory) / "opencode-db"
            self.assertTrue(scratch.is_dir())
            self.assertEqual(list(scratch.iterdir()), [])

    def test_work_root_rejects_missing_nonprivate_and_relative_selections(self):
        """Bad work roots cannot silently fall back to adjacent retained storage."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            database = root / "test.db"
            with closing(sqlite3.connect(database)):
                pass
            public = root / "public"
            public.mkdir(mode=0o755)
            for work, expected in (("relative", 2), (str(root / "missing"), 4), (str(public), 4)):
                code, output, _ = self._run(["cleanup", "preview", "--database", str(database), "--work-dir", work, "--json"])
                self.assertEqual(code, expected, output)
            self.assertFalse((root / ".opencode-db").exists())


if __name__ == "__main__":
    unittest.main()
