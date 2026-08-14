"""Reusable disposable fixtures and assertions for sibling move tests."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile


_TEST_ROOT = Path("/tmp/opencode-db-v1")


@contextmanager
def temporary_directory():
    """Yield one private disposable directory beneath the approved test root."""
    _TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_TEST_ROOT, 0o700)
    if stat.S_IMODE(_TEST_ROOT.stat().st_mode) != 0o700:
        raise RuntimeError("test root must be private")
    with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory:
        yield Path(directory)


def move_fixture(root: Path, *, duplicate: bool = False) -> tuple[Path, Path, Path]:
    """Create copied flat Git families and one minimal supported SQLite database."""
    source_parent = root / "source"
    target_parent = root / "target"
    source_parent.mkdir()
    target_parent.mkdir()
    names = ("main", "sandbox", "directory", "session", "workspace")
    for name in names:
        checkout = source_parent / name
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        (checkout / "marker").write_text(name, encoding="ascii")
        subprocess.run(["git", "-C", str(checkout), "add", "marker"], check=True)
        subprocess.run(
            [
                "git", "-C", str(checkout), "-c", "maintenance.auto=false", "-c",
                "gc.auto=0", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "initial",
            ],
            check=True,
        )
        shutil.copytree(
            checkout,
            target_parent / name,
            ignore=shutil.ignore_patterns("*.lock"),
        )
    database = root / "opencode.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE project (
                id TEXT PRIMARY KEY,
                worktree TEXT NOT NULL,
                sandboxes TEXT NOT NULL
            );
            CREATE TABLE project_directory (
                project_id TEXT NOT NULL,
                directory TEXT NOT NULL,
                type TEXT,
                strategy TEXT,
                time_created INTEGER NOT NULL,
                PRIMARY KEY (project_id, directory),
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
            );
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                directory TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
            );
            CREATE TABLE workspace (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                directory TEXT,
                FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
            );
            """
        )
        main = source_parent / "main"
        connection.execute(
            "INSERT INTO project VALUES (?, ?, ?)",
            ("project", str(main), json.dumps([str(source_parent / "sandbox")])),
        )
        connection.execute(
            "INSERT INTO project_directory VALUES (?, ?, ?, ?, ?)",
            ("project", str(main if duplicate else source_parent / "directory"), "root", None, 1),
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?)",
            ("session", "project", str(main if duplicate else source_parent / "session")),
        )
        connection.execute(
            "INSERT INTO workspace VALUES (?, ?, ?)",
            ("workspace", "project", str(main if duplicate else source_parent / "workspace")),
        )
        connection.execute("INSERT INTO workspace VALUES (?, ?, NULL)", ("null", "project"))
        connection.commit()
    finally:
        connection.close()
    return source_parent, target_parent, database


def location_rows(database: Path) -> tuple[tuple[object, ...], ...]:
    """Return stable location rows to prove planning did not write them."""
    connection = sqlite3.connect(database)
    try:
        return tuple(
            connection.execute(
                "SELECT worktree, sandboxes FROM project UNION ALL "
                "SELECT directory, project_id FROM project_directory UNION ALL "
                "SELECT directory, project_id FROM session UNION ALL "
                "SELECT directory, project_id FROM workspace ORDER BY 1, 2"
            ).fetchall()
        )
    finally:
        connection.close()


def structured_rows(database: Path) -> tuple[tuple[object, ...], ...]:
    """Return all known structured rows and identities in stable table-local order."""
    connection = sqlite3.connect(database)
    try:
        return tuple(
            ("project", *row)
            for row in connection.execute("SELECT id, worktree, sandboxes FROM project ORDER BY id")
        ) + tuple(
            ("project_directory", *row)
            for row in connection.execute(
                "SELECT project_id, directory, type, strategy, time_created "
                "FROM project_directory ORDER BY project_id, directory"
            )
        ) + tuple(
            ("session", *row)
            for row in connection.execute("SELECT id, project_id, directory FROM session ORDER BY id")
        ) + tuple(
            ("workspace", *row)
            for row in connection.execute("SELECT id, project_id, directory FROM workspace ORDER BY id")
        )
    finally:
        connection.close()


def tree_snapshot(directory: Path) -> tuple[tuple[str, bytes], ...]:
    """Return a stable fixture content snapshot to prove planning made no file writes."""
    return tuple(
        sorted(
            (str(path.relative_to(directory)), path.read_bytes())
            for path in directory.rglob("*")
            if path.is_file()
        )
    )


def fake_git(root: Path) -> Path:
    """Create a disposable local Git stand-in for subprocess-boundary tests."""
    script = root / "git"
    script.write_text(
        "#!/usr/bin/python3\n"
        "import os, sys, time\n"
        "mode = os.environ['MOVE_FAKE_GIT']\n"
        "if mode == 'nonzero': sys.exit(2)\n"
        "if mode == 'malformed': sys.stdout.write('broken'); sys.exit(0)\n"
        "if mode == 'large': sys.stdout.buffer.write(b'x' * 65537); sys.stdout.flush(); time.sleep(10)\n"
        "if mode == 'sleep': time.sleep(10)\n"
        "if mode == 'pid-sleep': open(os.environ['MOVE_FAKE_GIT_PID'], 'w').write(str(os.getpid())); time.sleep(10)\n",
        encoding="ascii",
    )
    script.chmod(0o700)
    return script
