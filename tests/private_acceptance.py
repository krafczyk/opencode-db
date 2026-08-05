"""Run a private, opt-in cleanup classification on an explicit copied fixture.

This entry point is not collected by the default suite. It copies only an
operator-supplied database set into an explicitly named new work directory,
checks the supplied source bytes before and after, and prints one bounded result
without rows, diagnostics, paths, or OpenCode output.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli

_SUFFIXES = ("", "-wal", "-shm", "-journal")
_STATUSES = frozenset({"complete", "uncertain", "invalid", "source_changed"})
_CANDIDATES = frozenset({"present", "absent"})
_IO_CHUNK_SIZE = 1024 * 1024
_DEADLINE_SECONDS = 1_800


def main(arguments: list[str] | None = None) -> int:
    """Run one private copied-fixture classification with bounded public output.

    Parameters: ``arguments`` optionally replaces command-line arguments.
    Returns zero only when the expected status, candidate presence, and source
    byte identity all match. Every outcome emits one fixed-schema JSON object;
    no source paths, rows, diagnostics, or arbitrary database content are sent
    to either output stream. This function never locates workspace or live paths
    implicitly and never starts OpenCode.
    """
    parsed = _parse(list(sys.argv[1:] if arguments is None else arguments))
    if parsed is None:
        return _emit("invalid_arguments", False, False, False)
    database, work_directory, scratch_directory, expected_status, expected_candidate = (
        parsed
    )
    try:
        deadline = time.monotonic() + _DEADLINE_SECONDS
        before = _manifest(database, deadline)
        if (
            work_directory.exists()
            or not database.is_file()
            or not scratch_directory.is_absolute()
        ):
            return _emit("precondition_refused", False, False, False)
        work_directory.mkdir(mode=0o700, parents=True)
        copied = work_directory / "fixture.db"
        for suffix in _SUFFIXES:
            source = Path(f"{database}{suffix}")
            if source.exists():
                _copy_file(source, Path(f"{copied}{suffix}"), deadline)
        remaining = math.floor(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError("acceptance deadline expired")
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            cli.main(
                [
                    "cleanup",
                    "preview",
                    "--database",
                    str(copied),
                    "--scratch-dir",
                    str(scratch_directory),
                    "--deadline-seconds",
                    str(remaining),
                    "--json",
                ]
            )
        result = json.loads(stdout.getvalue())
        observed_status = result.get("status")
        candidate_present = isinstance(result.get("candidate_id"), str)
        source_unchanged = before == _manifest(database, deadline)
        success = (
            source_unchanged
            and observed_status == expected_status
            and candidate_present == (expected_candidate == "present")
        )
        return _emit(
            observed_status
            if isinstance(observed_status, str)
            else "operational_failure",
            source_unchanged,
            candidate_present,
            success,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return _emit("operational_failure", False, False, False)


def _parse(
    arguments: list[str],
) -> tuple[Path, Path, Path, str, str] | None:
    """Parse exactly the explicit private acceptance arguments without output."""
    values: dict[str, str] = {}
    names = {
        "--database",
        "--work-directory",
        "--scratch-dir",
        "--expected-status",
        "--expect-candidate",
    }
    if len(arguments) != 10:
        return None
    for index in range(0, len(arguments), 2):
        name, value = arguments[index : index + 2]
        if name not in names or name in values:
            return None
        values[name] = value
    if set(values) != names:
        return None
    database = Path(values["--database"])
    work_directory = Path(values["--work-directory"])
    scratch_directory = Path(values["--scratch-dir"])
    if not all(
        path.is_absolute() for path in (database, work_directory, scratch_directory)
    ):
        return None
    if values["--expected-status"] not in _STATUSES:
        return None
    if values["--expect-candidate"] not in _CANDIDATES:
        return None
    return (
        database,
        work_directory,
        scratch_directory,
        values["--expected-status"],
        values["--expect-candidate"],
    )


def _manifest(
    database: Path, deadline: float
) -> tuple[tuple[str, int, str] | None, ...]:
    """Return streamed byte identities for exactly the standard file set."""
    identities: list[tuple[str, int, str] | None] = []
    for suffix in _SUFFIXES:
        path = Path(f"{database}{suffix}")
        if not path.exists():
            identities.append(None)
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(_IO_CHUNK_SIZE):
                _check_deadline(deadline)
                digest.update(chunk)
                size += len(chunk)
        identities.append((suffix, size, digest.hexdigest()))
    return tuple(identities)


def _copy_file(source: Path, destination: Path, deadline: float) -> None:
    """Copy one fixture file with bounded memory and private destination mode."""
    with source.open("rb") as reader:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as writer:
            while chunk := reader.read(_IO_CHUNK_SIZE):
                _check_deadline(deadline)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())


def _check_deadline(deadline: float) -> None:
    """Refuse additional acceptance I/O after the fixed private-gate deadline."""
    if time.monotonic() >= deadline:
        raise TimeoutError("acceptance deadline expired")


def _emit(
    observed_status: str, source_unchanged: bool, candidate_present: bool, success: bool
) -> int:
    """Emit one content-free acceptance result and return its matching exit code."""
    payload = {
        "schema_version": 1,
        "observed_status": observed_status[:64],
        "source_unchanged": source_unchanged,
        "candidate_present": candidate_present,
        "outcome": "matched" if success else "mismatched",
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
