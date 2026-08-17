"""Atomic repair contracts for recontaminated sibling project moves."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db.move import MoveError, MoveRequest, apply_sibling_move, plan_sibling_move
from opencode_db.move_repair import MoveRepairRequest, apply_move_repair, plan_move_repair
from move_test_support import move_fixture, structured_rows, temporary_directory


class MoveRepairTests(unittest.TestCase):
    """Prove repair removes duplicate source registrations without losing current state."""

    def test_repair_drops_duplicate_registrations_and_rebases_remaining_rows(self) -> None:
        """Restore a target-only location family after OpenCode re-registers the source."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            source, target, database = _recontaminated_fixture(root)
            (target / "main" / "advanced").write_text("advanced", encoding="ascii")
            subprocess.run(["git", "-C", str(target / "main"), "add", "advanced"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(target / "main"), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "advanced",
                ],
                check=True,
            )
            before = structured_rows(database)

            reviewed = plan_move_repair(
                MoveRepairRequest(database, "project", str(source / "main"), str(target / "main"))
            )

            self.assertEqual(before, structured_rows(database))
            self.assertEqual(
                [(mapping.source, mapping.target) for mapping in reviewed.mappings],
                [
                    (str(source / "directory"), str(target / "directory")),
                    (str(source / "main"), str(target / "main")),
                    (str(source / "session"), str(target / "session")),
                    (str(source / "workspace"), str(target / "workspace")),
                ],
            )
            self.assertEqual(
                [
                    (membership.category, membership.action)
                    for mapping in reviewed.mappings
                    for membership in mapping.memberships
                ],
                [
                    ("project_directory.directory", "drop"),
                    ("project.sandbox", "drop"),
                    ("project_directory.directory", "drop"),
                    ("session.directory", "rebase"),
                    ("workspace.directory", "rebase"),
                ],
            )

            apply_move_repair(reviewed)

            with closing(sqlite3.connect(database)) as connection:
                project = connection.execute(
                    "SELECT worktree, sandboxes FROM project WHERE id = 'project'"
                ).fetchone()
                self.assertEqual(
                    project,
                    (str(target / "main"), json.dumps([str(target / "sandbox")])),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT directory, type, strategy FROM project_directory ORDER BY directory"
                    ).fetchall(),
                    [
                        (str(target / "directory"), "root", None),
                        (str(target / "main"), None, None),
                    ],
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM session WHERE id = 'session'").fetchone(),
                    (str(target / "session"),),
                )
                self.assertEqual(
                    connection.execute("SELECT directory FROM workspace WHERE id = 'workspace'").fetchone(),
                    (str(target / "workspace"),),
                )

    def test_repair_refuses_conflicting_duplicate_and_external_location(self) -> None:
        """Fail closed when a target twin disagrees or selected state has a third family."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            source, target, database = _recontaminated_fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE project_directory SET strategy = 'other' WHERE directory = ?",
                    (str(target / "directory"),),
                )
            before = structured_rows(database)
            with self.assertRaisesRegex(MoveError, "conflicting target"):
                plan_move_repair(
                    MoveRepairRequest(database, "project", str(source / "main"), str(target / "main"))
                )
            self.assertEqual(before, structured_rows(database))

        with temporary_directory() as root:
            source, target, database = _recontaminated_fixture(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE workspace SET directory = ? WHERE id = 'workspace'",
                    (str(root / "external"),),
                )
            with self.assertRaisesRegex(MoveError, "outside source and target families"):
                plan_move_repair(
                    MoveRepairRequest(database, "project", str(source / "main"), str(target / "main"))
                )

    def test_repair_refuses_stale_review_without_partial_mutation(self) -> None:
        """Revalidate all selected rows after acquiring the writer lock."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            source, target, database = _recontaminated_fixture(root)
            reviewed = plan_move_repair(
                MoveRepairRequest(database, "project", str(source / "main"), str(target / "main"))
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE session SET directory = ? WHERE id = 'session'",
                    (str(target / "session"),),
                )
            changed = structured_rows(database)

            with self.assertRaisesRegex(MoveError, "preview is stale"):
                apply_move_repair(reviewed)

            self.assertEqual(changed, structured_rows(database))

    def test_repair_preserves_sandbox_json_without_a_sandbox_action(self) -> None:
        """Avoid rewriting the project row when only row-backed locations need repair."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            source, target, database = _recontaminated_fixture(root)
            sandboxes = f'[  "{target / "sandbox"}"  ]'
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE project SET sandboxes = ? WHERE id = 'project'", (sandboxes,)
                )
            reviewed = plan_move_repair(
                MoveRepairRequest(database, "project", str(source / "main"), str(target / "main"))
            )

            apply_move_repair(reviewed)

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT sandboxes FROM project WHERE id = 'project'"
                    ).fetchone(),
                    (sandboxes,),
                )


def _recontaminated_fixture(root: Path) -> tuple[Path, Path, Path]:
    """Move one fixture, then model OpenCode reopening its retained source family."""
    source, target, database = move_fixture(root)
    apply_sibling_move(plan_sibling_move(MoveRequest(database, "project", str(target / "main"))))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE project SET sandboxes = ? WHERE id = 'project'",
            (json.dumps([str(source / "main"), str(target / "sandbox")]),),
        )
        connection.execute(
            "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
            ("project", str(target / "main"), None, None, 2),
        )
        connection.execute(
            "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
            ("project", str(source / "main"), None, None, 3),
        )
        connection.execute(
            "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
            ("project", str(source / "directory"), "root", None, 4),
        )
        connection.execute(
            "UPDATE session SET directory = ? WHERE id = 'session'",
            (str(source / "session"),),
        )
        connection.execute(
            "UPDATE workspace SET directory = ? WHERE id = 'workspace'",
            (str(source / "workspace"),),
        )
    return source, target, database


if __name__ == "__main__":
    unittest.main()
