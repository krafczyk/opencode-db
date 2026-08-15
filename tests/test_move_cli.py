"""Contract tests for the human-only sibling move command-line interface."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli, move_cli
from opencode_db.model import (
    EXIT_OPERATIONAL_FAILURE,
    EXIT_PRECONDITION_REFUSED,
    EXIT_USAGE,
)
from move_test_support import move_fixture, temporary_directory


class MoveCliTests(unittest.TestCase):
    """Freeze move parsing, preview rendering, confirmation, and progress behavior."""

    def test_mv_has_closed_grammar_and_human_only_json_rejection(self) -> None:
        """Accept only sibling selectors before any planner access."""
        arguments = [
            "mv", "--project-id", "project", "--target-project-dir", "/target/main",
            "--db", "/tmp/opencode.db", "--method", "sibling",
            "--application-timeout-seconds", "60.5", "--yes", "--progress",
        ]

        request = cli.parse_command(arguments)

        self.assertEqual(request.command, "mv")
        self.assertEqual(request.database, "/tmp/opencode.db")
        self.assertEqual(request.method, "sibling")
        self.assertTrue(request.yes)
        self.assertTrue(request.progress)
        self.assertEqual(request.application_timeout_seconds, 60.5)
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg", "HOME": "relative"}, clear=True):
            defaulted = cli.parse_command(
                ["mv", "--project-id", "project", "--target-project-dir", "/target/main"]
            )
        self.assertEqual(defaulted.database, "/xdg/opencode/opencode.db")
        self.assertEqual(defaulted.method, "sibling")
        self.assertEqual(defaulted.application_timeout_seconds, 10.0)
        for rejected in (
            ["mv", "--project-id", "project"],
            ["mv", "--project-id", "project", "--project-id", "other", "--target-project-dir", "/target/main"],
            ["mv", "--project-id", "project", "--target-project-dir", "relative"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--method", "other"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--json"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--application-timeout-seconds"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--application-timeout-seconds", "0"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--application-timeout-seconds", "nan"],
            ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--application-timeout-seconds", "86401"],
        ):
            with self.subTest(arguments=rejected):
                stderr = io.StringIO()
                with patch.object(move_cli, "plan_sibling_move") as planner, redirect_stderr(stderr):
                    self.assertEqual(cli.main(rejected), EXIT_USAGE)
                planner.assert_not_called()
                self.assertIn("opencode-db:", stderr.getvalue())

    def test_mv_interactive_and_yes_paths_apply_real_sibling_fixtures(self) -> None:
        """Preview real plans, authorize exact ``y``, and support detached ``--yes``."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            _source, target, database = move_fixture(root)
            stdin = _TtyStream("y\n")
            stdout = _TtyStream()
            stderr = _TtyStream()
            with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                exit_code = cli.main(
                    [
                        "mv", "--project-id", "project", "--target-project-dir", str(target / "main"),
                        "--db", str(database), "--progress",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("project.worktree=1", stdout.getvalue())
            self.assertIn("move committed", stdout.getvalue())
            self.assertGreater(stdout.flush_count, 0)
            self.assertTrue(stderr.getvalue().endswith("\n"))
            for phase in ("collection", "Git pair validation", "revalidation", "update groups"):
                self.assertIn(f"progress: {phase}", stderr.getvalue())
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT worktree FROM project WHERE id = 'project'").fetchone(),
                    (str(target / "main"),),
                )

        with temporary_directory() as root:
            _source, target, database = move_fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "mv", "--project-id", "project", "--target-project-dir", str(target / "main"),
                        "--db", str(database), "--yes",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("source=", stdout.getvalue())
            self.assertNotIn("Apply sibling move", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_mv_preview_has_exact_ordered_fixture_mappings_and_category_counts(self) -> None:
        """Render every normal and duplicate fixture mapping in exact deterministic order."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        expected_categories = {
            False: (
                ("directory", "project_directory.directory=1"),
                ("main", "project.worktree=1"),
                ("sandbox", "project.sandbox=1"),
                ("session", "session.directory=1"),
                ("workspace", "workspace.directory=1"),
            ),
            True: (
                ("main", "project.worktree=1,project_directory.directory=1,session.directory=1,workspace.directory=1"),
                ("sandbox", "project.sandbox=1"),
            ),
        }
        for duplicate, mappings in expected_categories.items():
            with self.subTest(duplicate=duplicate), temporary_directory() as root:
                source, target, database = move_fixture(root, duplicate=duplicate)
                stdout = io.StringIO()
                with patch.object(move_cli, "apply_sibling_move") as apply, redirect_stdout(stdout):
                    exit_code = cli.main(
                        [
                            "mv", "--project-id", "project", "--target-project-dir", str(target / "main"),
                            "--db", str(database), "--yes",
                        ]
                    )

                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    stdout.getvalue().splitlines(),
                    [
                        f"source='{source / name}' target='{target / name}' categories={categories}"
                        for name, categories in mappings
                    ]
                    + ["opencode-db: move committed"],
                )
                apply.assert_called_once()

    def test_mv_preview_escapes_quote_backslash_control_and_non_ascii_paths(self) -> None:
        """Keep renderer output single-line ASCII-safe without invalid Git fixture paths."""
        value = "/path/quo'te\\slash\ncontrol-\x01-\u00e9-\U0001f600"
        stdout = io.StringIO()

        move_cli._render_move_preview(_reviewed_move(value, value.replace("/path", "/target")), stdout)

        self.assertEqual(
            stdout.getvalue(),
            "source='/path/quo\\'te\\\\slash\\x0acontrol-\\x01-\\xe9-\\U0001f600' "
            "target='/target/quo\\'te\\\\slash\\x0acontrol-\\x01-\\xe9-\\U0001f600' "
            "categories=project.worktree=1\n",
        )

    def test_mv_refuses_detached_streams_without_reading_stdin_or_applying(self) -> None:
        """Require both terminal streams before reading any confirmation input or applying."""
        cases = (
            ("detached stdout", _ReadTrackingTty("y\n"), io.StringIO()),
            ("detached stdin", _ReadTrackingStream("y\n"), _TtyStream()),
            ("stdin isatty error", _OSErrorTty("y\n"), _TtyStream()),
        )
        for name, stdin, stdout in cases:
            with self.subTest(name=name), patch.object(
                move_cli, "plan_sibling_move", return_value=_reviewed_move()
            ), patch.object(move_cli, "apply_sibling_move") as apply:
                stderr = io.StringIO()
                with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                    exit_code = cli.main(
                        ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--db", "/tmp/opencode.db"]
                    )
                self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
                self.assertEqual(stdin.readline_count, 0)
                self.assertIn("terminal", stderr.getvalue())
                apply.assert_not_called()

    def test_mv_refuses_detached_or_nonexact_confirmation_without_application(self) -> None:
        """Accept only an exact lowercase confirmation after both streams validate."""
        for reply in ("n\n", "Y\n", " y\n", "y \n", "", "\n"):
            with self.subTest(reply=reply), patch.object(
                move_cli, "plan_sibling_move", return_value=_reviewed_move()
            ), patch.object(move_cli, "apply_sibling_move") as apply:
                stdin = _TtyStream(reply)
                stdout = _TtyStream()
                stderr = _TtyStream()
                with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                    exit_code = cli.main(
                        ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--db", "/tmp/opencode.db"]
                    )
                self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
                self.assertIn("cancelled", stderr.getvalue())
                apply.assert_not_called()

        stdin = _InterruptingTty()
        stdout = _TtyStream()
        stderr = _TtyStream()
        with patch.object(move_cli, "plan_sibling_move", return_value=_reviewed_move()), patch.object(move_cli, "apply_sibling_move") as apply:
            with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                exit_code = cli.main(
                    ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--db", "/tmp/opencode.db"]
                )
        self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
        self.assertIn("cancelled", stderr.getvalue())
        apply.assert_not_called()

    def test_mv_preserves_exit_classes_and_redirected_progress_boundary(self) -> None:
        """Keep operational failures and redirected aggregate progress separate from output."""
        reviewed = _reviewed_move()

        def plan_with_progress(_request: object, *, progress: object = None) -> object:
            assert callable(progress)
            progress("collection", 0, 1, False)
            progress("collection", 1, 1, True)
            progress("Git pair validation", 0, 1, False)
            progress("Git pair validation", 1, 1, True)
            return reviewed

        def fail_during_revalidation(
            _reviewed: object,
            *,
            progress: object = None,
            application_timeout_seconds: float,
        ) -> None:
            assert callable(progress)
            self.assertEqual(application_timeout_seconds, 10.0)
            progress("revalidation", 0, 1, False)
            raise move_cli.MoveOperationalError("git is unavailable")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(move_cli, "plan_sibling_move", side_effect=plan_with_progress), patch.object(move_cli, "apply_sibling_move", side_effect=fail_during_revalidation):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--db", "/tmp/opencode.db", "--yes", "--progress"]
                )
        self.assertEqual(exit_code, EXIT_OPERATIONAL_FAILURE)
        self.assertIn("source='/source/main'", stdout.getvalue())
        self.assertIn("progress: collection 1/1 complete", stderr.getvalue())
        self.assertIn("progress: Git pair validation 1/1 complete", stderr.getvalue())
        self.assertIn("progress: revalidation failed", stderr.getvalue())
        self.assertNotIn("\r", stderr.getvalue())
        self.assertNotIn("/source/main", stderr.getvalue())

        capped = io.StringIO()
        reporter = move_cli._MoveProgressReporter(capped)
        for completed in range(150):
            reporter.update("collection", completed, 150, False)
        reporter.update("collection", 150, 150, True)
        lines = capped.getvalue().splitlines()
        self.assertLessEqual(len(lines), 100)
        self.assertTrue(lines[-1].endswith("complete"))

        with patch.object(move_cli, "plan_sibling_move", side_effect=move_cli.MoveError("move schema is incomplete")), patch.object(move_cli, "apply_sibling_move") as apply:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli.main(
                    ["mv", "--project-id", "project", "--target-project-dir", "/target/main", "--db", "/tmp/opencode.db", "--yes"]
                )
        self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
        self.assertIn("schema is incomplete", stderr.getvalue())
        apply.assert_not_called()

    def test_mv_forwards_application_timeout_to_atomic_apply(self) -> None:
        """Pass the validated timeout override only to the application boundary."""
        reviewed = _reviewed_move()
        with patch.object(move_cli, "plan_sibling_move", return_value=reviewed), patch.object(
            move_cli, "apply_sibling_move"
        ) as apply:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli.main(
                    [
                        "mv",
                        "--project-id",
                        "project",
                        "--target-project-dir",
                        "/target/main",
                        "--db",
                        "/tmp/opencode.db",
                        "--application-timeout-seconds",
                        "45.25",
                        "--yes",
                    ]
                )

        self.assertEqual(exit_code, 0)
        apply.assert_called_once_with(
            reviewed,
            progress=None,
            application_timeout_seconds=45.25,
        )

    def test_mv_keeps_actionable_planner_refusals_on_stderr(self) -> None:
        """Preserve missing-location categories and quoted paths through the CLI boundary."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with temporary_directory() as root:
            _source, target, database = move_fixture(root)
            shutil.rmtree(target / "sandbox")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "mv", "--project-id", "project", "--target-project-dir", str(target / "main"),
                        "--db", str(database), "--yes",
                    ]
                )
        self.assertEqual(exit_code, EXIT_PRECONDITION_REFUSED)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("target directory is missing", stderr.getvalue())
        self.assertIn("project.sandbox", stderr.getvalue())
        self.assertIn("path=\"", stderr.getvalue())


class _TtyStream(io.StringIO):
    """Provide an in-memory text stream that reports terminal capability for CLI tests."""

    def __init__(self, initial_value: str = "") -> None:
        """Initialize the stream and track explicit flushes before prompt reads."""
        super().__init__(initial_value)
        self.flush_count = 0

    def flush(self) -> None:
        """Record one explicit flush while preserving normal text-stream behavior."""
        self.flush_count += 1
        super().flush()

    def isatty(self) -> bool:
        """Return true so confirmation and terminal-progress paths are testable."""
        return True


class _ReadTrackingTty(_TtyStream):
    """Record forbidden confirmation reads from a stream reported as a terminal."""

    def __init__(self, initial_value: str = "") -> None:
        """Initialize one terminal stream with a zeroed read counter."""
        super().__init__(initial_value)
        self.readline_count = 0

    def readline(self, *args: object, **kwargs: object) -> str:
        """Count each attempted confirmation read before delegating to the text stream."""
        self.readline_count += 1
        return super().readline(*args, **kwargs)


class _ReadTrackingStream(_ReadTrackingTty):
    """Record reads from a stream deliberately reported as detached."""

    def isatty(self) -> bool:
        """Report detached capability while retaining a readable in-memory stream."""
        return False


class _OSErrorTty(_ReadTrackingTty):
    """Model a terminal capability probe that fails like a detached stream."""

    def isatty(self) -> bool:
        """Raise the supported capability-probe failure without reading input."""
        raise OSError("terminal probe failed")


class _InterruptingTty(_TtyStream):
    """Model an interactive input stream interrupted at the confirmation read."""

    def readline(self) -> str:
        """Raise the same interruption that an operator can send at a prompt."""
        raise KeyboardInterrupt


def _reviewed_move(source: str = "/source/main", target: str = "/target/main") -> object:
    """Create one minimal reviewed mapping without accessing SQLite or Git."""
    from opencode_db.move import (
        CapturedState,
        GitEvidence,
        LocationMembership,
        MoveMapping,
        MoveRequest,
        ReviewedMovePlan,
    )

    request = MoveRequest("/tmp/opencode.db", "project", target)
    state = CapturedState((), "project", source, "[]", ())
    evidence = GitEvidence("root:abc", "attached", "main", "a" * 40, "/source/.git", "/source/.git")
    mapping = MoveMapping(
        source, target, (LocationMembership("project.worktree", ("project",)),), evidence, evidence
    )
    return ReviewedMovePlan(request, state, (mapping,))


if __name__ == "__main__":
    unittest.main()
