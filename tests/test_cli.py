"""Contract tests for the version-1 command-line interface."""

from __future__ import annotations

import io
import json
import os
import shlex
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli
from opencode_db.transfer import TransferError, TransferOperationalError
from opencode_db.model import (
    EXIT_OPERATIONAL_FAILURE,
    EXIT_DECISION_REQUIRED,
    EXIT_PRECONDITION_REFUSED,
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
                    "snapshot-20260805T000000Z-000000000000000000000000",
                ],
                "cleanup prune-backup",
            ),
        ]

        for arguments, command in cases:
            with self.subTest(command=command):
                request = cli.parse_command(arguments)
                self.assertEqual(request.command, command)
                self.assertEqual(request.database, database)

    def test_cleanup_commands_select_the_omitted_database_from_xdg(self) -> None:
        """Keep cleanup request targets concrete without checking the selected file."""
        expected = "/missing/xdg/opencode/opencode.db"
        cases = [
            ["cleanup", "preview"],
            ["cleanup", "install", "--candidate", "candidate-1"],
            ["cleanup", "status"],
            ["cleanup", "abort", "--operation", "operation-1"],
            ["cleanup", "resume", "--operation", "operation-1"],
            ["cleanup", "rollback", "--operation", "operation-1"],
            [
                "cleanup",
                "prune-backup",
                "--snapshot",
                "snapshot-20260805T000000Z-000000000000000000000000",
            ],
        ]
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/missing/xdg", "HOME": "relative"}, clear=True):
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    self.assertEqual(cli.parse_command(arguments).database, expected)

    def test_cleanup_default_prunes_recorded_target_when_the_main_file_is_absent(self) -> None:
        """Leave catalog-derived cleanup actions available after the default main file moves."""
        with tempfile.TemporaryDirectory(dir="/tmp/opencode-db-v1") as directory:
            root = Path(directory)
            xdg = root / "xdg"
            database = xdg / "opencode" / "opencode.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg), "HOME": "relative"}, clear=True):
                preview_exit, preview = self._run_json(["cleanup", "preview", "--json"])
                database.unlink()
                prune_exit, pruned = self._run_json(
                    [
                        "cleanup",
                        "prune-backup",
                        "--snapshot",
                        str(preview["snapshot_id"]),
                        "--json",
                    ]
                )

        self.assertEqual(preview_exit, 0)
        self.assertEqual(prune_exit, 0)
        self.assertEqual(pruned["status"], Status.BACKUP_PRUNED.value)

    def test_transfer_commands_have_closed_top_level_grammar(self) -> None:
        """Accept the explicit transfer synopses and reject cleanup-only options."""
        cases = [
            (
                [
                    "export",
                    "--db",
                    "/tmp/opencode.db",
                    "--project-dir",
                    "/tmp/project",
                    "--export-dir",
                    "/tmp/exports",
                ],
                "export",
            ),
            (
                [
                    "import",
                    "--target-project-dir",
                    "/tmp/project",
                    "--db",
                    "/tmp/opencode.db",
                    "--import",
                    "/tmp/transfer.sqlite",
                ],
                "import",
            ),
        ]
        for arguments, command in cases:
            with self.subTest(command=command):
                request = cli.parse_command(arguments)
                self.assertEqual(request.command, command)

        exit_code, payload = self._run_json(
            ["export", "--db", "/tmp/opencode.db", "--json"]
        )
        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(payload["status"], Status.SYNTAX_ERROR.value)

    def test_transfer_commands_allow_an_omitted_database_option(self) -> None:
        """Select the shared environment default while retaining transfer-only grammar."""
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg", "HOME": "relative"}, clear=True):
            exported = cli.parse_command(
                ["export", "--project-dir", "/tmp/project", "--export-dir", "/tmp/exports"]
            )
            imported = cli.parse_command(
                [
                    "import",
                    "--target-project-dir",
                    "/tmp/project",
                    "--import",
                    "/tmp/transfer.sqlite",
                ]
            )

        self.assertEqual(exported.database, "/xdg/opencode/opencode.db")
        self.assertEqual(imported.database, "/xdg/opencode/opencode.db")

    def test_transfer_failures_use_operational_or_safety_exit_classes(self) -> None:
        """Classify transfer operational failures separately from safety refusals."""
        arguments = [
            "export",
            "--db",
            "/tmp/opencode.db",
            "--project-dir",
            "/tmp/project",
            "--export-dir",
            "/tmp/exports",
        ]
        cases = (
            (TransferError("project directory resolves ambiguously or not at all"), EXIT_PRECONDITION_REFUSED),
            (TransferOperationalError("database integrity check failed"), EXIT_OPERATIONAL_FAILURE),
        )

        for failure, expected_exit in cases:
            with self.subTest(failure=type(failure).__name__):
                stderr = io.StringIO()
                with patch.object(cli, "export_sessions", side_effect=failure):
                    with redirect_stderr(stderr):
                        exit_code = cli.main(arguments)

                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(stderr.getvalue(), f"opencode-db: {failure}\n")

    def test_prune_is_an_exposed_human_only_command(self) -> None:
        """Accept active session retention without repurposing the cleanup grammar."""
        request = cli.parse_command(
            ["prune", "--db", "/tmp/opencode.db", "--keep-newest", "2d"]
        )
        self.assertEqual(request.command, "prune")
        self.assertEqual(request.keep_newest, "2d")
        exit_code, payload = self._run_json(
            ["cleanup", "prune", "--database", "/tmp/opencode.db", "--json"]
        )
        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(payload["status"], Status.SYNTAX_ERROR.value)

    def test_prune_rejects_oversized_numeric_components_as_bounded_usage_errors(self) -> None:
        """Reject huge retention values before Python's integer conversion limit can escape."""
        for selector, value in (
            ("--oldest", "9" * 5000),
            ("--keep-newest", ("9" * 5000) + "d"),
            ("--target-size", ("9" * 5000) + "MiB"),
        ):
            with self.subTest(selector=selector):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    exit_code = cli.main(["prune", "--db", "/tmp/opencode.db", selector, value])

                self.assertEqual(exit_code, EXIT_USAGE)
                self.assertLessEqual(len(stderr.getvalue()), 512)

    def test_documentation_and_package_metadata_match_public_contract(self) -> None:
        """Keep install metadata and operator docs aligned with actual command names."""
        root = Path(__file__).parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        protocol = (root / "docs" / "protocol.md").read_text(encoding="utf-8")
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        normalized_readme = " ".join(readme.split())
        normalized_protocol = " ".join(protocol.split())

        project = metadata["project"]
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            metadata["project"]["scripts"]["opencode-db"], "opencode_db.cli:main"
        )
        for text in (readme, protocol):
            self.assertIn("prune-backup", text)
            self.assertIn("opencode-db import --target-project-dir", text)
            self.assertIn("opencode-db list-projects", text)
            self.assertIn("opencode-db show-project", text)
            self.assertIn("opencode-db show-session", text)
            self.assertIn("opencode-db prune", text)
            self.assertIn("absolute", text)
            self.assertIn("OpenCode", text)
            self.assertIn("--application-timeout-seconds", text)
        self.assertIn("[--db", protocol)
        self.assertIn("Add `--db /absolute/path", readme)
        self.assertIn("Linux only", readme)
        self.assertIn("The destructive `mv` and `prune` commands ask for confirmation", normalized_readme)
        self.assertIn("never starts OpenCode", normalized_readme)
        for text in (normalized_readme, normalized_protocol):
            self.assertIn("source or target project checkout entries", text)
            self.assertIn("SQLite-managed standard sidecars", text)
        for command in (
            "mv",
            "list-projects",
            "show-project",
            "show-session",
            "list-sessions",
            "export",
            "import",
            "cleanup preview",
            "cleanup install",
            "cleanup status",
            "cleanup resume",
            "cleanup rollback",
            "cleanup abort",
            "cleanup prune-backup",
        ):
            self.assertNotIn(f"opencode-db {command} [--", readme)
        self.assertIn("complete", readme)
        self.assertIn("uncertain", readme)
        self.assertIn("invalid", readme)
        self.assertIn("schema_version, command, ok", normalized_protocol)
        self.assertIn("manual_recovery_required", normalized_protocol)
        self.assertIn("destination paths", normalized_protocol)
        self.assertIn("source manifest", normalized_protocol)
        for text in (readme, protocol):
            self.assertIn("--target-project-dir", text)
            self.assertIn("--method sibling", text)
            self.assertIn("--yes", text)
            self.assertIn("--progress", text)
            self.assertIn("structured", text)
        for text in (readme, protocol):
            self.assertIn("opencode-db: prune preview", text)
            self.assertIn("sessions_to_prune", text)
            self.assertIn("sessions_to_keep", text)
            self.assertIn("oldest_surviving_session_updated", text)
            self.assertIn("numeric UTC offset", text)
            self.assertIn("--estimate-size", text)
            self.assertIn("--vacuum", text)
            self.assertIn("--yes", text)
            self.assertIn("rerun", text)
            self.assertIn("mode=ro", text)
            self.assertIn("250,000", text)
            self.assertIn("16 KiB", text)
            self.assertIn("64 MiB", text)
            self.assertIn("ten-second", text)

    def test_prune_backup_rejects_nonexact_snapshot_selectors_before_execution(
        self,
    ) -> None:
        """Reject paths, prefixes, and globs without reaching retained artifacts."""
        for selector in (
            "../snapshot",
            "snapshot-",
            "snapshot-*",
            "snapshot/child",
            "candidate-20260805T000000Z-000000000000000000000000",
        ):
            with self.subTest(selector=selector):
                exit_code, payload = self._run_json(
                    [
                        "cleanup",
                        "prune-backup",
                        "--database",
                        "/tmp/opencode.db",
                        "--snapshot",
                        selector,
                        "--json",
                    ]
                )
                self.assertEqual(exit_code, EXIT_USAGE)
                self.assertEqual(payload["status"], Status.SYNTAX_ERROR.value)

    def test_private_acceptance_uses_only_explicit_copies_and_bounded_output(
        self,
    ) -> None:
        """Keep the opt-in observed-case entry point credential-free by default."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            database = root / "copied.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('value')")
            source_bytes = database.read_bytes()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("private_acceptance.py")),
                    "--database",
                    str(database),
                    "--work-directory",
                    str(root / "private-work"),
                    "--scratch-dir",
                    str(root / "private-scratch"),
                    "--expected-status",
                    "complete",
                    "--expect-candidate",
                    "present",
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(database.read_bytes(), source_bytes)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "observed_status",
                "source_unchanged",
                "candidate_present",
                "outcome",
            },
        )
        self.assertEqual(payload["outcome"], "matched")
        self.assertTrue(payload["source_unchanged"])

    def test_prune_backup_reports_success_only_after_exact_group_removal(self) -> None:
        """Return backup_pruned without opening or changing active database bytes."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
            active_bytes = database.read_bytes()
            preview_exit, preview = self._run_json(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )
            assert isinstance(preview["snapshot_id"], str)
            prune_exit, pruned = self._run_json(
                [
                    "cleanup",
                    "prune-backup",
                    "--database",
                    str(database),
                    "--snapshot",
                    preview["snapshot_id"],
                    "--json",
                ]
            )
            self.assertEqual(database.read_bytes(), active_bytes)

        self.assertEqual(preview_exit, 0)
        self.assertEqual(prune_exit, 0)
        self.assertEqual(pruned["status"], Status.BACKUP_PRUNED.value)

    def test_help_lists_each_normative_synopsis_without_mutation(self) -> None:
        """Keep help read-only while exposing the frozen command names and selectors."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "opencode.db"
            database.write_bytes(b"unchanged")
            before = database.read_bytes()
            actions = [
                "mv",
                "export",
                "import",
                "preview",
                "install",
                "status",
                "abort",
                "resume",
                "rollback",
                "prune-backup",
                "list-projects",
                "show-project",
                "show-session",
                "prune",
            ]

            for action in actions:
                with self.subTest(action=action):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        arguments = (
                            [action, "--help"]
                            if action in {"mv", "export", "import", "list-projects", "show-project", "show-session", "list-sessions", "prune"}
                            else ["cleanup", action, "--help"]
                        )
                        self.assertEqual(cli.main(arguments), 0)
                    self.assertIn(action, stdout.getvalue())
                    option = (
                        "[--db ABSOLUTE_DB]"
                        if action in {"mv", "export", "import", "list-projects", "show-project", "show-session", "list-sessions", "prune"}
                        else "[--database ABSOLUTE_PATH]"
                    )
                    self.assertIn(option, stdout.getvalue())
                    if action == "prune":
                        self.assertIn("--yes", stdout.getvalue())
                        self.assertIn("--estimate-size", stdout.getvalue())
                        self.assertIn("--vacuum", stdout.getvalue())
                        self.assertIn("sessions_to_prune", stdout.getvalue())
                        self.assertIn("sessions_to_keep", stdout.getvalue())
                        self.assertIn(
                            "oldest_surviving_session_updated", stdout.getvalue()
                        )
                        self.assertIn("bypasses only the prompt", stdout.getvalue())
                        self.assertIn("rerun", stdout.getvalue())

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
        """Run real capture and cleanup while missing OpenCode domains stay unavailable."""
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
        preview = payload["preview"]
        assert isinstance(preview, dict)
        self.assertEqual(preview["projects"], "unavailable")
        self.assertEqual(preview["recent_sessions"], "unavailable")
        self.assertIsNotNone(payload["snapshot_id"])
        self.assertIsNotNone(payload["candidate_id"])
        self.assertEqual(
            payload["next_actions"],
            [
                "opencode-db cleanup install "
                f"--database {database} --candidate {payload['candidate_id']} "
                f"--approve-uncertain-report {payload['report_sha256']}"
            ],
        )

    def test_install_runs_the_exact_preview_candidate_without_confirmation(
        self,
    ) -> None:
        """Install a reviewed candidate directly from explicit CLI evidence."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('value')")
            preview_exit, preview = self._run_json(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )
            assert isinstance(preview["candidate_id"], str)
            install_exit, installed, _, stderr = self._run_json_details(
                [
                    "cleanup",
                    "install",
                    "--database",
                    str(database),
                    "--candidate",
                    preview["candidate_id"],
                    "--json",
                ]
            )

        self.assertEqual(preview_exit, 0)
        self.assertEqual(install_exit, 0)
        self.assertEqual(installed["status"], Status.INSTALLED.value)
        self.assertEqual(stderr, "")

    def test_follow_up_actions_quote_targets_and_omit_unpersisted_recovery(
        self,
    ) -> None:
        """Render pasteable paths and never invent an installation operation."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "database with spaces.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE entries (value TEXT)")

            preview_exit, preview = self._run_json(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )
            missing_exit, missing = self._run_json(
                [
                    "cleanup",
                    "install",
                    "--database",
                    str(database),
                    "--candidate",
                    "candidate-20260805T000000Z-0123456789abcdef01234567",
                    "--json",
                ]
            )

        self.assertEqual(preview_exit, 0)
        self.assertEqual(
            preview["next_actions"],
            [
                "opencode-db cleanup install "
                f"--database {shlex.quote(str(database))} "
                f"--candidate {preview['candidate_id']}"
            ],
        )
        self.assertEqual(missing_exit, EXIT_PRECONDITION_REFUSED)
        self.assertIsNone(missing["operation_id"])
        self.assertEqual(missing["next_actions"], [])

    def test_preview_exposes_only_bounded_opencode_project_and_session_fields(
        self,
    ) -> None:
        """Render four newest non-archived OpenCode sessions from a real candidate."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL, name TEXT)"
                )
                connection.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                    "title TEXT NOT NULL, time_updated INTEGER NOT NULL, time_archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO project VALUES (?, ?, ?)",
                    [("project-1", str(Path(directory) / "missing-worktree"), "One")],
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                    [
                        (f"session-{index}", "project-1", f"Title {index}", index, None)
                        for index in range(6)
                    ]
                    + [("archived", "project-1", "Archived", 99, 1)],
                )

            exit_code, payload, stdout, stderr = self._run_json_details(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )
            human_stderr = io.StringIO()
            with redirect_stderr(human_stderr):
                human_exit_code = cli.main(
                    ["cleanup", "preview", "--database", str(database)]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["status"], Status.COMPLETE.value)
        self.assertEqual(
            payload["preview"],
            {
                "projects": [
                    {
                        "id": "project-1",
                        "name": "One",
                        "origin": "unknown",
                        "worktree": str(Path(directory) / "missing-worktree"),
                    }
                ],
                "recent_sessions": [
                    {
                        "id": f"session-{index}",
                        "project_id": "project-1",
                        "time_updated": index,
                        "title": f"Title {index}",
                    }
                    for index in (5, 4, 3, 2)
                ],
                "table_issue_counts": {},
                "database_issue_count": 0,
            },
        )
        self.assertEqual(
            payload["next_actions"],
            [
                f"opencode-db cleanup install --database {database} --candidate {payload['candidate_id']}"
            ],
        )
        self.assertIn("project-1", stdout)
        self.assertNotIn("Archived", stdout)
        self.assertEqual(human_exit_code, 0)
        self.assertIn("project: project-1 | One", human_stderr.getvalue())
        self.assertIn(
            "session: session-5 | Title 5 | project-1 | 5", human_stderr.getvalue()
        )
        self.assertNotIn("Archived", human_stderr.getvalue())

    def test_preview_falls_back_without_archival_column_and_redacts_git_origin(
        self,
    ) -> None:
        """Use compatible session shape and a real read-only credential-bearing Git remote."""
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable on this test host")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(worktree)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "remote",
                    "add",
                    "origin",
                    "https://user:token@example.test/org/repo?secret=1",
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            database = root / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL, name TEXT)"
                )
                connection.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                    "title TEXT NOT NULL, time_updated INTEGER NOT NULL)"
                )
                connection.execute("CREATE TABLE message (secret TEXT)")
                connection.execute(
                    "INSERT INTO project VALUES ('p', ?, 'Project')", (str(worktree),)
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, 'p', ?, ?)",
                    [(f"s-{index}", f"Title {index}", index) for index in range(5)],
                )
                connection.execute(
                    "INSERT INTO message VALUES ('prompt token=do-not-show')"
                )

            exit_code, payload, stdout, _ = self._run_json_details(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )

        self.assertEqual(exit_code, 0)
        preview = payload["preview"]
        assert isinstance(preview, dict)
        projects = preview["projects"]
        assert isinstance(projects, list)
        self.assertEqual(projects[0]["origin"], "https://example.test/org/repo")
        self.assertEqual(
            [item["id"] for item in preview["recent_sessions"]],
            ["s-4", "s-3", "s-2", "s-1"],
        )
        self.assertNotIn("user:token", stdout)
        self.assertNotIn("do-not-show", stdout)

    def test_readable_invalid_candidate_keeps_bounded_foreign_key_summary(self) -> None:
        """Expose grouped foreign-key counts without offering an install action."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
                )
                connection.execute("INSERT INTO child VALUES (1)")

            exit_code, payload = self._run_json(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )

        self.assertEqual(exit_code, EXIT_OPERATIONAL_FAILURE)
        self.assertEqual(payload["status"], Status.INVALID.value)
        preview = payload["preview"]
        assert isinstance(preview, dict)
        self.assertEqual(preview["table_issue_counts"], {"child": 1})
        self.assertEqual(payload["next_actions"], [])

    def test_failed_global_integrity_uses_table_scoped_counts_without_messages(
        self,
    ) -> None:
        """Count structured table and database failures without rendering SQLite text."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            database = Path(directory) / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE checked (value INTEGER CHECK (value > 0))"
                )
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute("INSERT INTO checked VALUES (-1)")

            exit_code, payload, stdout, _ = self._run_json_details(
                ["cleanup", "preview", "--database", str(database), "--json"]
            )

        self.assertEqual(exit_code, EXIT_OPERATIONAL_FAILURE)
        preview = payload["preview"]
        assert isinstance(preview, dict)
        self.assertGreaterEqual(preview["table_issue_counts"].get("checked", 0), 1)
        self.assertGreaterEqual(preview["database_issue_count"], 1)
        self.assertNotIn("CHECK constraint failed", stdout)

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
