"""Exercise durable preview restart boundaries with generated SQLite fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import errno
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import artifacts, cleanup, cli
from opencode_db.artifacts import CaptureError, capture_source_set, preview_operation_lock
from sqlite_fixtures import create_abrupt_wal_database

_ROOT = Path("/tmp/opencode-db-v1")
_CHILD = """
import os, sys
from pathlib import Path
from opencode_db.cleanup import clean_snapshot
snapshot, scratch, boundary = sys.argv[1:]
if boundary in {"before registration", "after registration"}:
    from opencode_db import artifacts
    register = artifacts._register_candidate
    def publish(*args, **kwargs):
        if boundary == "after registration":
            register(*args, **kwargs)
        os._exit(71)
    artifacts._register_candidate = publish
def stop(stage):
    if stage == boundary:
        os._exit(71)
result = clean_snapshot(Path(snapshot), scratch_dir=scratch, progress=stop)
raise SystemExit(0 if result.installable else 1)
"""


class PreviewResumeTests(unittest.TestCase):
    """Prove restart reuses only hash-verified completed stages and exact authority."""

    def _fixture(self, root):
        database = root / "opencode.db"
        create_abrupt_wal_database(database, committed=True)
        scratch = root / "scratch"
        capture = capture_source_set(str(database), scratch_dir=str(scratch))
        self.assertTrue(capture.accepted)
        return database, scratch, capture

    def _run(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def _kill(self, capture, scratch, stage):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.run(
            [sys.executable, "-c", _CHILD, str(capture.snapshot_dir), str(scratch), stage],
            env=environment, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(child.returncode, 71, child.stderr)

    def test_fresh_process_resumes_every_durable_boundary_without_earlier_sqlite_work(self):
        """A hard exit releases the lock and preserves all completed copy/check stages."""
        boundaries = ("copied", "recovered", "candidate", "integrity", "foreign_keys", "validated", "published")
        for stage in boundaries:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                root = Path(directory)
                database, scratch, capture = self._fixture(root)
                self._kill(capture, scratch, "saved " + stage)
                code, output, error = self._run([
                    "cleanup", "status", "--database", str(database), "--operation", capture.operation_id, "--json",
                ])
                self.assertEqual((code, error), (0, ""), output)
                self.assertIn("saved stage " + stage, json.loads(output)["diagnostics"][0]["message"])
                self.assertIn("cleanup resume", json.loads(output)["next_actions"][0])
                statements = []
                connect = cleanup._connect_rw
                def traced(path):
                    connection = connect(path)
                    connection.set_trace_callback(statements.append)
                    return connection
                with patch.object(cleanup, "_copy_inputs", side_effect=AssertionError("copy repeated")), patch.object(
                    cleanup, "_checkpoint", wraps=cleanup._checkpoint
                ) as recover, patch.object(cleanup, "_backup", wraps=cleanup._backup) as backup, patch.object(
                    cleanup, "_connect_rw", side_effect=traced
                ):
                    code, output, error = self._run([
                        "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                    ])
                self.assertEqual((code, error), (0, ""), output)
                result = json.loads(output)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["operation_id"], capture.operation_id)
                self.assertEqual(len(list(capture.snapshot_dir.glob("candidate-*.sqlite"))), 1)
                if stage != "copied":
                    self.assertEqual(recover.call_count, 0)
                if stage not in {"copied", "recovered"}:
                    self.assertEqual(backup.call_count, 0)
                if stage in {"integrity", "foreign_keys", "validated", "published"}:
                    self.assertNotIn("PRAGMA integrity_check", statements)
                if stage in {"foreign_keys", "validated", "published"}:
                    self.assertNotIn("PRAGMA foreign_key_check", statements)
                self.assertFalse((scratch / capture.operation_id).exists())

    def test_deadline_retains_candidate_and_cli_provides_an_exact_resume_command(self):
        """An expired validation attempt keeps candidate creation out of the retry path."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database, scratch, capture = self._fixture(root)
            def expired(stage):
                if stage == "checking integrity":
                    raise cleanup.CleanupError("deadline_exceeded")
            paused = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch), progress=expired)
            self.assertTrue(paused.resumable)
            self.assertEqual(paused.diagnostic_code, "deadline_exceeded")
            self.assertTrue((scratch / capture.operation_id / "candidate.sqlite").exists())
            with patch.object(cli, "clean_snapshot", return_value=paused):
                code, output, error = self._run([
                    "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                ])
            self.assertEqual((code, error), (5, ""))
            result = json.loads(output)
            self.assertEqual(result["status"], "operational_failure")
            command = shlex.split(result["next_actions"][0])[1:]
            self.assertIn("14400", command)
            with patch.object(cleanup, "_backup", side_effect=AssertionError("backup repeated")):
                code, output, error = self._run([*command, "--json"])
            self.assertEqual((code, error), (0, ""), output)

    def test_cli_preview_and_resume_use_four_hours_unless_explicitly_overridden(self):
        """Large databases no longer inherit the old thirty-minute preview budget."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            with patch.object(cli, "clean_snapshot", wraps=cli.clean_snapshot) as run:
                code, output, _ = self._run([
                    "cleanup", "preview", "--database", str(database), "--work-dir", str(root), "--json",
                ])
            self.assertEqual(code, 0, output)
            self.assertGreater(run.call_args.kwargs["deadline_seconds"], 14000)
            operation = json.loads(output)["operation_id"]
            with patch.object(cli, "clean_snapshot", wraps=cli.clean_snapshot) as run:
                self._run(["cleanup", "resume", "--database", str(database), "--work-dir", str(root), "--operation", operation, "--json"])
            self.assertEqual(run.call_args.kwargs["deadline_seconds"], 14400)
            with patch.object(cli, "clean_snapshot", wraps=cli.clean_snapshot) as run:
                self._run(["cleanup", "resume", "--database", str(database), "--work-dir", str(root), "--operation", operation, "--deadline-seconds", "7200", "--json"])
            self.assertEqual(run.call_args.kwargs["deadline_seconds"], 7200)

    def test_incomplete_source_capture_is_not_promoted_into_a_resumable_snapshot(self):
        """Source acceptance still requires the full pre/copy/post identity proof."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            def stop(source, number):
                raise InterruptedError()
            captured = capture_source_set(str(database), scratch_dir=str(root / "scratch"), after_chunk=stop)
            self.assertFalse(captured.accepted)
            code, output, _ = self._run([
                "cleanup", "resume", "--database", str(database), "--operation", captured.operation_id, "--json",
            ])
            self.assertEqual(code, 4, output)
            self.assertEqual(json.loads(output)["next_actions"], [])

    def test_resuming_original_snapshot_does_not_authorize_install_over_changed_source(self):
        """Resume is independent of active bytes, but installation still binds capture identity."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "saved candidate")
            with database.open("ab") as writer:
                writer.write(b"changed since capture")
            changed = database.read_bytes()
            code, output, error = self._run([
                "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
            ])
            self.assertEqual((code, error), (0, ""), output)
            install_args = shlex.split(json.loads(output)["next_actions"][0])[1:]
            code, output, _ = self._run([*install_args, "--json"])
            self.assertEqual(code, 4, output)
            self.assertEqual(json.loads(output)["status"], "source_changed")
            self.assertEqual(database.read_bytes(), changed)

    def test_recovery_interruption_restarts_only_the_mutable_recovery_stage(self):
        """A recovering intent cannot incorrectly treat mutated input as a saved copy."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "recovering WAL")
            with patch.object(cleanup, "_copy_inputs", wraps=cleanup._copy_inputs) as copy:
                result = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
            self.assertTrue(result.installable)
            self.assertEqual(copy.call_count, 1)

    def test_partial_backup_is_replaced_without_recapturing_or_recovering_inputs(self):
        """An interrupted backup keeps its recovered input but not partial output authority."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            _, scratch, capture = self._fixture(Path(directory))
            def interrupted(source, candidate, started, deadline):
                candidate.write_bytes(b"incomplete backup")
                raise KeyboardInterrupt()
            with patch.object(cleanup, "_backup", side_effect=interrupted):
                paused = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
            self.assertTrue(paused.resumable)
            with patch.object(cleanup, "_copy_inputs", side_effect=AssertionError("copy repeated")), patch.object(
                cleanup, "_checkpoint", side_effect=AssertionError("WAL recovery repeated")
            ):
                result = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
            self.assertTrue(result.installable)

    def test_publication_crash_retries_unregistered_bytes_or_reuses_registered_receipt(self):
        """Publication is idempotent on either side of durable catalog registration."""
        for boundary in ("before registration", "after registration"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, scratch, capture = self._fixture(Path(directory))
                self._kill(capture, scratch, boundary)
                candidates = list(capture.snapshot_dir.glob("candidate-*.sqlite"))
                self.assertEqual(len(candidates), 1)
                with patch.object(cleanup, "_backup", side_effect=AssertionError("backup repeated")):
                    code, output, error = self._run([
                        "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                    ])
                self.assertEqual((code, error), (0, ""), output)
                self.assertEqual(json.loads(output)["candidate_id"], candidates[0].stem)
                artifacts.prune_backup(str(database), capture.snapshot_id)
                self.assertFalse(capture.snapshot_dir.exists())

    def test_abort_removes_only_checkpoint_bound_unpublished_outputs(self):
        """Aborting a publication crash leaves no unknown partial candidate to block pruning."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "before registration")
            artifacts.abort_preview_operation(str(database), capture.operation_id)
            self.assertEqual(list(capture.snapshot_dir.glob("candidate-*")), [])
            artifacts.prune_backup(str(database), capture.snapshot_id)
            self.assertFalse(capture.snapshot_dir.exists())

    def test_deadline_during_snapshot_revalidation_is_not_misreported_as_corruption(self):
        """A hash budget expiry preserves both the immutable snapshot and saved candidate."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            _, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "saved candidate")
            with patch.object(artifacts, "_hash_file", side_effect=CaptureError("deadline_exceeded")):
                result = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
            self.assertTrue(result.resumable)
            self.assertEqual(result.diagnostic_code, "deadline_exceeded")
            self.assertTrue((scratch / capture.operation_id / "candidate.sqlite").exists())
            self.assertTrue(cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch)).installable)

    def test_changed_candidate_or_future_state_is_preserved_and_not_reused(self):
        """Do not reuse successful checks on changed bytes or unknown checkpoint schemas."""
        for mutation in ("candidate", "future", "path", "cross-target"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                root = Path(directory)
                database, scratch, capture = self._fixture(root)
                self._kill(capture, scratch, "saved integrity")
                work = scratch / capture.operation_id
                state = work / ".opencode-db-preview.json"
                if mutation == "candidate":
                    with (work / "candidate.sqlite").open("ab") as writer:
                        writer.write(b"changed")
                else:
                    value = json.loads(state.read_text())
                    if mutation == "future":
                        value["schema_version"] = 999
                    elif mutation == "path":
                        value["files"]["../unrelated"] = value["files"].pop("candidate.sqlite")
                    else:
                        value["target_id"] = "f" * 32
                    artifacts._write_json(state, value)
                before = {path.name: path.read_bytes() for path in work.iterdir()}
                code, output, _ = self._run([
                    "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                ])
                self.assertEqual(code, 4, output)
                self.assertEqual({path.name: path.read_bytes() for path in work.iterdir()}, before)
                self.assertFalse(list(capture.snapshot_dir.glob("candidate-*.sqlite")))

    def test_changed_published_receipt_refuses_without_rewriting_the_catalog(self):
        """Malformed/future published evidence stays a read-only compatibility refusal."""
        for mutation in ("candidate", "future-report"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, scratch, capture = self._fixture(Path(directory))
                result = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
                self.assertTrue(result.installable)
                catalog = capture.snapshot_dir.parent.parent / "catalog.json"
                before = catalog.read_bytes()
                if mutation == "candidate":
                    with result.candidate_path.open("ab") as writer:
                        writer.write(b"changed")
                else:
                    value = json.loads(result.report_path.read_text())
                    value["schema_version"] = 999
                    artifacts._write_json(result.report_path, value)
                code, output, _ = self._run([
                    "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                ])
                self.assertEqual(code, 4, output)
                self.assertEqual(catalog.read_bytes(), before)

    def test_lock_excludes_concurrent_resume_and_abort_without_deleting_work(self):
        """A cooperative process cannot resume or abort a live operation."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "saved candidate")
            with preview_operation_lock(capture.snapshot_dir):
                result = cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch))
                self.assertEqual(result.diagnostic_code, "preview_busy")
                with self.assertRaisesRegex(CaptureError, "preview_busy"):
                    artifacts.abort_preview_operation(str(database), capture.operation_id)
            self.assertTrue((scratch / capture.operation_id / "candidate.sqlite").exists())
            self.assertTrue(cleanup.clean_snapshot(capture.snapshot_dir, scratch_dir=str(scratch)).installable)

    def test_abort_discards_paused_work_and_allows_exact_backup_pruning(self):
        """Explicit discard remains available instead of automatic expiry cleanup."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            self._kill(capture, scratch, "saved candidate")
            artifacts.abort_preview_operation(str(database), capture.operation_id)
            self.assertFalse((scratch / capture.operation_id).exists())
            with self.assertRaises(CaptureError):
                artifacts.load_preview_operation(str(database), capture.operation_id)
            artifacts.prune_backup(str(database), capture.snapshot_id)
            self.assertFalse(capture.snapshot_dir.exists())

    def test_old_failed_preview_can_reuse_its_accepted_snapshot_without_recapture(self):
        """Legacy preview failures still retain a usable immutable capture stage."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, scratch, capture = self._fixture(Path(directory))
            snapshot = artifacts.load_accepted_snapshot(capture.snapshot_dir)
            artifacts.finish_preview_operation(snapshot, "invalid")
            with patch.object(cli, "capture_source_set", side_effect=AssertionError("recaptured")):
                code, output, error = self._run([
                    "cleanup", "resume", "--database", str(database), "--operation", capture.operation_id, "--json",
                ])
            self.assertEqual((code, error), (0, ""), output)

    def test_pause_with_full_storage_retains_work_and_capacity_diagnostic(self):
        """Recoverable ENOSPC does not remove already recovered scratch input."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            _, scratch, capture = self._fixture(Path(directory))
            result = cleanup.clean_snapshot(
                capture.snapshot_dir, scratch_dir=str(scratch),
                hooks=cleanup.CleanupHooks(before_backup=lambda _: (_ for _ in ()).throw(OSError(errno.ENOSPC, "secret"))),
            )
            self.assertTrue(result.resumable)
            self.assertIn("Storage exhausted", result.diagnostic_message)
            self.assertNotIn("secret", result.diagnostic_message)
            self.assertTrue((scratch / capture.operation_id / "source.sqlite").exists())

    def test_cross_target_and_wrong_host_selection_refuse(self):
        """Resume never routes an operation to another target or host's scratch."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database, scratch, capture = self._fixture(root)
            self._kill(capture, scratch, "saved candidate")
            with self.assertRaises(CaptureError):
                artifacts.load_preview_operation(str(root / "other.db"), capture.operation_id)
            with patch.object(artifacts.socket, "gethostname", return_value="other-host"):
                with self.assertRaisesRegex(CaptureError, "scratch_wrong_host"):
                    artifacts.load_preview_operation(str(database), capture.operation_id)

    def test_completed_resume_returns_same_candidate_and_progress_is_stderr_only(self):
        """Repeating resume is idempotent, and progress never pollutes JSON stdout."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            args = ["--database", str(database), "--work-dir", str(root), "--json", "--progress"]
            code, output, error = self._run(["cleanup", "preview", *args])
            self.assertEqual(code, 0, output)
            first = json.loads(output)
            self.assertIn(first["operation_id"], error)
            self.assertLess(error.index(first["operation_id"]), error.index("creating candidate"))
            self.assertIn("checking integrity", error)
            code, output, error = self._run(["cleanup", "resume", *args, "--operation", first["operation_id"]])
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["candidate_id"], first["candidate_id"])


if __name__ == "__main__":
    unittest.main()
