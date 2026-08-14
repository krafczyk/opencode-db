"""Tests for explicit target resolution and local scratch admission."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db.target import (
    StorageSpace,
    TargetEnvironment,
    TargetError,
    admit_scratch,
    select_default_database,
    resolve_target,
)


class TargetTests(unittest.TestCase):
    """Verify that target and scratch validation fail before database access."""

    def test_default_database_selection_uses_xdg_then_home_without_inspecting_files(
        self,
    ) -> None:
        """Choose only an absolute environment base without probing its database."""
        self.assertEqual(
            select_default_database(
                {"XDG_DATA_HOME": "/missing/xdg", "HOME": "/home/operator"}
            ),
            "/missing/xdg/opencode/opencode.db",
        )
        self.assertEqual(
            select_default_database(
                {"XDG_DATA_HOME": "relative", "HOME": "/home/operator"}
            ),
            "/home/operator/.local/share/opencode/opencode.db",
        )
        with self.assertRaises(TargetError) as error:
            select_default_database({"XDG_DATA_HOME": "relative", "HOME": "relative"})
        self.assertEqual(error.exception.code, "default_database_unavailable")
        with self.assertRaises(TargetError) as error:
            select_default_database(
                {"XDG_DATA_HOME": "/invalid\x00base", "HOME": "/home/operator"}
            )
        self.assertEqual(error.exception.code, "default_database_unavailable")

    def test_resolve_target_requires_an_existing_regular_absolute_file(self) -> None:
        """Reject invalid target forms without creating a replacement database."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "opencode.db"
            database.write_bytes(b"database")
            dangling = root / "dangling.db"
            dangling.symlink_to(root / "missing.db")
            cases = [
                "relative.db",
                ":memory:",
                str(root / "missing.db"),
                str(root),
                str(dangling),
            ]

            for value in cases:
                with self.subTest(value=value):
                    with self.assertRaises(TargetError) as error:
                        resolve_target(value)
                    self.assertEqual(error.exception.code, "target_invalid")

            resolved = resolve_target(str(database))
            self.assertEqual(resolved.path, database.resolve())
            self.assertFalse((root / ".opencode-db").exists())

    def test_admit_scratch_refuses_remote_unknown_private_and_capacity_failures(
        self,
    ) -> None:
        """Require a known local mount, private workspace, and sufficient space."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            mountinfo = f"36 25 0:32 / {root} rw - ext4 /dev/test rw\n"
            environment = TargetEnvironment(
                mountinfo_reader=lambda: mountinfo,
                storage_probe=lambda _: StorageSpace(10_000, 100),
            )

            admitted = admit_scratch(
                scratch, required_bytes=100, required_inodes=3, environment=environment
            )
            self.assertEqual(admitted, scratch.resolve())

            remote = TargetEnvironment(
                mountinfo_reader=lambda: mountinfo.replace("ext4", "nfs"),
                storage_probe=lambda _: StorageSpace(10_000, 100),
            )
            with self.assertRaises(TargetError) as error:
                admit_scratch(scratch, 100, 3, remote)
            self.assertEqual(error.exception.code, "scratch_not_local")

            unknown = TargetEnvironment(
                mountinfo_reader=lambda: "",
                storage_probe=lambda _: StorageSpace(10_000, 100),
            )
            with self.assertRaises(TargetError) as error:
                admit_scratch(scratch, 100, 3, unknown)
            self.assertEqual(error.exception.code, "scratch_not_local")

            insufficient = TargetEnvironment(
                mountinfo_reader=lambda: mountinfo,
                storage_probe=lambda _: StorageSpace(99, 2),
            )
            with self.assertRaises(TargetError) as error:
                admit_scratch(scratch, 100, 3, insufficient)
            self.assertEqual(error.exception.code, "scratch_capacity")

            os.chmod(scratch, 0o755)
            with self.assertRaises(TargetError) as error:
                admit_scratch(scratch, 100, 3, environment)
            self.assertEqual(error.exception.code, "scratch_not_private")


if __name__ == "__main__":
    unittest.main()
