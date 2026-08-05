"""Generate disposable SQLite WAL states for cleanup tests.

The helpers create database state only beneath caller-provided temporary
directories. They never access an operator database or retained artifact.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def create_abrupt_wal_database(path: Path, *, committed: bool) -> None:
    """Create a WAL database whose writer exits without closing.

    Parameters: ``path`` is a database path in a private temporary directory
    and ``committed`` selects whether the inserted marker transaction commits.
    Returns ``None`` after the writer process exits. Raises
    :class:`subprocess.CalledProcessError` if SQLite fixture setup fails. The
    helper writes only the requested disposable fixture files.
    """
    script = """
import os
import sqlite3
import sys

database = sys.argv[1]
committed = sys.argv[2] == '1'
connection = sqlite3.connect(database)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE entries (value TEXT)')
connection.commit()
connection.execute('BEGIN IMMEDIATE')
connection.execute('INSERT INTO entries VALUES (?)', ('committed-value',))
if committed:
    connection.commit()
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(path), "1" if committed else "0"],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
