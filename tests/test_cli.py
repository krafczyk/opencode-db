"""Contract tests for the version-1 command-line interface."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli
from opencode_db.model import (
    EXIT_OPERATIONAL_FAILURE,
    EXIT_DECISION_REQUIRED,
    EXIT_USAGE,
    RESULT_SCHEMA_VERSION,
    Result,
    Status,
)


class CliContractTests(unittest.TestCase):
    """Freeze command parsing, JSON rendering, and bootstrap safety behavior."""

    def test_every_normative_command_parses_with_its_required_selection(self) -> None:
        """Accept each U2 command synopsis without performing cleanup work."""
        database = "/tmp/opencode.db"
        cases = [
            (["cleanup", "preview", "--database", database], "cleanup preview"),
            (
                [
                    "cleanup",
                    "install",
                    "--database",
                    database,
                    "--candidate",
                    "candidate-1",
                ],
                "cleanup install",
            ),
            (["cleanup", "status", "--database", database], "cleanup status"),
            (
                [
                    "cleanup",
                    "abort",
                    "--database",
                    database,
                    "--operation",
                    "operation-1",
                ],
                "cleanup abort",
            ),
            (
                [
                    "cleanup",
                    "resume",
                    "--database",
                    database,
                    "--operation",
                    "operation-1",
                ],
                "cleanup resume",
            ),
            (
                [
                    "cleanup",
                    "rollback",
                    "--database",
                    database,
                    "--operation",
                    "operation-1",
                ],
                "cleanup rollback",
            ),
            (
                [
                    "cleanup",
                    "prune-backup",
                    "--database",
                    database,
                    "--snapshot",
                    "snapshot-1",
                ],
                "cleanup prune-backup",
            ),
        ]

        for arguments, command in cases:
            with self.subTest(command=command):
                request = cli.parse_command(arguments)
                self.assertEqual(request.command, command)
                self.assertEqual(request.database, database)

    def test_prune_is_not_an_exposed_command(self) -> None:
        """Reserve the future short prune name outside the version-1 grammar."""
        result = self._run_json(
            ["cleanup", "prune", "--database", "/tmp/opencode.db", "--json"]
        )

        self.assertEqual(result[0], EXIT_USAGE)
        self.assertEqual(result[1]["status"], Status.SYNTAX_ERROR.value)

    def test_help_lists_each_normative_synopsis_without_mutation(self) -> None:
        """Keep help read-only while exposing the frozen command names and selectors."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "opencode.db"
            database.write_bytes(b"unchanged")
            before = database.read_bytes()
            actions = [
                "preview",
                "install",
                "status",
                "abort",
                "resume",
                "rollback",
                "prune-backup",
            ]

            for action in actions:
                with self.subTest(action=action):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(cli.main(["cleanup", action, "--help"]), 0)
                    self.assertIn(f"cleanup {action}", stdout.getvalue())
                    self.assertIn("--database ABSOLUTE_PATH", stdout.getvalue())

            self.assertEqual(database.read_bytes(), before)

    def test_machine_invalid_input_is_one_pure_bounded_json_document(self) -> None:
        """Render grammar failures as a single schema-versioned JSON result."""
        exit_code, payload, stdout, stderr = self._run_json_details(
            ["cleanup", "preview", "--database", "relative.db", "--json"]
        )

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.endswith("\n"))
        self.assertLessEqual(len(stdout.encode("ascii")), 64 * 1024)
        self.assertEqual(payload["schema_version"], RESULT_SCHEMA_VERSION)
        self.assertEqual(payload["command"], "cleanup preview")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], Status.SYNTAX_ERROR.value)
        self.assertEqual(payload["exit_code"], EXIT_USAGE)
        self.assertEqual(set(payload), set(Result.json_fields()))

    def test_detached_stdin_does_not_prompt_before_refusing_unimplemented_work(
        self,
    ) -> None:
        """Begin from explicit input without reading stdin or asking for shutdown confirmation."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "opencode.db"
            database.write_bytes(b"not a sqlite database")
            read_end, write_end = os.pipe()
            os.close(write_end)
            original_stdin = sys.stdin
            try:
                with os.fdopen(read_end, "r", encoding="utf-8") as detached_stdin:
                    sys.stdin = detached_stdin
                    exit_code, payload, stdout, stderr = self._run_json_details(
                        ["cleanup", "preview", "--database", str(database), "--json"]
                    )
            finally:
                sys.stdin = original_stdin

        self.assertEqual(exit_code, EXIT_OPERATIONAL_FAILURE)
        self.assertEqual(payload["status"], Status.INVALID.value)
        self.assertNotIn("confirm", stdout.lower())
        self.assertNotIn("confirm", stderr.lower())

    def test_preview_renders_the_actual_cleanup_class_not_capture_completion(
        self,
    ) -> None:
        """Run real capture and cleanup while U5 domain preview remains unavailable."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            Path(f"{database}-shm").write_bytes(b"stale shared memory")
            exit_code, payload = self._run_json(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )

        self.assertEqual(exit_code, EXIT_DECISION_REQUIRED)
        self.assertEqual(payload["status"], Status.UNCERTAIN.value)
        self.assertEqual(payload["completeness"], "uncertain")
        self.assertEqual(payload["preview"], None)
        self.assertIsNotNone(payload["snapshot_id"])
        self.assertIsNotNone(payload["candidate_id"])

    def test_rejects_path_ids_prefixes_overlong_and_nonfinite_deadlines(self) -> None:
        """Reject ambiguous identifiers and malformed bounded option values before execution."""
        cases = [
            [
                "cleanup",
                "install",
                "--database",
                "/tmp/opencode.db",
                "--candidate",
                "../candidate",
            ],
            [
                "cleanup",
                "abort",
                "--database",
                "/tmp/opencode.db",
                "--operation",
                "abc*",
            ],
            [
                "cleanup",
                "rollback",
                "--database",
                "/tmp/opencode.db",
                "--operation",
                "a" * 257,
            ],
            [
                "cleanup",
                "preview",
                "--database",
                "/tmp/opencode.db",
                "--deadline-seconds",
                "NaN",
            ],
        ]

        for arguments in cases:
            with self.subTest(arguments=arguments):
                exit_code, payload = self._run_json([*arguments, "--json"])
                self.assertEqual(exit_code, EXIT_USAGE)
                self.assertEqual(payload["status"], Status.SYNTAX_ERROR.value)

    def test_json_renderer_is_sorted_ascii_safe_and_redacts_bounded_diagnostics(
        self,
    ) -> None:
        """Prevent credential-like or unbounded exception text from leaking to machine output."""
        result = Result.failure(
            command="cleanup preview",
            status=Status.OPERATIONAL_FAILURE,
            exit_code=5,
            diagnostic_code="unexpected_error",
            diagnostic_message="token=secret " + ("x" * 10_000),
        )

        rendered = cli.render_json(result)
        payload = json.loads(rendered)

        self.assertTrue(rendered.endswith("\n"))
        self.assertEqual(
            rendered,
            json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
            + "\n",
        )
        self.assertLessEqual(len(rendered.encode("ascii")), 64 * 1024)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("x" * 100, rendered)
        self.assertEqual(payload["diagnostics"][0]["code"], "unexpected_error")

    def test_model_rejects_unknown_future_schema_without_mutation_surface(self) -> None:
        """Fail closed for unsupported persisted result schemas before future artifact code exists."""
        with self.assertRaises(ValueError):
            Result.from_json({"schema_version": 2})

    @staticmethod
    def _run_json(arguments: list[str]) -> tuple[int, dict[str, object]]:
        """Run the CLI in JSON mode and return its exit code and parsed payload."""
        exit_code, payload, _, _ = CliContractTests._run_json_details(arguments)
        return exit_code, payload

    @staticmethod
    def _run_json_details(
        arguments: list[str],
    ) -> tuple[int, dict[str, object], str, str]:
        """Run the CLI with captured streams without invoking a subprocess."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        rendered = stdout.getvalue()
        return exit_code, json.loads(rendered), rendered, stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
