"""Tests for immutable synthetic SQLite source-set capture."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db.artifacts import (
    CaptureError,
    capture_source_set,
    load_accepted_snapshot,
)
from opencode_db.model import SourceManifest
from opencode_db.target import StorageSpace, TargetEnvironment


class ArtifactTests(unittest.TestCase):
    """Verify source snapshots retain evidence and reject unsafe acceptance."""

    def test_capture_retains_exact_private_bytes_for_wal_shm_combinations(self) -> None:
        """Copy each present standard sidecar without changing the active set."""
        for wal_present, shm_present in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with self.subTest(wal_present=wal_present, shm_present=shm_present):
                with tempfile.TemporaryDirectory(dir="/tmp") as directory:
                    root = Path(directory)
                    database = root / "opencode.db"
                    database.write_bytes(b"main-bytes")
                    files = {database: b"main-bytes"}
                    if wal_present:
                        files[Path(f"{database}-wal")] = b"wal-bytes"
                    if shm_present:
                        files[Path(f"{database}-shm")] = b"shm-bytes"
                    for path, content in files.items():
                        path.write_bytes(content)

                    outcome = capture_source_set(
                        str(database),
                        scratch_dir=str(root / "scratch"),
                        environment=self._environment(root),
                    )

                    self.assertTrue(outcome.accepted)
                    self.assertIsNotNone(outcome.snapshot_dir)
                    snapshot_dir = outcome.snapshot_dir
                    assert snapshot_dir is not None
                    for source, content in files.items():
                        self.assertEqual(
                            (snapshot_dir / source.name).read_bytes(), content
                        )
                        self.assertEqual(source.read_bytes(), content)
                    manifest = json.loads((snapshot_dir / "manifest.json").read_text())
                    self.assertEqual(manifest["state"], "accepted")
                    self.assertEqual(manifest["target"], str(database.resolve()))
                    self.assertEqual(
                        {
                            item["name"]: item["present"]
                            for item in manifest["post_copy_manifest"]["files"]
                        },
                        {
                            "main": True,
                            "wal": wal_present,
                            "shm": shm_present,
                            "journal": False,
                        },
                    )
                    self.assertEqual((snapshot_dir.stat().st_mode & 0o777), 0o700)
                    self.assertEqual(
                        ((snapshot_dir / "manifest.json").stat().st_mode & 0o777),
                        0o600,
                    )

    def test_capture_marks_source_mutation_diagnostic_only_before_acceptance(
        self,
    ) -> None:
        """Observe a deterministic source mutation after copying, before acceptance."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"before")

            def mutate_source(_: Path) -> None:
                database.write_bytes(b"after!")

            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                after_copy=mutate_source,
            )

            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "source_changed")
            self.assertIsNotNone(outcome.snapshot_dir)
            snapshot_dir = outcome.snapshot_dir
            assert snapshot_dir is not None
            manifest = json.loads((snapshot_dir / "manifest.json").read_text())
            self.assertEqual(manifest["state"], "diagnostic_only")
            self.assertEqual(manifest["reason"], "source_changed")
            self.assertFalse((snapshot_dir / "candidate").exists())

    def test_capture_detects_added_removed_and_changed_sidecars(self) -> None:
        """Reject every standard-sidecar presence or byte transition after preflight."""

        def add(database: Path) -> None:
            Path(f"{database}-wal").write_bytes(b"added")

        def remove(database: Path) -> None:
            Path(f"{database}-wal").unlink()

        def change(database: Path) -> None:
            Path(f"{database}-wal").write_bytes(b"changed-size")

        cases = {"add": add, "remove": remove, "change": change}
        for name, mutate in cases.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(dir="/tmp") as directory,
            ):
                root = Path(directory)
                database = root / "opencode.db"
                database.write_bytes(b"main")
                if name != "add":
                    Path(f"{database}-wal").write_bytes(b"wal")

                outcome = capture_source_set(
                    str(database),
                    scratch_dir=str(root / "scratch"),
                    environment=self._environment(root),
                    after_copy=lambda _: mutate(database),
                )

                self.assertFalse(outcome.accepted)
                self.assertEqual(outcome.status, "source_changed")

    def test_capture_preserves_but_refuses_rollback_journal_without_following_super_journal(
        self,
    ) -> None:
        """Retain only standard evidence and classify rollback journal state unsupported."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            journal = Path(f"{database}-journal")
            super_journal = root / "outside-super-journal"
            database.write_bytes(b"main")
            journal.write_bytes(b"super-journal-name: outside-super-journal")
            super_journal.write_bytes(b"must-not-read-or-copy")

            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "unsupported_journal")
            snapshot_dir = outcome.snapshot_dir
            assert snapshot_dir is not None
            self.assertEqual(
                (snapshot_dir / journal.name).read_bytes(), journal.read_bytes()
            )
            self.assertFalse((snapshot_dir / super_journal.name).exists())

    def test_capture_refuses_capacity_and_retains_interrupted_copy_as_diagnostic_only(
        self,
    ) -> None:
        """Fail closed before copying for capacity and preserve partial copy evidence on interruption."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"x" * 8192)
            limited = TargetEnvironment(
                mountinfo_reader=lambda: self._mountinfo(root),
                storage_probe=lambda _: StorageSpace(0, 0),
            )
            outcome = capture_source_set(
                str(database), scratch_dir=str(root / "scratch"), environment=limited
            )
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "storage_capacity")
            self.assertIsNone(outcome.snapshot_dir)

            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
                after_chunk=lambda _, __: (_ for _ in ()).throw(InterruptedError()),
            )
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "copy_failed")
            snapshot_dir = outcome.snapshot_dir
            assert snapshot_dir is not None
            self.assertTrue((snapshot_dir / "manifest.json").exists())

            database.write_bytes(b"x" * (2 * 1024 * 1024))
            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                deadline_seconds=1,
                environment=self._environment(root),
                after_chunk=lambda _, __: time.sleep(1),
            )
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "deadline_exceeded")

    def test_capture_refuses_nonprivate_retained_control_directory(self) -> None:
        """Never repair permissive existing retained storage during source capture."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"main")
            control = root / ".opencode-db"
            control.mkdir(mode=0o755)
            os.chmod(control, 0o755)

            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "artifact_not_private")

    def test_capture_refuses_unknown_catalog_without_allocating_snapshot(self) -> None:
        """Treat a future retained catalog as a read-only compatibility failure."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"main")
            control = root / ".opencode-db"
            control.mkdir(mode=0o700)
            os.chmod(control, 0o700)
            target_id = hashlib.sha256(str(database.resolve()).encode()).hexdigest()[
                :32
            ]
            target_root = control / target_id
            target_root.mkdir(mode=0o700)
            os.chmod(target_root, 0o700)
            (target_root / "catalog.json").write_text('{"schema_version": 2}\n')
            os.chmod(target_root / "catalog.json", 0o600)

            outcome = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )

            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.status, "artifact_schema_unsupported")
            self.assertFalse((target_root / "snapshots").exists())

    def test_capture_restores_the_callers_umask(self) -> None:
        """Limit the private write umask to the capture call itself."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"main")
            previous = os.umask(0o022)
            try:
                capture_source_set(
                    str(database),
                    scratch_dir=str(root / "scratch"),
                    environment=self._environment(root),
                )
                observed = os.umask(0o022)
                self.assertEqual(observed, 0o022)
            finally:
                os.umask(previous)

    def test_manifest_rejects_non_hex_digests_and_boolean_schema_versions(self) -> None:
        """Reject malformed content authority in persisted version-1 manifests."""
        manifest = {
            "schema_version": 1,
            "tool_version": "0.1.0",
            "target": "/tmp/opencode.db",
            "target_id": "target-1",
            "files": [
                {"name": "main", "present": True, "size": 4, "sha256": "a" * 64},
                {"name": "wal", "present": False, "size": None, "sha256": None},
                {"name": "shm", "present": False, "size": None, "sha256": None},
                {"name": "journal", "present": False, "size": None, "sha256": None},
            ],
        }

        malformed_digest = json.loads(json.dumps(manifest))
        malformed_digest["files"][0]["sha256"] = "z" * 64
        with self.assertRaises(ValueError):
            SourceManifest.from_dict(malformed_digest)

        boolean_schema = json.loads(json.dumps(manifest))
        boolean_schema["schema_version"] = True
        with self.assertRaises(ValueError):
            SourceManifest.from_dict(boolean_schema)

    def test_accepted_snapshot_is_bound_to_its_target_scoped_directory(self) -> None:
        """Reject copied manifests outside their exact retained snapshot location."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"main")
            capture = capture_source_set(
                str(database),
                scratch_dir=str(root / "scratch"),
                environment=self._environment(root),
            )
            snapshot_dir = capture.snapshot_dir
            assert snapshot_dir is not None
            renamed = snapshot_dir.parent / "snapshot-wrong-location"
            shutil.copytree(snapshot_dir, renamed)

            with self.assertRaises(CaptureError):
                load_accepted_snapshot(renamed)

    @staticmethod
    def _mountinfo(root: Path) -> str:
        """Return one deterministic admitted mount entry for a temporary root."""
        return f"36 25 0:32 / {root} rw - ext4 /dev/test rw\n"

    @classmethod
    def _environment(cls, root: Path) -> TargetEnvironment:
        """Build a local, capacious synthetic filesystem probe for capture tests."""
        return TargetEnvironment(
            mountinfo_reader=lambda: cls._mountinfo(root),
            storage_probe=lambda _: StorageSpace(10_000_000, 1000),
        )


if __name__ == "__main__":
    unittest.main()
