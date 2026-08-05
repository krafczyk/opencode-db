"""Tests for normal SQLite recovery of accepted copied source snapshots."""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db.artifacts import capture_source_set
from opencode_db.cleanup import CleanupHooks, clean_snapshot
from opencode_db.target import StorageSpace, TargetEnvironment

from sqlite_fixtures import create_abrupt_wal_database


class CleanupTests(unittest.TestCase):
    """Verify cleanup uses SQLite recovery on a copy and emits no sidecars."""

    def test_committed_wal_frames_become_a_complete_sidecar_free_candidate(
        self,
    ) -> None:
        """Replay committed abrupt-writer state without changing retained source bytes."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            capture = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            self.assertTrue(capture.accepted)
            assert capture.snapshot_dir is not None

            outcome = clean_snapshot(
                capture.snapshot_dir,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            self.assertEqual(outcome.completeness, "complete")
            self.assertTrue(outcome.installable)
            self.assertIsNotNone(outcome.candidate_path)
            assert outcome.candidate_path is not None
            self.assertFalse(Path(f"{outcome.candidate_path}-wal").exists())
            self.assertFalse(Path(f"{outcome.candidate_path}-shm").exists())
            self.assertFalse(Path(f"{outcome.candidate_path}-journal").exists())
            with sqlite3.connect(outcome.candidate_path) as candidate:
                self.assertEqual(
                    candidate.execute("SELECT value FROM entries").fetchall(),
                    [("committed-value",)],
                )
            assert outcome.report_path is not None
            report = json.loads(outcome.report_path.read_text())
            self.assertEqual(report["completeness_scope"], "captured_source_set")
            self.assertEqual(report["historical_completeness"], "not_proven")
            self.assertEqual(len(report["passive_checkpoint"]), 3)
            self.assertEqual(report["passive_checkpoint"][0], 0)
            self.assertEqual(
                report["passive_checkpoint"][1], report["passive_checkpoint"][2]
            )
            self.assertEqual(report["truncate_checkpoint"], [0, 0, 0])
            self.assertRegex(
                outcome.candidate_id or "",
                r"^candidate-\d{8}T\d{6}Z-[0-9a-f]{24}$",
            )

    def test_uncommitted_writer_rows_are_excluded_by_normal_recovery(self) -> None:
        """Let SQLite discard an unfinished transaction without frame inspection."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=False)
            outcome = self._capture_and_clean(database, root)

            self.assertTrue(outcome.installable)
            assert outcome.candidate_path is not None
            with sqlite3.connect(outcome.candidate_path) as candidate:
                self.assertEqual(
                    candidate.execute("SELECT value FROM entries").fetchall(), []
                )

    def test_clean_main_and_incomplete_wal_evidence_have_distinct_classes(self) -> None:
        """Classify only contradictory captured sidecar evidence as uncertain."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            clean = root / "clean.db"
            with sqlite3.connect(clean) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('plain')")
            complete = self._capture_and_clean(clean, root)
            self.assertEqual(complete.completeness, "complete")

            missing_wal = root / "missing-wal.db"
            with sqlite3.connect(missing_wal) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            Path(f"{missing_wal}-shm").write_bytes(b"stale shared memory")
            uncertain = self._capture_and_clean(missing_wal, root)
            self.assertEqual(uncertain.completeness, "uncertain")

    def test_trailing_wal_is_uncertain_but_stale_shm_is_not_opened(self) -> None:
        """Keep accepted trailing bytes and SHM evidence out of SQLite scratch paths."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            create_abrupt_wal_database(database, committed=True)
            Path(f"{database}-wal").write_bytes(
                Path(f"{database}-wal").read_bytes() + b"tail"
            )
            Path(f"{database}-shm").write_bytes(b"inconsistent SHM")
            outcome = self._capture_and_clean(database, root)

            self.assertEqual(outcome.completeness, "uncertain")
            self.assertTrue(outcome.installable)
            assert outcome.report_path is not None
            self.assertEqual(
                json.loads(outcome.report_path.read_text())["wal_evidence"][
                    "trailing_bytes"
                ],
                4,
            )

    def test_candidate_preserves_unusual_values_without_reporting_them(self) -> None:
        """Keep backup content exact while validation reports contain no row values."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "values.db"
            long_secret = "token=not-for-report " + ("x" * 4096) + " caf\u00e9"
            binary = b"\x00\xff\x01"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE entries (text_value TEXT, binary_value BLOB, nan_like REAL)"
                )
                connection.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    (long_secret, binary, float("nan")),
                )
            outcome = self._capture_and_clean(database, root)

            self.assertTrue(outcome.installable)
            assert outcome.candidate_path is not None
            with sqlite3.connect(outcome.candidate_path) as candidate:
                self.assertEqual(
                    candidate.execute(
                        "SELECT text_value, binary_value FROM entries"
                    ).fetchone(),
                    (long_secret, binary),
                )
            assert outcome.report_path is not None
            self.assertNotIn("token=not-for-report", outcome.report_path.read_text())

    def test_malformed_states_foreign_keys_and_checkpoint_failure_are_not_installable(
        self,
    ) -> None:
        """Reject unsupported normal-recovery inputs before candidate persistence."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            malformed_main = root / "malformed.db"
            malformed_main.write_bytes(b"not sqlite")
            self.assertFalse(self._capture_and_clean(malformed_main, root).installable)

            malformed_wal = root / "malformed-wal.db"
            with sqlite3.connect(malformed_wal) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            Path(f"{malformed_wal}-wal").write_bytes(b"bad")
            self.assertFalse(self._capture_and_clean(malformed_wal, root).installable)

            foreign_keys = root / "foreign-keys.db"
            with sqlite3.connect(foreign_keys) as connection:
                connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
                )
                connection.execute("INSERT INTO child VALUES (99)")
            fk_outcome = self._capture_and_clean(foreign_keys, root)
            self.assertFalse(fk_outcome.installable)
            self.assertEqual(fk_outcome.validation.foreign_keys.value, "fail")

            checkpoint = root / "checkpoint.db"
            with sqlite3.connect(checkpoint) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            capture = self._capture(checkpoint, root)
            checkpoint_outcome = clean_snapshot(
                capture.snapshot_dir,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                hooks=CleanupHooks(checkpoint=lambda _, __: (1, 1, 0)),
            )
            self.assertFalse(checkpoint_outcome.installable)
            self.assertEqual(
                checkpoint_outcome.diagnostic_code, "checkpoint_incomplete"
            )
            self.assertEqual(checkpoint_outcome.validation.checkpoint.value, "fail")

    def test_backup_interruption_and_journal_evidence_offer_no_candidate(self) -> None:
        """Preserve capture evidence when normal recovery is interrupted or unsupported."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "interrupted.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            capture = self._capture(database, root)
            outcome = clean_snapshot(
                capture.snapshot_dir,
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                hooks=CleanupHooks(
                    before_backup=lambda _: (_ for _ in ()).throw(InterruptedError())
                ),
            )
            self.assertFalse(outcome.installable)
            self.assertIsNone(outcome.candidate_path)

            journal = root / "journal.db"
            journal.write_bytes(b"main")
            Path(f"{journal}-journal").write_bytes(b"journal")
            refused = capture_source_set(
                str(journal),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            self.assertFalse(refused.accepted)
            self.assertEqual(refused.status, "unsupported_journal")

    def _capture_and_clean(self, database: Path, root: Path):
        """Capture and clean one generated database with deterministic scratch probes."""
        capture = self._capture(database, root)
        return clean_snapshot(
            capture.snapshot_dir,
            scratch_dir=str(root / "scratch"),
            environment=self._environment(root),
        )

    def _capture(self, database: Path, root: Path):
        """Capture one generated fixture and assert it is accepted recovery evidence."""
        capture = capture_source_set(
            str(database),
            scratch_dir=str(root / "scratch"),
            environment=self._environment(root),
        )
        self.assertTrue(capture.accepted)
        self.assertIsNotNone(capture.snapshot_dir)
        return capture

    @staticmethod
    def _environment(root: Path) -> TargetEnvironment:
        """Return deterministic admitted capacity evidence for a temp directory."""
        return TargetEnvironment(
            mountinfo_reader=lambda: f"36 25 0:32 / {root} rw - ext4 /dev/test rw\n",
            storage_probe=lambda _: StorageSpace(100_000_000, 10_000),
        )


if __name__ == "__main__":
    unittest.main()
