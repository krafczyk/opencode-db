"""Select database paths and validate source and scratch preconditions.

This module never opens SQLite, starts OpenCode, or creates a replacement
database. It selects documented environment defaults, resolves operator input,
and checks storage evidence needed before the artifact layer writes private
source snapshots.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


ADMITTED_SCRATCH_FILESYSTEMS = frozenset(
    {"tmpfs", "ramfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs"}
)
"""Linux filesystem types admitted for writable SQLite scratch."""


class TargetError(RuntimeError):
    """Report one safe target or preflight refusal without exposing OS errors.

    Parameters: ``code`` is a stable diagnostic code. The exception is raised
    before SQLite is opened or source content is changed. ``diagnostic`` is an
    optional trusted tool-generated storage message, never raw exception text.
    """

    def __init__(self, code: str, diagnostic: str | None = None) -> None:
        """Store a stable code and optional tool-generated storage diagnostic."""
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def select_default_database(environment: Mapping[str, str] | None = None) -> str:
    """Select the documented OpenCode database path from environment bases only.

    Parameters: ``environment`` optionally supplies XDG/HOME values for
    deterministic callers; omitted uses :data:`os.environ`. Returns
    ``$XDG_DATA_HOME/opencode/opencode.db`` when ``XDG_DATA_HOME`` is absolute,
    otherwise ``$HOME/.local/share/opencode/opencode.db`` when ``HOME`` is
    absolute. Raises :class:`TargetError` with ``default_database_unavailable``
    when neither base is absolute. The function never stats, opens, resolves, or
    creates the selected path.
    """
    values = os.environ if environment is None else environment
    xdg_data_home = values.get("XDG_DATA_HOME")
    if isinstance(xdg_data_home, str) and os.path.isabs(xdg_data_home):
        if "\x00" in xdg_data_home:
            raise TargetError("default_database_unavailable")
        return os.path.join(xdg_data_home, "opencode", "opencode.db")
    home = values.get("HOME")
    if isinstance(home, str) and os.path.isabs(home):
        if "\x00" in home:
            raise TargetError("default_database_unavailable")
        return os.path.join(home, ".local", "share", "opencode", "opencode.db")
    raise TargetError("default_database_unavailable")


@dataclass(frozen=True)
class StorageSpace:
    """Describe available storage returned by one filesystem capacity probe.

    ``available_bytes`` and ``available_inodes`` are nonnegative free capacity
    observations. The type has no side effects and supports deterministic test
    probes.
    """

    available_bytes: int
    available_inodes: int


@dataclass(frozen=True)
class TargetEnvironment:
    """Provide bounded system probes used by target admission.

    ``mountinfo_reader`` returns Linux mountinfo text and ``storage_probe``
    returns available capacity for an existing path. Production defaults read
    ``/proc/self/mountinfo`` and use :func:`os.statvfs`; tests may inject
    deterministic probes. Neither callback may open SQLite or source files.
    """

    mountinfo_reader: Callable[[], str]
    storage_probe: Callable[[Path], StorageSpace]


@dataclass(frozen=True)
class ResolvedTarget:
    """Identify one canonical regular-file target and its private control root.

    ``path`` is the canonical existing source path, ``target_id`` is a
    path-derived opaque identifier, and ``control_dir`` is the selected private
    artifact directory (adjacent by default) created only after preflight. This
    value does not create paths or access SQLite.
    """

    path: Path
    target_id: str
    control_dir: Path


def default_environment() -> TargetEnvironment:
    """Return the production Linux mount and storage probes.

    Returns a :class:`TargetEnvironment` that reads the procfs mount table and
    filesystem capacity. A missing or unreadable procfs mount table is handled
    as a refusal by :func:`admit_scratch`; this function has no file writes.
    """
    return TargetEnvironment(
        mountinfo_reader=lambda: Path("/proc/self/mountinfo").read_text(
            encoding="utf-8"
        ),
        storage_probe=_storage_space,
    )


def resolve_target(database: str, *, work_dir: str | None = None) -> ResolvedTarget:
    """Resolve one explicit database argument to a canonical regular file.

    Parameters: ``database`` is the operator-supplied absolute path; ``work_dir``
    optionally selects the retained store's absolute parent. Returns a
    :class:`ResolvedTarget` only for an existing regular file. Raises
    :class:`TargetError` with ``target_invalid`` for relative, in-memory,
    missing, dangling, directory, or non-regular paths, or for an invalid private
    work root. It never creates files,
    invokes OpenCode, opens SQLite, or changes source permissions.
    """
    if not database or database == ":memory:" or "\x00" in database:
        raise TargetError("target_invalid")
    supplied = Path(database)
    if not supplied.is_absolute():
        raise TargetError("target_invalid")
    try:
        canonical = supplied.resolve(strict=True)
        metadata = canonical.stat()
    except OSError as error:
        raise TargetError("target_invalid") from error
    if not os.path.isfile(canonical) or metadata.st_size < 0:
        raise TargetError("target_invalid")
    target_id = hashlib.sha256(os.fsencode(str(canonical))).hexdigest()[:32]
    return ResolvedTarget(
        path=canonical,
        target_id=target_id,
        control_dir=_control_dir(canonical, target_id, work_dir),
    )


def resolve_recorded_target(
    database: str, *, work_dir: str | None = None
) -> ResolvedTarget:
    """Resolve an absolute target identity without requiring its main file.

    Parameters: ``database`` is the exact absolute path previously used for a
    target catalog; ``work_dir`` selects the same store used at capture. Returns
    its canonical lexical identity and selected control
    directory so status and recovery can work while installation has moved the
    main file. Raises :class:`TargetError` for relative, in-memory, or malformed
    paths or an invalid private work root. It creates nothing and never opens
    SQLite or source files.
    """
    if not database or database == ":memory:" or "\x00" in database:
        raise TargetError("target_invalid")
    supplied = Path(database)
    if not supplied.is_absolute():
        raise TargetError("target_invalid")
    canonical = supplied.resolve(strict=False)
    target_id = hashlib.sha256(os.fsencode(str(canonical))).hexdigest()[:32]
    return ResolvedTarget(
        path=canonical,
        target_id=target_id,
        control_dir=_control_dir(canonical, target_id, work_dir),
    )


def _control_dir(database: Path, target_id: str, work_dir: str | None) -> Path:
    """Select an explicit store without searching other roots or creating it."""
    if work_dir is None:
        return database.parent / ".opencode-db" / target_id
    root = validate_work_directory(work_dir)
    return root / ".opencode-db" / target_id


def validate_work_directory(work_dir: str) -> Path:
    """Return an existing absolute mode-0700 work root without modifying it.

    ``work_dir`` is operator input. Raises :class:`TargetError` for a malformed,
    unavailable, symlink, non-directory, or non-private root. Performs no writes.
    """
    if "\x00" in work_dir or not Path(work_dir).is_absolute():
        raise TargetError("work_invalid")
    supplied = Path(work_dir)
    try:
        if supplied.is_symlink():
            raise TargetError("work_not_private")
        root = supplied.resolve(strict=True)
        if not root.is_dir() or root.stat().st_mode & 0o777 != 0o700:
            raise TargetError("work_not_private")
    except OSError as error:
        raise TargetError("work_not_private") from error
    return root


@contextmanager
def sqlite_temporary_directory(
    directory: str | None, *, validate: bool = True, update_tmpdir: bool = True
) -> Iterator[None]:
    """Select SQLite temporary storage for a CLI invocation and restore the env.

    ``directory`` is an explicit existing private local root, or ``None`` to
    preserve the caller environment. Raises :class:`TargetError` before command
    execution when admission fails. Sets SQLITE_TMPDIR and TMPDIR temporarily;
    SQLite reads these on its first temporary-file allocation in the process.
    ``validate=False`` lets cleanup perform its own scratch admission and
    creation before opening SQLite. ``update_tmpdir=False`` preserves cleanup's
    TMPDIR-based scratch default while setting SQLITE_TMPDIR. Does not relocate
    database journals, WAL, or final installation staging. Yields ``None`` while
    the environment is selected and restores it even when command execution fails.
    """
    if directory is None:
        yield
        return
    root = Path(directory)
    if validate:
        root = validate_work_directory(directory)
        admit_scratch(root, 0, 0)
    keys = ("SQLITE_TMPDIR", "TMPDIR") if update_tmpdir else ("SQLITE_TMPDIR",)
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update({key: str(root) for key in previous})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def require_capacity(
    path: Path,
    required_bytes: int,
    required_inodes: int,
    *,
    code: str = "storage_capacity",
    environment: TargetEnvironment | None = None,
) -> None:
    """Require free bytes and inodes at ``path`` before writing.

    Returns ``None`` on success; raises :class:`TargetError` with a bounded,
    tool-generated path, deficient resource, and required/available counts on
    refusal. ``environment`` injects probes for tests. Creates no files.
    """
    try:
        available = storage_space(path, environment)
    except TargetError as error:
        raise TargetError(
            code, f"Cannot measure storage capacity at {_display_path(path)}."
        ) from error
    lacking = []
    if available.available_bytes < required_bytes:
        lacking.append("disk space")
    if available.available_inodes < required_inodes:
        lacking.append("inodes")
    if lacking:
        raise TargetError(
            code,
            f"Insufficient {' and '.join(lacking)} at {_display_path(path)}; "
            f"bytes required={required_bytes} available={available.available_bytes}; "
            f"inodes required={required_inodes} available={available.available_inodes}.",
        )


def storage_failure(error: BaseException, *paths: Path) -> str | None:
    """Describe ENOSPC, quota, or SQLite FULL errors without exposing raw text.

    ``error`` may wrap the OS/SQLite failure through exception causes. ``paths``
    are known working locations, not paths parsed from exception messages.
    Returns a bounded diagnostic with current capacity, or ``None`` for other
    failures. ENOSPC cannot distinguish quotas, bytes, and inodes on its own;
    positive current counts are reported as inconclusive. Only reads statvfs.
    """
    current: BaseException | None = error
    seen: set[int] = set()
    quota = False
    full = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TargetError) and current.diagnostic:
            return current.diagnostic
        if isinstance(current, OSError):
            quota |= current.errno == errno.EDQUOT
            full |= current.errno in (errno.ENOSPC, errno.EDQUOT)
        sqlite_code = getattr(current, "sqlite_errorcode", 0)
        full |= isinstance(sqlite_code, int) and sqlite_code & 255 == sqlite3.SQLITE_FULL
        current = current.__cause__
    if not full:
        return None
    observations = []
    for path in list(dict.fromkeys(paths))[:3]:
        try:
            available = storage_space(path)
            lacking = []
            if available.available_bytes == 0:
                lacking.append("disk space")
            if available.available_inodes == 0:
                lacking.append("inodes")
            resource = " and ".join(lacking) or "resource undetermined (quota or temporary exhaustion possible)"
            observations.append(
                f"{_display_path(path)}: {resource}; bytes available={available.available_bytes}; "
                f"inodes available={available.available_inodes}"
            )
        except TargetError:
            observations.append(f"{_display_path(path)}: capacity unavailable")
    summary = "Storage quota exceeded" if quota else "Storage exhausted"
    return summary + ". " + "; ".join(observations)


def _display_path(path: Path) -> str:
    """Quote a bounded operator-selected path without terminal control bytes."""
    value = str(path)
    return json.dumps(value if len(value) <= 160 else value[:157] + "...", ensure_ascii=True)


def admit_scratch(
    scratch_dir: Path,
    required_bytes: int,
    required_inodes: int,
    environment: TargetEnvironment | None = None,
) -> Path:
    """Admit one private writable scratch root only on a known local mount.

    Parameters: ``scratch_dir`` is an absolute workspace root, capacity values
    are required free bytes/inodes, and ``environment`` optionally supplies
    deterministic probes. Returns the canonical scratch root. Raises
    :class:`TargetError` for nonabsolute paths, remote or unclassified mounts,
    non-private existing roots, unavailable procfs data, or insufficient
    capacity. The function creates nothing and never opens SQLite.
    """
    if not scratch_dir.is_absolute() or required_bytes < 0 or required_inodes < 0:
        raise TargetError("scratch_invalid")
    canonical = scratch_dir.resolve()
    existing = _nearest_existing_parent(canonical)
    probes = environment or default_environment()
    try:
        mount_type = _mount_type(existing, probes.mountinfo_reader())
    except OSError as error:
        raise TargetError("scratch_not_local") from error
    if mount_type not in ADMITTED_SCRATCH_FILESYSTEMS:
        raise TargetError("scratch_not_local")
    if canonical.exists() and (
        not canonical.is_dir() or canonical.is_symlink() or _mode(canonical) != 0o700
    ):
        raise TargetError("scratch_not_private")
    require_capacity(
        canonical, required_bytes, required_inodes,
        code="scratch_capacity", environment=probes,
    )
    return canonical


def storage_space(
    path: Path, environment: TargetEnvironment | None = None
) -> StorageSpace:
    """Return available bytes and inodes for an existing ancestor of ``path``.

    Parameters: ``path`` may not exist yet and ``environment`` may provide a
    deterministic capacity probe. Returns :class:`StorageSpace`. Raises
    :class:`TargetError` when capacity cannot be observed. The function does not
    write files or open SQLite.
    """
    try:
        return (environment or default_environment()).storage_probe(
            _nearest_existing_parent(path)
        )
    except OSError as error:
        raise TargetError("storage_capacity") from error


def ensure_private_directory(path: Path) -> None:
    """Create or verify a tool-owned directory with exact ``0700`` permissions.

    Parameters: ``path`` is a directory beneath the target control root. Returns
    ``None`` after creating it with private permissions. Raises
    :class:`TargetError` if a symlink, non-directory, or non-private existing
    path prevents safe use. It changes only artifact directories, never source
    database paths or permissions.
    """
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or _mode(path) != 0o700:
            raise TargetError("artifact_not_private")
        return
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except OSError as error:
        raise TargetError("artifact_not_private") from error


def _storage_space(path: Path) -> StorageSpace:
    """Read free capacity from ``path`` with no mutation or SQLite access."""
    status = os.statvfs(path)
    return StorageSpace(
        available_bytes=status.f_bavail * status.f_frsize,
        available_inodes=status.f_favail,
    )


def _nearest_existing_parent(path: Path) -> Path:
    """Return the nearest existing ancestor or raise a bounded target error."""
    current = path
    while not current.exists():
        if current == current.parent:
            raise TargetError("scratch_invalid")
        current = current.parent
    return current


def _mount_type(path: Path, mountinfo: str) -> str | None:
    """Return the longest matching Linux mount type from mountinfo text."""
    matches: list[tuple[int, str]] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 1 >= len(fields) or len(fields) < 5:
            continue
        mountpoint = Path(_unescape_mount_field(fields[4]))
        try:
            path.relative_to(mountpoint)
        except ValueError:
            continue
        matches.append((len(str(mountpoint)), fields[separator + 1]))
    if not matches:
        return None
    return max(matches)[1]


def _unescape_mount_field(value: str) -> str:
    """Decode the octal path escapes used by Linux mountinfo."""
    for octal in ("040", "011", "012", "134"):
        value = value.replace(f"\\{octal}", chr(int(octal, 8)))
    return value


def _mode(path: Path) -> int:
    """Return only permission bits for a tool-owned path without changing it."""
    return path.stat().st_mode & 0o777
