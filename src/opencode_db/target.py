"""Select database paths and validate source and scratch preconditions.

This module never opens SQLite, starts OpenCode, or creates a replacement
database. It selects documented environment defaults, resolves operator input,
and checks storage evidence needed before the artifact layer writes private
source snapshots.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


ADMITTED_SCRATCH_FILESYSTEMS = frozenset(
    {"tmpfs", "ramfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs"}
)
"""Linux filesystem types admitted for writable SQLite scratch."""


class TargetError(RuntimeError):
    """Report one safe target or preflight refusal without exposing OS errors.

    Parameters: ``code`` is a stable diagnostic code. The exception is raised
    before SQLite is opened or source content is changed; callers render only
    the code's fixed public message.
    """

    def __init__(self, code: str) -> None:
        """Store the stable refusal ``code`` without retaining exception details."""
        super().__init__(code)
        self.code = code


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
    path-derived opaque identifier, and ``control_dir`` is the adjacent private
    artifact directory to be created only after preflight. This value does not
    create paths or access SQLite.
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


def resolve_target(database: str) -> ResolvedTarget:
    """Resolve one explicit database argument to a canonical regular file.

    Parameters: ``database`` is the operator-supplied absolute path. Returns a
    :class:`ResolvedTarget` only for an existing regular file. Raises
    :class:`TargetError` with ``target_invalid`` for relative, in-memory,
    missing, dangling, directory, or non-regular paths. It never creates files,
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
        control_dir=canonical.parent / ".opencode-db" / target_id,
    )


def resolve_recorded_target(database: str) -> ResolvedTarget:
    """Resolve an absolute target identity without requiring its main file.

    Parameters: ``database`` is the exact absolute path previously used for a
    target catalog. Returns its canonical lexical identity and adjacent control
    directory so status and recovery can work while installation has moved the
    main file. Raises :class:`TargetError` for relative, in-memory, or malformed
    paths. It creates nothing and never opens SQLite or source files.
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
        control_dir=canonical.parent / ".opencode-db" / target_id,
    )


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
    try:
        available = probes.storage_probe(existing)
    except OSError as error:
        raise TargetError("scratch_capacity") from error
    if (
        available.available_bytes < required_bytes
        or available.available_inodes < required_inodes
    ):
        raise TargetError("scratch_capacity")
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
