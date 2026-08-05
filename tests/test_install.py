"""Integration tests for durable installation of generated SQLite fixtures."""

from __future__ import annotations

from pathlib import Path
import json
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from opencode_db.artifacts import (
    CaptureError,
    capture_source_set,
    prune_backup,
    select_candidate,
)
from opencode_db.cleanup import clean_snapshot
from opencode_db.install import (
    InstallHooks,
    install_candidate,
    resume_install,
    rollback_install,
)
from opencode_db.target import StorageSpace, TargetEnvironment

from sqlite_fixtures import create_abrupt_wal_database


class InstallTests(unittest.TestCase):
    """Exercise replacement only through private, generated SQLite fixtures."""

    def test_install_retains_original_and_replaces_the_active_database(self) -> None:
        """Install one exact candidate and retain the byte-identical source snapshot."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('before')")
            original = database.read_bytes()
            capture = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            assert capture.snapshot_dir is not None
            outcome = clean_snapshot(
                capture.snapshot_dir,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            assert outcome.candidate_id is not None
            selected = select_candidate(str(database), outcome.candidate_id)

            installed = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            self.assertEqual(installed.state, "installed")
            self.assertTrue(installed.validation.clean_reopen.value == "pass")
            assert outcome.candidate_path is not None
            self.assertEqual(database.read_bytes(), outcome.candidate_path.read_bytes())
            self.assertEqual(
                (capture.snapshot_dir / database.name).read_bytes(), original
            )
            self.assertFalse(Path(f"{database}-wal").exists())
            self.assertFalse(Path(f"{database}-shm").exists())
            self.assertFalse(Path(f"{database}-journal").exists())

    def test_changed_main_or_sidecar_refuses_before_active_mutation(self) -> None:
        """Reject byte and presence changes from the accepted source snapshot."""
        for change in ("main", "add-wal", "change-wal", "remove-wal"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory(dir="/tmp") as directory,
            ):
                root = Path(directory)
                database, selected = self._candidate(
                    root, wal=change in {"change-wal", "remove-wal"}
                )
                original = database.read_bytes()
                wal = Path(f"{database}-wal")
                if change == "main":
                    database.write_bytes(original + b"changed")
                elif change == "add-wal":
                    wal.write_bytes(b"added")
                elif change == "change-wal":
                    wal.write_bytes(b"changed")
                else:
                    wal.unlink()

                result = install_candidate(
                    str(database),
                    selected,
                    scratch_dir=str(root / "scratch"),
                    environment=self._environment(root),
                )

                self.assertEqual(result.code, "source_changed")
                self.assertFalse(
                    (selected.candidate_path.parent / "quarantine").exists()
                )

    def test_resume_and_rollback_reconcile_durable_interruption_boundaries(
        self,
    ) -> None:
        """Continue only logged moves and restore original bytes after rollback interruption."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database, selected = self._candidate(root)
            original = database.read_bytes()
            interrupted = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                hooks=InstallHooks(
                    after_original_retained=lambda _: (_ for _ in ()).throw(
                        InterruptedError()
                    )
                ),
            )
            self.assertEqual(interrupted.state, "install_incomplete")
            self.assertFalse(database.exists())

            restored = rollback_install(
                str(database),
                interrupted.operation_id,
                environment=self._environment(root),
                hooks=InstallHooks(
                    during_rollback=lambda _: (_ for _ in ()).throw(InterruptedError())
                ),
            )
            self.assertEqual(restored.state, "install_incomplete")
            self.assertEqual(
                resume_install(
                    str(database),
                    restored.operation_id,
                    environment=self._environment(root),
                ).state,
                "rolled_back",
            )
            self.assertEqual(database.read_bytes(), original)

    def test_fresh_process_recovers_after_durable_original_retention(self) -> None:
        """Resume an abruptly terminated process using only its synced intent log."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database, selected = self._candidate(root)
            script = """
import os
import sys
from opencode_db.artifacts import select_candidate
from opencode_db.install import InstallHooks, install_candidate

database, candidate, scratch = sys.argv[1:]
selected = select_candidate(database, candidate)
install_candidate(database, selected, scratch_dir=scratch,
    hooks=InstallHooks(after_original_retained=lambda _: os._exit(75)))
"""
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(database),
                    selected.candidate_id,
                    str(root / "scratch"),
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                },
            )
            self.assertEqual(completed.returncode, 75)
            assert selected.candidate_path is not None
            catalog = json.loads(
                (
                    selected.candidate_path.parent.parent.parent / "catalog.json"
                ).read_text()
            )
            operation = next(
                item["operation_id"]
                for item in catalog["operations"]
                if item["operation_id"].startswith("install-")
            )

            resumed = resume_install(
                str(database), operation, environment=self._environment(root)
            )

            self.assertEqual(resumed.state, "installed")
            self.assertEqual(
                resume_install(
                    str(database), operation, environment=self._environment(root)
                ).state,
                "installed",
            )

    def test_uncertain_install_requires_explicit_report_digest(self) -> None:
        """Prevent a selected uncertain candidate from approving its own report."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            Path(f"{database}-shm").write_bytes(b"stale")
            capture = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            assert capture.snapshot_dir is not None
            preview = clean_snapshot(
                capture.snapshot_dir,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            assert preview.candidate_id is not None
            assert preview.report_sha256 is not None
            selected = select_candidate(
                str(database), preview.candidate_id, preview.report_sha256
            )

            refused = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            self.assertEqual(refused.code, "approval_required")

            installed = install_candidate(
                str(database),
                selected,
                approve_uncertain_report=preview.report_sha256,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            self.assertEqual(installed.state, "installed")

    def test_resume_rejects_intent_paths_outside_recorded_namespaces(self) -> None:
        """Preserve unrelated files when a persisted move path is malformed."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database, selected = self._candidate(root)
            interrupted = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                hooks=InstallHooks(
                    after_original_retained=lambda _: (_ for _ in ()).throw(
                        InterruptedError()
                    )
                ),
            )
            protected = root / "protected"
            protected.write_bytes(b"keep")
            state_path = (
                selected.candidate_path.parent
                / f"{interrupted.operation_id}.install.json"
            )
            state = json.loads(state_path.read_text())
            state["actions"][state["index"]]["source"] = str(protected)
            state["actions"][state["index"]]["source_before_sha256"] = hashlib.sha256(
                b"keep"
            ).hexdigest()
            state_path.write_text(json.dumps(state, sort_keys=True) + "\n")

            resumed = resume_install(
                str(database),
                interrupted.operation_id,
                environment=self._environment(root),
            )
            self.assertEqual(resumed.state, "manual_recovery_required")
            self.assertEqual(protected.read_bytes(), b"keep")

    def test_prune_refuses_snapshot_required_by_an_incomplete_install(self) -> None:
        """Keep rollback authority when an interrupted installation references it."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database, selected = self._candidate(root)
            interrupted = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                hooks=InstallHooks(
                    after_original_retained=lambda _: (_ for _ in ()).throw(
                        InterruptedError()
                    )
                ),
            )

            with self.assertRaisesRegex(CaptureError, "snapshot_required"):
                prune_backup(str(database), selected.snapshot_id)

            self.assertTrue(selected.candidate_path.parent.exists())
            self.assertEqual(interrupted.state, "install_incomplete")

    def test_prune_allows_a_terminal_installation_group(self) -> None:
        """Remove retired recovery evidence only after its install reaches terminal state."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database, selected = self._candidate(root)
            installed = install_candidate(
                str(database),
                selected,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            prune_backup(str(database), selected.snapshot_id)

            self.assertEqual(installed.state, "installed")
            self.assertFalse(selected.candidate_path.parent.exists())
            self.assertTrue(database.exists())

    def _candidate(self, root: Path, *, wal: bool = False):
        """Create one generated preview candidate without accessing user state."""
        database = root / "opencode.db"
        if wal:
            create_abrupt_wal_database(database, committed=True)
        else:
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('before')")
        capture = capture_source_set(
            str(database),
            scratch_dir=str(root / "scratch"),
            environment=self._environment(root),
        )
        assert capture.snapshot_dir is not None
        outcome = clean_snapshot(
            capture.snapshot_dir,
            scratch_dir=str(root / "scratch"),
            environment=self._environment(root),
        )
        assert outcome.candidate_id is not None
        return database, select_candidate(
            str(database), outcome.candidate_id, outcome.report_sha256
        )

    @staticmethod
    def _environment(root: Path) -> TargetEnvironment:
        """Return capacious local probe evidence for a private test directory."""
        return TargetEnvironment(
            mountinfo_reader=lambda: f"36 25 0:32 / {root} rw - ext4 /dev/test rw\n",
            storage_probe=lambda _: StorageSpace(100_000_000, 10_000),
        )


if __name__ == "__main__":
    unittest.main()
