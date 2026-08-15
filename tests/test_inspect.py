"""Focused read-only project and session inspection contracts."""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli
from opencode_db.inspect import InspectionError, inspect_database
from opencode_db.model import EXIT_PRECONDITION_REFUSED, EXIT_USAGE
from opencode_db.prune import session_logical_sizes


_TEST_ROOT = Path("/tmp/opencode-db-v1")


@contextmanager
def _temporary_directory():
    """Yield one private disposable fixture directory under the approved root."""
    _TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_TEST_ROOT, 0o700)
    if stat.S_IMODE(_TEST_ROOT.stat().st_mode) != 0o700:
        raise RuntimeError("test root must be private")
    with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory:
        yield Path(directory)


class InspectionTests(unittest.TestCase):
    """Prove bounded selection, stable summaries, transcripts, and read-only access."""

    def test_default_database_uses_xdg_then_home_fallback_and_canonical_path(self) -> None:
        """Resolve only absolute XDG/HOME bases and print the canonical selected file."""
        with _temporary_directory() as root:
            xdg = root / "xdg"
            database = xdg / "opencode" / "opencode.db"
            database.parent.mkdir(parents=True)
            self._create_database(database)
            code, stdout, stderr = self._run(
                ["list-projects"], {"XDG_DATA_HOME": str(xdg), "HOME": "/relative"}
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(f"database: {database.resolve()}", stdout)

            fallback = root / "home" / ".local" / "share" / "opencode" / "opencode.db"
            fallback.parent.mkdir(parents=True)
            self._create_database(fallback)
            code, stdout, stderr = self._run(
                ["list-projects"], {"XDG_DATA_HOME": "relative", "HOME": str(root / "home")}
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(f"database: {fallback.resolve()}", stdout)
            missing, _stdout, missing_stderr = self._run(
                ["list-projects"], {"XDG_DATA_HOME": "relative", "HOME": "relative"}
            )
            self.assertEqual(missing, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(
                missing_stderr, "opencode-db: default database path is unavailable\n"
            )

    def test_list_projects_is_deterministic_and_never_reads_message_data(self) -> None:
        """Show grouped project locations while leaving arbitrary transcript rows private."""
        with _temporary_directory() as root:
            database = root / "db" / "opencode.db"
            database.parent.mkdir()
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                    ("b", "/work/b", "B", '["/sandbox/b"]', 2),
                )
                connection.execute(
                    "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                    ("a", "/work/a", "A", '["/sandbox/a", "/sandbox/z"]', 1),
                )
                connection.executemany(
                    "INSERT INTO project_directory VALUES (?, ?, ?, ?)",
                    [("a", "/work/a", "main", "git"), ("a", "/work/a/sub", "root", None)],
                )
                connection.executemany(
                    "INSERT INTO workspace VALUES (?, ?, ?)",
                    [("wa", "a", "/work/a"), ("wb", "a", "/work/a")],
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                    [("s2", "a", "/work/a", "two", 2), ("s1", "a", "/work/a", "one", 1)],
                )
                connection.execute(
                    "INSERT INTO message VALUES ('m-secret', 's1', 1, ?)",
                    (json.dumps({"role": "user", "secret": "do-not-show"}),),
                )

            code, stdout, stderr = self._run(["list-projects", "--db", str(database)])
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertLess(stdout.index("project_id: a"), stdout.index("project_id: b"))
            self.assertIn("sandbox: /sandbox/a", stdout)
            self.assertIn("project_directory: /work/a | main | git", stdout)
            self.assertIn("workspace_directory: /work/a | 2", stdout)
            self.assertIn("session_directory: /work/a | 2", stdout)
            self.assertNotIn("do-not-show", stdout)

    def test_show_project_requires_one_exact_row_and_avoids_transcripts(self) -> None:
        """Reject missing IDs and print summary counts without message or input bodies."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session_message VALUES ('v2', 'session', 'user', 1, 1, ?)",
                    (json.dumps({"text": "v2 secret"}),),
                )
                connection.execute(
                    "INSERT INTO message VALUES ('legacy', 'session', 2, ?)",
                    (json.dumps({"role": "assistant"}),),
                )
                connection.execute(
                    "INSERT INTO session_input VALUES ('pending', 'session', ?, NULL, 3)",
                    (json.dumps({"text": "pending secret"}),),
                )
            code, stdout, stderr = self._run(
                ["show-project", "--db", str(database), "--project-id", "project"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("session_id: session", stdout)
            self.assertIn("v2_turns: 1", stdout)
            self.assertIn("legacy_turns: 1", stdout)
            self.assertIn("pending_inputs: 1", stdout)
            self.assertLess(stdout.index("v2_turns: 1"), stdout.index("title: Session"))
            self.assertNotIn("v2 secret", stdout)
            self.assertNotIn("pending secret", stdout)
            missing, _stdout, missing_stderr = self._run(
                ["show-project", "--db", str(database), "--project-id", "missing"]
            )
            self.assertEqual(missing, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(missing_stderr, "opencode-db: project ID was not found\n")
            missing, _stdout, missing_stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "missing"]
            )
            self.assertEqual(missing, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(missing_stderr, "opencode-db: session ID was not found\n")
            usage, _stdout, usage_stderr = self._run(["list-projects", "--json"])
            self.assertEqual(usage, EXIT_USAGE)
            self.assertEqual(
                usage_stderr,
                "opencode-db: Command arguments do not match the supported grammar.\n",
            )

    def test_show_session_renders_v2_in_sequence_and_deduplicates_promoted_inputs(self) -> None:
        """Render text only, label non-text V2 records, and retain only pending inbox prompts."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.executemany(
                    "INSERT INTO session_message VALUES (?, 'session', ?, ?, ?, ?)",
                    [
                        ("a", "assistant", 3, 3, json.dumps({"content": [{"type": "text", "text": "answer"}, {"type": "reasoning", "text": "hidden"}, {"type": "tool", "name": "read", "state": {"status": "completed", "input": {"secret": 1}, "result": "hidden"}}]})),
                        ("u", "user", 1, 1, json.dumps({"text": "question"})),
                        ("s", "system", 2, 2, json.dumps({"text": "system text"})),
                        ("shell", "shell", 4, 4, json.dumps({"command": "secret command", "output": "secret output"})),
                    ],
                )
                connection.executemany(
                    "INSERT INTO session_input VALUES (?, 'session', ?, ?, ?)",
                    [
                        ("u", json.dumps({"text": "question"}), 1, 1),
                        ("pending", json.dumps({"text": "later prompt"}), None, 2),
                    ],
                )
            code, stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "session"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertLess(stdout.index("user: question"), stdout.index("system: system text"))
            self.assertLess(stdout.index("system: system text"), stdout.index("assistant: answer"))
            self.assertIn("reasoning: present", stdout)
            self.assertIn("tool: read | completed", stdout)
            self.assertIn("shell: present", stdout)
            self.assertIn("pending_input: later prompt", stdout)
            self.assertNotIn("secret command", stdout)
            self.assertNotIn("secret output", stdout)
            self.assertNotIn("hidden", stdout)
            self.assertEqual(stdout.count("question"), 1)

    def test_show_session_renders_legacy_parts_and_keeps_mixed_sections_separate(self) -> None:
        """Keep legacy ordering and V2 chronology separate while labeling non-text parts."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session_message VALUES ('v2', 'session', 'user', 1, 1, ?)",
                    (json.dumps({"text": "v2 user"}),),
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, 'session', ?, ?)",
                    [
                        ("later", 20, json.dumps({"role": "assistant"})),
                        ("first", 10, json.dumps({"role": "user"})),
                    ],
                )
                connection.executemany(
                    "INSERT INTO part VALUES (?, ?, 'session', ?, ?)",
                    [
                        ("p2", "first", 1, json.dumps({"type": "tool", "tool": "bash", "state": {"input": {"secret": 1}}})),
                        ("p1", "first", 2, json.dumps({"type": "text", "text": "legacy user"})),
                        ("p3", "later", 1, json.dumps({"type": "text", "text": "legacy answer"})),
                    ],
                )
            code, stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "session"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertLess(stdout.index("v2_transcript:"), stdout.index("legacy_transcript:"))
            self.assertLess(stdout.index("legacy user"), stdout.index("legacy answer"))
            self.assertLess(stdout.index("legacy user"), stdout.index("tool: bash"))
            self.assertIn("tool: bash", stdout)
            self.assertNotIn("secret", stdout)

    def test_present_location_tables_require_supported_columns(self) -> None:
        """Refuse present-but-unsupported location tables instead of omitting their data."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE project (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO project VALUES ('project')")
                connection.execute("CREATE TABLE project_directory (project_id TEXT)")

            code, _stdout, stderr = self._run(
                ["list-projects", "--db", str(database)]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: inspection schema is incomplete\n")

    def test_foreign_key_failures_are_refused_and_control_bytes_are_escaped(self) -> None:
        """Validate database relationships and prevent stored text from controlling a terminal."""
        with _temporary_directory() as root:
            malformed = root / "malformed.db"
            with sqlite3.connect(malformed) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("CREATE TABLE project (id TEXT PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, "
                    "FOREIGN KEY(project_id) REFERENCES project(id))"
                )
                connection.execute("INSERT INTO session VALUES ('session', 'missing', '/work')")

            code, _stdout, stderr = self._run(
                ["list-projects", "--db", str(malformed)]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: database foreign key check failed\n")

            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                    ("project", "/work/project", "unsafe\x1b[31m\tname", "[]", 1),
                )
            code, stdout, stderr = self._run(["list-projects", "--db", str(database)])
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn("\x1b", stdout)
            self.assertNotIn("\t", stdout)
            self.assertIn(r"unsafe\x1b[31m\tname", stdout)

    def test_show_session_refuses_missing_promoted_projection(self) -> None:
        """Do not hide a promoted prompt whose corresponding user row is absent."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session_input VALUES ('missing', 'session', ?, 7, 1)",
                    (json.dumps({"text": "must remain visible"}),),
                )

            code, _stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "session"]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: inspection data is malformed\n")

    def test_show_session_refuses_cross_session_legacy_part(self) -> None:
        """Validate both redundant part ownership columns before rendering a message."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session VALUES ('other', 'project', '/work/project', 'Other', 2)"
                )
                connection.execute(
                    "INSERT INTO message VALUES ('message', 'session', 1, ?)",
                    (json.dumps({"role": "user"}),),
                )
                connection.execute(
                    "INSERT INTO part VALUES ('part', 'message', 'other', 1, ?)",
                    (json.dumps({"type": "text", "text": "must not disappear"}),),
                )

            code, _stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "session"]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: inspection data is malformed\n")

    def test_show_session_escapes_surrogates_on_utf8_stdout(self) -> None:
        """Render malformed Unicode safely through the real strict stdout encoding path."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session_input VALUES ('pending', 'session', ?, NULL, 1)",
                    (json.dumps({"text": "\ud800"}),),
                )

            buffer = io.BytesIO()
            stdout = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
            stderr = io.StringIO()
            with patch.object(sys, "stdout", stdout), redirect_stderr(stderr):
                code = cli.main(
                    ["show-session", "--db", str(database), "--session-id", "session"]
                )
                stdout.flush()
            rendered = buffer.getvalue().decode("utf-8")
            stdout.detach()

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn(r"pending_input: \ud800", rendered)

    def test_malformed_schema_or_json_refuses_and_read_only_access_preserves_bytes(self) -> None:
        """Reject unsupported transcript shapes without mutating the selected database."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "project", "session")
                connection.execute(
                    "INSERT INTO session_message VALUES ('bad', 'session', 'user', 1, 1, '{')"
                )
            before = database.read_bytes()
            code, _stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "session"]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: inspection data is malformed\n")
            self.assertEqual(database.read_bytes(), before)
            malformed = root / "malformed.db"
            with sqlite3.connect(malformed) as connection:
                connection.execute("CREATE TABLE project (name TEXT)")
            with self.assertRaisesRegex(InspectionError, "schema"):
                inspect_database(malformed, "list-projects")

    def test_list_sessions_and_logical_estimates_are_metadata_only_and_read_only(self) -> None:
        """List newest sessions and expose opt-in estimates without transcript content or writes."""
        with _temporary_directory() as root:
            database = root / "opencode.db"
            self._create_database(database)
            with sqlite3.connect(database) as connection:
                self._insert_project_session(connection, "a", "old")
                connection.execute("INSERT INTO session VALUES ('new', 'a', '/work/project', 'New', 2)")
                connection.execute("INSERT INTO project VALUES ('b', '/work/b', 'B', '[]', 1)")
                connection.execute("INSERT INTO session VALUES ('other', 'b', '/work/b', 'Other', 3)")
                connection.execute(
                    "INSERT INTO message VALUES ('secret', 'new', 1, ?)",
                    (json.dumps({"role": "user", "secret": "do-not-show"}),),
                )
            with sqlite3.connect(database) as connection:
                sizes = session_logical_sizes(connection, project_id="a")
            before = database.read_bytes()
            code, stdout, stderr = self._run(
                ["list-sessions", "--db", str(database), "--project-id", "a", "--estimate-session-size"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                stdout,
                "\n".join(
                    (
                        f"database: {database.resolve()}",
                        "",
                        "session:",
                        "session_id: new",
                        "project_id: a",
                        "directory: /work/project",
                        "title: New",
                        "time_updated: 2",
                        f"estimated_session_logical_bytes: {sizes['new']}",
                        "",
                        "session:",
                        "session_id: old",
                        "project_id: a",
                        "directory: /work/project",
                        "title: Session",
                        "time_updated: 1",
                        f"estimated_session_logical_bytes: {sizes['old']}",
                        "",
                    )
                ),
            )
            self.assertNotIn("counts:", stdout)
            self.assertNotIn("do-not-show", stdout)
            self.assertEqual(database.read_bytes(), before)

            code, stdout, stderr = self._run(
                ["list-projects", "--db", str(database), "--estimate-project-size"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("estimated_project_logical_bytes:", stdout)

            code, stdout, stderr = self._run(
                ["show-project", "--db", str(database), "--project-id", "a", "--estimate-project-size"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("estimated_project_logical_bytes:", stdout)
            self.assertNotIn("do-not-show", stdout)

            code, stdout, stderr = self._run(
                ["show-session", "--db", str(database), "--session-id", "new", "--estimate-session-size"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("estimated_session_logical_bytes:", stdout)
            self.assertEqual(database.read_bytes(), before)

            code, _stdout, stderr = self._run(
                ["list-sessions", "--db", str(database), "--project-id", "missing"]
            )
            self.assertEqual(code, EXIT_PRECONDITION_REFUSED)
            self.assertEqual(stderr, "opencode-db: project ID was not found\n")

    @staticmethod
    def _create_database(database: Path) -> None:
        """Create a compact supported schema without production-only side effects."""
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE project (id TEXT, worktree TEXT, name TEXT, sandboxes TEXT, time_created INTEGER);
                CREATE TABLE project_directory (project_id TEXT, directory TEXT, type TEXT, strategy TEXT);
                CREATE TABLE workspace (id TEXT, project_id TEXT, directory TEXT);
                CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT, time_updated INTEGER);
                CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);
                CREATE TABLE session_input (id TEXT, session_id TEXT, prompt TEXT, promoted_seq INTEGER, admitted_seq INTEGER);
                CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
                CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
                CREATE TABLE session_context_epoch (session_id TEXT, baseline TEXT, snapshot TEXT, baseline_seq INTEGER);
                CREATE TABLE todo (session_id TEXT, data TEXT);
                CREATE TABLE session_share (session_id TEXT, data TEXT);
                CREATE TABLE event_sequence (aggregate_id TEXT, data TEXT);
                CREATE TABLE event (aggregate_id TEXT, data TEXT);
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _insert_project_session(
        connection: sqlite3.Connection, project_id: str, session_id: str
    ) -> None:
        """Insert one minimally complete project and session fixture pair."""
        connection.execute(
            "INSERT INTO project VALUES (?, ?, ?, ?, ?)",
            (project_id, "/work/project", "Project", "[]", 1),
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            (session_id, project_id, "/work/project", "Session", 1),
        )

    @staticmethod
    def _run(
        arguments: list[str], environment: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        """Run the CLI under an isolated environment and capture both output streams."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with patch.dict(os.environ, environment or {}, clear=environment is not None):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
