"""Command-line contracts for explicit sibling move repair."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli, move_repair_cli
from opencode_db.model import EXIT_PRECONDITION_REFUSED, EXIT_USAGE
from opencode_db.move import MoveRequest, apply_sibling_move, plan_sibling_move
from move_test_support import move_fixture, temporary_directory
from test_move_cli import _ReadTrackingStream


class MoveRepairCliTests(unittest.TestCase):
    """Freeze repair parsing, preview, confirmation, and dispatch behavior."""

    def test_repair_move_has_closed_human_only_grammar(self) -> None:
        """Require exact source, target, project, and bounded database selectors."""
        request = cli.parse_command(
            [
                "repair-move",
                "--project-id",
                "project",
                "--source-project-dir",
                "/source/main",
                "--target-project-dir",
                "/target/main",
                "--db",
                "/tmp/opencode.db",
                "--application-timeout-seconds",
                "30.5",
                "--yes",
            ]
        )
        self.assertEqual(request.command, "repair-move")
        self.assertEqual(request.source_project_dir, "/source/main")
        self.assertEqual(request.target_project_dir, "/target/main")
        self.assertEqual(request.application_timeout_seconds, 30.5)
        self.assertTrue(request.yes)

        for rejected in (
            ["repair-move", "--project-id", "project"],
            [
                "repair-move",
                "--project-id",
                "project",
                "--source-project-dir",
                "relative",
                "--target-project-dir",
                "/target/main",
            ],
            [
                "repair-move",
                "--project-id",
                "project",
                "--source-project-dir",
                "/source/main",
                "--target-project-dir",
                "/target/main",
                "--json",
            ],
        ):
            with self.subTest(arguments=rejected), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(rejected), EXIT_USAGE)

    def test_repair_move_previews_and_applies_recontaminated_fixture(self) -> None:
        """Render exact drop actions and commit them after explicit automation opt-in."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            source, target, database = _fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "repair-move",
                        "--project-id",
                        "project",
                        "--source-project-dir",
                        str(source / "main"),
                        "--target-project-dir",
                        str(target / "main"),
                        "--db",
                        str(database),
                        "--yes",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("action=drop categories=project.sandbox=1,project_directory.directory=1", stdout.getvalue())
            self.assertTrue(stdout.getvalue().endswith("opencode-db: move repair committed\n"))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT sandboxes FROM project WHERE id = 'project'").fetchone(),
                    (json.dumps([str(target / "sandbox")]),),
                )

    def test_repair_move_requires_terminal_confirmation_before_application(self) -> None:
        """Preview detached requests but never read or mutate without ``--yes``."""
        request = move_repair_cli.MoveRepairCommandRequest(
            "/tmp/opencode.db",
            "project",
            "/source/main",
            "/target/main",
            False,
            10.0,
        )
        reviewed = move_repair_cli.ReviewedMoveRepairPlan(
            move_repair_cli.MoveRepairRequest(
                request.database,
                request.project_id,
                request.source_project_dir,
                request.target_project_dir,
            ),
            _captured_state(),
            (_mapping(),),
        )
        stdin = _ReadTrackingStream("y\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(move_repair_cli, "plan_move_repair", return_value=reviewed), patch.object(
            move_repair_cli, "apply_move_repair"
        ) as apply:
            exit_code = move_repair_cli.execute_move_repair(
                request, stdin=stdin, stdout=stdout, stderr=stderr
            )
        self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
        self.assertEqual(stdin.readline_count, 0)
        self.assertIn("source='/source/main'", stdout.getvalue())
        apply.assert_not_called()


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    """Create one moved fixture with the old main re-registered by OpenCode."""
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
    return source, target, database


def _captured_state():
    """Return minimal immutable state for stream-boundary tests."""
    from opencode_db.move import CapturedState

    return CapturedState((), "project", "/target/main", '["/source/main"]', ())


def _mapping():
    """Return one minimal drop mapping for stream-boundary tests."""
    from opencode_db.move import GitEvidence
    from opencode_db.move_repair import RepairMapping, RepairMembership

    evidence = GitEvidence("root:abc", "attached", "main", "a" * 40, "/repo/.git", "/repo/.git")
    return RepairMapping(
        "/source/main",
        "/target/main",
        (RepairMembership("project.sandbox", ("project", "0"), "drop"),),
        evidence,
        evidence,
    )


if __name__ == "__main__":
    unittest.main()
