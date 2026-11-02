"""Prune private WAL snapshots and catch them up using accepted activity stamps.

The source is opened read-only and receives no tracking objects. Synchronization
intentionally cannot detect child/metadata edits that leave time_updated equal.
Only explicit cutover replaces the active database; see staged_install.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import struct
import time

from .artifacts import _fsync_directory, _read_canonical_json, _write_json, new_artifact_id
from .prune import PruneError, PruneOperationalError, PruneRequest, ReviewedPrunePlan, _validate_prune_database_health, _vacuum, apply_prune_plan, parse_prune_timeout, plan_prune
from .target import admit_scratch, ensure_private_directory, require_capacity, resolve_recorded_target, resolve_target, validate_work_directory
from .transfer import CHILD_SESSION_TABLES, SESSION_SELECTOR_COLUMNS, SESSION_TABLES, _quote, _validate_session_schema

DEFAULT_STAGE_TIMEOUT = 86_400
"""Default finite budget for each staged phase, including twelve-hour pruning."""
_MAX_SESSIONS = 1_000_000
_MAX_METADATA = 128 * 1024 * 1024
_MAX_GLOBAL = 64 * 1024 * 1024
_ID = re.compile(r"prune-\d{8}T\d{6}Z-[0-9a-f]{24}")
_META_KEYS = {"phase", "schema", "source_digest", "candidate_sha256", "now_ms", "generation", "result", "cursor_sha256", "job_sha256"}
_PHASES = {"snapshot", "planning", "pruning", "pruned", "vacuum", "sealing", "ready", "installed", "rolled_back"}
_MANIFEST_KEYS = {"schema_version", "operation_id", "target", "target_id", "host", "policy", "detection"}
_RESULT_KEYS = {"pruned_sessions", "new_sessions", "updated_sessions", "restored_pruned_sessions", "dependency_sessions_restored", "deleted_source_sessions"}


@dataclass(frozen=True)
class StageRequest:
    """Select an exact staged job and an explicit action without performing I/O.

    ``database``/``work_dir`` select its recorded source/store. ``operation_id``
    is opaque; ``action`` is update/status/rollback/discard. ``yes`` authorizes
    any pending private-copy selection during resumed preparation. ``install`` requests
    operator-arranged stopped-source cutover after updating. Timeout and progress
    control this invocation only. No process or lifecycle coordination occurs.
    """

    database: str
    operation_id: str
    action: str
    timeout_seconds: float = DEFAULT_STAGE_TIMEOUT
    install: bool = False
    progress: bool = False
    work_dir: str | None = None
    yes: bool = False

    @property
    def command(self) -> str:
        """Return the human CLI name without accessing the filesystem."""
        return "prune " + self.action


@dataclass(frozen=True)
class StageJob:
    """Bind a private job directory to its canonical source and immutable policy.

    Values come from validated target/host-bound metadata. ``candidate`` and
    ``state`` derive fixed filenames; no persisted arbitrary output paths are used.
    Construction itself performs no I/O. Use create_job/load_job for admission.
    """

    directory: Path
    target: Path
    target_id: str
    operation_id: str
    policy: dict[str, object]

    @property
    def candidate(self) -> Path:
        """Return the private, non-active SQLite image path."""
        return self.directory / "candidate.sqlite"

    @property
    def state(self) -> Path:
        """Return the private synchronization metadata database path."""
        return self.directory / "state.sqlite"


class Budget:
    """Bound one phase's SQLite and Python work with a finite monotonic deadline.

    ``seconds`` is validated using the existing prune timeout contract. ``check``
    raises a bounded PruneOperationalError on expiry; ``bind`` installs an SQLite
    progress callback. Neither method inspects processes or changes source data.
    """

    def __init__(self, seconds: float) -> None:
        """Capture the validated budget and its monotonic deadline."""
        self.seconds = parse_prune_timeout(seconds)
        self.started = time.monotonic()
        self.deadline = self.started + self.seconds

    def check(self) -> None:
        """Return before expiry or raise a content-free staged timeout."""
        if time.monotonic() >= self.deadline:
            raise PruneOperationalError("staged prune phase timed out", phase="staged")

    def bind(self, connection: sqlite3.Connection) -> None:
        """Install deadline interruption on an already selected SQLite connection."""
        connection.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)

    def digest(self, path: Path) -> str:
        """Hash one selected closed image within the exact fractional-second budget.

        Returns SHA-256 hex; raises PruneOperationalError on expiry and OS errors
        on reading failure. Reads in bounded chunks without modifying the image.
        Callers validate path authority/privacy before invoking this method.
        """
        result = hashlib.sha256()
        self.check()
        with path.open("rb") as reader:
            while block := reader.read(1024 * 1024):
                self.check()
                result.update(block)
        self.check()
        return result.hexdigest()


def create_job(request: PruneRequest) -> StageJob:
    """Allocate an admitted private job for a live source without changing source rows.

    ``request`` supplies the existing retention policy and explicit private local
    work root. Returns a new StageJob; raises PruneError on unsupported selection
    and OS/target errors on allocation. Creates only versioned job metadata and a
    separate rollback-journal state database under --work-dir.
    """
    policy = {name: getattr(request, name) for name in (
        "project_id", "oldest", "keep_newest", "target_size", "estimate_size", "vacuum", "vacuum_only",
    )}
    _validate_policy(policy, request.database)
    if request.work_dir is None:
        raise PruneError("--staged requires an existing private local --work-dir")
    root = validate_work_directory(request.work_dir)
    admit_scratch(root, 0, 8)
    target = resolve_target(request.database, work_dir=str(root))
    wal = Path(str(target.path) + "-wal")
    source_bytes = target.path.stat().st_size + (wal.stat().st_size if wal.is_file() else 0)
    required = max(source_bytes * 2 + 1024 * 1024, 1024 * 1024)
    require_capacity(root, required, 12)
    identifier = new_artifact_id("prune")
    directory = target.control_dir / "staged-prunes" / identifier
    for path in (target.control_dir.parent, target.control_dir, directory.parent, directory):
        ensure_private_directory(path)
    manifest = {
        "schema_version": 1, "operation_id": identifier, "target": str(target.path),
        "target_id": target.target_id, "host": socket.gethostname(), "policy": policy,
        "detection": "session_last_activity_only",
    }
    _write_json(directory / "job.json", manifest)
    _create_file(directory / "state.sqlite")
    with _state_connection(directory / "state.sqlite") as state:
        state.executescript(
            "CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);"
            "CREATE TABLE versions (id TEXT PRIMARY KEY, stamp INTEGER NOT NULL, retained INTEGER NOT NULL);"
            "CREATE TABLE selected (id TEXT PRIMARY KEY);"
            "CREATE TABLE global_events (id TEXT PRIMARY KEY, digest TEXT NOT NULL);"
        )
        _write_meta(state, {
            "phase": "snapshot", "schema": None, "source_digest": None,
            "candidate_sha256": None, "now_ms": None, "generation": 0, "result": {},
            "job_sha256": hashlib.sha256(_read_canonical_json(directory / "job.json")[1]).hexdigest(),
            "cursor_sha256": None,
        })
        state.commit()
    _fsync_directory(directory)
    return StageJob(directory, target.path, target.target_id, identifier, policy)


def load_job(database: str, work_dir: str | None, operation_id: str) -> StageJob:
    """Resolve one exact private staged job, including during interrupted cutover.

    Parameters select source, store and opaque ID. Returns StageJob without
    opening the source; raises PruneError for malformed/future/foreign metadata,
    host mismatch or unsafe paths. Reads only bounded private metadata.
    """
    if work_dir is None or _ID.fullmatch(operation_id) is None:
        raise PruneError("staged operations require --work-dir and an exact prune operation ID")
    root = validate_work_directory(work_dir)
    admit_scratch(root, 0, 0)
    if "\x00" in database or not Path(database).is_absolute():
        raise PruneError("staged source must be an absolute filesystem path")
    try:
        canonical = Path(database).resolve()
    except (OSError, RuntimeError) as error:
        raise PruneError("recorded staged source path cannot be resolved") from error
    target = resolve_recorded_target(str(canonical), work_dir=str(root))
    directory = target.control_dir / "staged-prunes" / operation_id
    _private(directory, 0o700)
    if (directory / "job.json").stat().st_size > 65536:
        raise PruneError("staged job metadata exceeds supported bounds")
    value, _ = _read_canonical_json(directory / "job.json")
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PruneError("staged job schema is unsupported")
    if (value["operation_id"], value["target"], value["target_id"], value["host"], value["detection"]) != (
        operation_id, str(target.path), target.target_id, socket.gethostname(), "session_last_activity_only",
    ) or not isinstance(value["policy"], dict):
        raise PruneError("staged job target or host does not match")
    policy = value["policy"]
    if set(policy) != {"project_id", "oldest", "keep_newest", "target_size", "estimate_size", "vacuum", "vacuum_only"}:
        raise PruneError("staged policy is malformed")
    job = StageJob(directory, target.path, target.target_id, operation_id, policy)
    # Reuse the closed native request validator before allowing any job mutation.
    _validate_policy(policy, str(job.target))
    _private(job.state, 0o600)
    return job


@contextmanager
def job_lock(job: StageJob) -> Iterator[None]:
    """Exclusively hold a job's stable private lock; process death releases it.

    Yields None. Raises PruneError when busy or unsafe, and OS errors on lock
    creation. The empty lock file persists until explicit discard.
    """
    lock = job.directory / ".lock"
    if lock.is_symlink():
        raise PruneError("staged lock is unsafe")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PruneError("staged operation is already running") from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def source_lock(target: Path, *, exclusive: bool) -> Iterator[None]:
    """Coordinate cooperative staged readers/cutovers for one canonical source.

    ``target`` is the explicitly selected source. Shared locks protect online
    snapshots/updates; exclusive locks serialize cutover/rollback across jobs.
    Yields None or raises PruneError when busy. Creates only adjacent private
    control directories and an empty stable lock, never source SQLite objects.
    Native OpenCode does not participate; shutdown remains the operator's duty.
    """
    control = resolve_recorded_target(str(target)).control_dir
    for path in (control.parent, control):
        ensure_private_directory(path)
    path = control / ".staged-cutover.lock"
    if path.is_symlink():
        raise PruneError("staged source lock is unsafe")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PruneError("another staged source snapshot/update/cutover is running") from error
        yield
    finally:
        os.close(descriptor)


def prepare_job(job: StageJob, timeout_seconds: float, *, authorize: Callable[[ReviewedPrunePlan], None], progress: Callable[[str], None]) -> dict[str, object]:
    """Build/resume a private staged prune without writable-opening the source.

    Caller holds job_lock. ``authorize`` receives the native reviewed preview
    before deletion; ``progress`` receives fixed phase names. Returns bounded
    summary metadata. Failures preserve the job; a committed exact deletion set
    is recognized on retry rather than applying another oldest-N batch.
    """
    meta = read_meta(job)
    reviewed = None
    if meta["phase"] in {"installed", "rolled_back"}:
        raise PruneError("this staged operation is already terminal")
    if meta["phase"] == "snapshot":
        progress("snapshot: copying a consistent live WAL view")
        budget = Budget(timeout_seconds)
        _remove_image(job.candidate)
        _create_file(job.candidate)
        with source_lock(job.target, exclusive=False), _source(job.target, budget, require_wal=True) as source:
            _schema(source)
            with _image(job.candidate) as destination:
                source.backup(destination, pages=256, progress=lambda *_: budget.check(), sleep=0.01)
        with _source(job.candidate, budget) as snapshot:
            schema, versions, globals_, digest = projection(snapshot, budget)
            _validate_session_schema(snapshot, archive=False)
            _check_owners(snapshot, budget)
        with _state_connection(job.state) as state:
            state.execute("DELETE FROM versions")
            state.executemany("INSERT INTO versions VALUES (?, ?, 1)", versions.items())
            state.execute("DELETE FROM global_events")
            state.executemany("INSERT INTO global_events VALUES (?,?)", _global_markers(globals_).items())
            _write_meta(state, {**meta, "phase": "planning", "schema": schema, "source_digest": digest})
            state.commit()
        meta = read_meta(job)
    if meta["phase"] == "planning":
        progress("planning: validating and selecting on the private copy")
        reviewed = plan_prune(_policy_request(job, timeout_seconds), now_ms=meta["now_ms"])
        authorize(reviewed)
        with _state_connection(job.state) as state:
            state.execute("DELETE FROM selected")
            state.executemany("INSERT INTO selected VALUES (?)", ((value,) for value in reviewed.evidence.selected_session_ids))
            _write_meta(state, {**meta, "phase": "pruning", "now_ms": reviewed.evidence.now_ms})
            state.commit()
        meta = read_meta(job)
    if meta["phase"] == "pruning":
        progress("pruning: deleting the frozen selection on the private copy")
        with _state_connection(job.state) as state:
            selected = {row[0] for row in state.execute("SELECT id FROM selected")}
            expected = {row[0] for row in state.execute("SELECT id FROM versions")}
        with _source(job.candidate, Budget(timeout_seconds)) as candidate:
            actual = set(_versions(candidate, Budget(timeout_seconds)))
        if actual == expected:
            if reviewed is None:
                reviewed = plan_prune(_policy_request(job, timeout_seconds), now_ms=meta["now_ms"])
            if set(reviewed.evidence.selected_session_ids) != selected:
                raise PruneError("the staged selection changed unexpectedly")
            if not job.policy["vacuum_only"]:
                apply_prune_plan(reviewed)
        elif actual != expected - selected:
            raise PruneError("staged prune completion is ambiguous; saved files were preserved")
        meta = {**meta, "phase": "pruned", "result": {"pruned_sessions": len(selected)}}
        write_meta(job, meta)
    if meta["phase"] in {"pruned", "vacuum"}:
        if job.policy["vacuum"] or job.policy["vacuum_only"]:
            write_meta(job, {**meta, "phase": "vacuum"})
            progress("vacuum: compacting the private copy")
            budget = Budget(timeout_seconds)
            with _image(job.candidate) as image:
                budget.bind(image)
                _vacuum(image)
        write_meta(job, {**meta, "phase": "sealing"})
    return seal_job(job, timeout_seconds, progress=progress)


def update_job(job: StageJob, timeout_seconds: float, *, progress: Callable[[str], None]) -> dict[str, object]:
    """Atomically catch up new/changed/deleted sessions in an already-pruned copy.

    Caller holds job_lock. Compare IDs/time_updated only, explicitly accepting
    unreflected edits. Copy complete owned row families and parent dependencies,
    synchronize global metadata, and commit image/versions together. Returns
    counts and sealed metadata; failures roll back both databases. No source
    writes, retention reevaluation or second vacuum occurs.
    """
    meta = seal_job(job, timeout_seconds, progress=progress)
    if meta["phase"] != "ready":
        raise PruneError("staged pruning must finish before catch-up")
    budget = Budget(timeout_seconds)
    progress("update: reading session IDs/activity stamps and global metadata")
    with source_lock(job.target, exclusive=False), _source(job.target, budget, require_wal=True) as source:
        schema, current, globals_, digest = projection(source, budget)
        if schema != meta["schema"]:
            raise PruneError("source schema changed; restart staged pruning")
        if digest == meta["source_digest"]:
            source.rollback()
            result = {key: 0 for key in ("new_sessions", "updated_sessions", "restored_pruned_sessions", "dependency_sessions_restored", "deleted_source_sessions")}
            meta = {**meta, "result": result}
            write_meta(job, meta)
            return meta
        with _state_connection(job.state) as state:
            previous = {row[0]: (row[1], bool(row[2])) for row in state.execute("SELECT id, stamp, retained FROM versions")}
            previous_events = dict(state.execute("SELECT id,digest FROM global_events"))
        current_events = _global_markers(globals_)
        changed_events = {key for key, value in current_events.items() if previous_events.get(key) != value}
        removed_events = set(previous_events) - set(current_events)
        changed = {key for key, stamp in current.items() if key not in previous or previous[key][0] != stamp}
        removed = set(previous) - set(current)
        new_count = sum(key not in previous for key in changed)
        restored = sum(key in previous and not previous[key][1] for key in changed)
        dependency_count = 0
        with _image(job.candidate) as target:
            budget.bind(target)
            retained = {row[0] for row in target.execute("SELECT id FROM session")}
            if "parent_id" in _columns(source, "session"):
                queue = list(changed)
                while queue:
                    budget.check()
                    key = queue.pop()
                    row = source.execute("SELECT parent_id FROM session WHERE id=?", (key,)).fetchone()
                    parent = row[0] if row else None
                    if parent in current and parent not in retained and parent not in changed:
                        changed.add(parent)
                        queue.append(parent)
                        dependency_count += 1
            target.execute("PRAGMA foreign_keys=OFF")
            target.execute("ATTACH DATABASE ? AS tracking", (str(job.state),))
            try:
                target.execute("BEGIN IMMEDIATE")
                target.execute("CREATE TEMP TABLE affected(id TEXT PRIMARY KEY)")
                target.executemany("INSERT INTO affected VALUES (?)", ((key,) for key in changed | removed))
                target.execute("CREATE TEMP TABLE current_ids(id TEXT PRIMARY KEY)")
                target.executemany("INSERT INTO current_ids VALUES (?)", ((key,) for key in current))
                progress(f"update: copying {len(changed)} new/changed/dependency sessions; removing {len(removed)} source sessions")
                for table in CHILD_SESSION_TABLES:
                    budget.check()
                    owner = _quote(SESSION_SELECTOR_COLUMNS[table])
                    target.execute(f"DELETE FROM main.{_quote(table)} WHERE {owner} IN (SELECT id FROM affected)")
                target.execute("DELETE FROM main.session WHERE id IN (SELECT id FROM affected)")
                _copy_selected(source, target, "session", "id", changed, budget)
                for table in CHILD_SESSION_TABLES:
                    _copy_selected(source, target, table, SESSION_SELECTOR_COLUMNS[table], changed, budget)
                progress("update: synchronizing non-session metadata")
                for table, columns, rows in globals_:
                    budget.check()
                    if table == "event_sequence":
                        owner = _quote(SESSION_SELECTOR_COLUMNS[table])
                        target.execute(f"DELETE FROM main.{_quote(table)} WHERE {owner} IS NULL OR {owner} NOT IN (SELECT id FROM current_ids)")
                    else:
                        target.execute(f"DELETE FROM main.{_quote(table)}")
                    _insert_rows(target, table, columns, rows, budget)
                for key in (changed_events | removed_events) - set(current):
                    budget.check()
                    target.execute("DELETE FROM main.event WHERE aggregate_id=?", (key,))
                _copy_selected(source, target, "event", "aggregate_id", changed_events, budget)
                # All source rows are now copied from one snapshot. Release it
                # before validating/sealing locally so source WAL can checkpoint.
                source.rollback()
                progress("update: validating the synchronized copy")
                _validate_image(target)
                target.execute("DELETE FROM tracking.versions")
                final_retained = (retained - removed) | changed
                target.executemany(
                    "INSERT INTO tracking.versions VALUES (?, ?, ?)",
                    ((key, stamp, int(key in final_retained)) for key, stamp in current.items()),
                )
                target.execute("DELETE FROM tracking.global_events")
                target.executemany("INSERT INTO tracking.global_events VALUES (?,?)", current_events.items())
                result = {
                    "new_sessions": new_count, "updated_sessions": len(changed) - new_count - dependency_count,
                    "restored_pruned_sessions": restored, "dependency_sessions_restored": dependency_count,
                    "deleted_source_sessions": len(removed),
                }
                _write_meta(target, {**meta, "phase": "sealing", "source_digest": digest, "generation": meta["generation"] + 1, "result": result}, prefix="tracking.")
                budget.check()
                target.commit()
            except BaseException as error:
                target.set_progress_handler(None, 0)
                target.rollback()
                if not isinstance(error, KeyboardInterrupt):
                    budget.check()
                raise
    return seal_job(job, timeout_seconds, progress=progress)


def seal_job(job: StageJob, timeout_seconds: float, *, progress: Callable[[str], None]) -> dict[str, object]:
    """Recover private journals, validate and hash an unsealed committed image.

    Caller holds job_lock. Returns ready metadata and verifies a prior ready
    digest before reuse. Unknown phases/changed bytes refuse; SQLite/OS errors
    preserve artifacts. Only private files are opened writable for recovery.
    """
    meta = read_meta(job)
    if meta["phase"] not in {"ready", "sealing"}:
        return meta
    progress("seal: verifying the staged image")
    budget = Budget(timeout_seconds)
    _private(job.candidate, 0o600)
    with _image(job.candidate) as image:
        budget.bind(image)
        image.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        if _schema(image) != meta["schema"]:
            raise PruneError("staged candidate schema changed")
        if meta["phase"] == "sealing":
            _validate_image(image)
    digest = budget.digest(job.candidate)
    if meta["phase"] == "ready" and digest != meta["candidate_sha256"]:
        raise PruneError("staged candidate bytes changed; files were preserved")
    if meta["phase"] == "sealing":
        with _state_connection(job.state) as state:
            with _source(job.candidate, budget) as image:
                retained = set(_versions(image, budget))
            expected = {row[0] for row in state.execute("SELECT id FROM versions WHERE retained=1")}
            if meta["generation"] == 0:
                expected -= {row[0] for row in state.execute("SELECT id FROM selected")}
            if retained != expected:
                raise PruneError("staged candidate session set differs from its committed selection/cursor")
            state.execute("UPDATE versions SET retained=0")
            state.executemany("UPDATE versions SET retained=1 WHERE id=?", ((key,) for key in retained))
            meta = {**meta, "phase": "ready", "candidate_sha256": digest}
            _write_meta(state, meta)
            state.commit()
    return meta


def source_digest(path: Path, timeout_seconds: float) -> str:
    """Read one consistent logical source generation under the timestamp profile.

    Returns a digest of schema, session IDs/stamps and global metadata; it is
    explicitly not a full source-content hash. Never writes source rows/schema.
    Raises PruneError for unsupported data and bounded SQLite/OS errors otherwise.
    """
    budget = Budget(timeout_seconds)
    with _source(path, budget, require_wal=True) as source:
        return projection(source, budget)[3]


def projection(connection: sqlite3.Connection, budget: Budget) -> tuple[str, dict[str, int], list, str]:
    """Collect bounded metadata from one already-open read snapshot.

    Returns schema digest, session stamps, global row batches and generation
    digest. Non-session events use native sequence-row markers; immutable event
    payloads are copied only when their aggregate changes. Raises
    PruneError on unsupported schema/data/limits; no source mutation occurs.
    """
    schema = _schema(connection)
    versions = _versions(connection, budget)
    globals_ = []
    count = 0
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    global_ids = set()
    for (value,) in connection.execute("SELECT aggregate_id FROM event_sequence"):
        budget.check()
        if not isinstance(value, str) or len(value.encode("utf-8")) > 16384:
            raise PruneError("global event ownership is invalid")
        if value not in versions:
            global_ids.add(value)
            if len(global_ids) > _MAX_SESSIONS:
                raise PruneError("global event metadata exceeds supported bounds")
    global_ids = sorted(global_ids)
    for table in tables + (["sqlite_sequence"] if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone() else []):
        if table in SESSION_TABLES and table != "event_sequence":
            continue
        columns = _columns(connection, table)
        selected = ",".join(map(_quote, columns))
        rows = []
        cursors = (connection.execute(f"SELECT {selected} FROM {_quote(table)} WHERE aggregate_id=?", (value,)) for value in global_ids) if table == "event_sequence" else [connection.execute(f"SELECT {selected} FROM {_quote(table)}")]
        for cursor in cursors:
            for row in cursor:
                budget.check()
                count += sum(len(_cell(value)) for value in row)
                if count > _MAX_GLOBAL:
                    raise PruneError("non-session staged metadata exceeds supported bounds")
                rows.append(tuple(row))
        # Row order is not a change; primary keys are not required for globals.
        rows.sort(key=lambda row: tuple(_cell(value) for value in row))
        globals_.append((table, columns, rows))
    digest = hashlib.sha256(schema.encode("ascii"))
    digest.update(_cell(len(versions)))
    for key, stamp in sorted(versions.items()):
        digest.update(_cell(key) + _cell(stamp))
    digest.update(_cell(len(globals_)))
    for table, columns, rows in globals_:
        digest.update(_cell(table) + _cell(json.dumps(columns)) + _cell(len(rows)))
        for row in rows:
            for value in row:
                digest.update(_cell(value))
    return schema, versions, globals_, digest.hexdigest()


def read_meta(job: StageJob) -> dict[str, object]:
    """Read/recover the private state database and reject future/malformed state.

    Returns the closed metadata mapping; it never opens the source. Private
    hot-journal recovery may write the state database. Errors preserve files.
    """
    _private(job.state, 0o600)
    with _state_connection(job.state) as state:
        state.execute("BEGIN")
        row = state.execute("SELECT value FROM meta WHERE id=1").fetchone()
        cursor_digest = _cursor_digest(state)
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, IndexError) as error:
        raise PruneError("staged state is malformed") from error
    if not isinstance(value, dict) or set(value) != _META_KEYS or not isinstance(value["phase"], str) or value["phase"] not in _PHASES or type(value["generation"]) is not int or not 0 <= value["generation"] < 2**63 or not isinstance(value["result"], dict):
        raise PruneError("staged state is malformed or unsupported")
    if not set(value["result"]) <= _RESULT_KEYS or any(type(item) is not int or not 0 <= item <= _MAX_SESSIONS for item in value["result"].values()):
        raise PruneError("staged result counts are malformed")
    for key in ("schema", "source_digest", "candidate_sha256", "cursor_sha256", "job_sha256"):
        if value[key] is not None and (not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None):
            raise PruneError("staged state digest is malformed")
    if value["phase"] != "snapshot" and (value["schema"] is None or value["source_digest"] is None):
        raise PruneError("staged state lacks its source binding")
    if value["phase"] in {"ready", "installed", "rolled_back"} and value["candidate_sha256"] is None:
        raise PruneError("staged state lacks its candidate binding")
    if value["now_ms"] is not None and (type(value["now_ms"]) is not int or not 0 <= value["now_ms"] < 2**63):
        raise PruneError("staged policy clock is malformed")
    if value["job_sha256"] != hashlib.sha256(_read_canonical_json(job.directory / "job.json")[1]).hexdigest():
        raise PruneError("staged job policy changed; files were preserved")
    if value["cursor_sha256"] != cursor_digest:
        raise PruneError("staged synchronization cursor changed; files were preserved")
    return value


def write_meta(job: StageJob, value: dict[str, object]) -> None:
    """Commit tool-generated metadata to the private state file, never the source.

    Caller holds job_lock and owns phase/schema correctness. ``value`` is the
    next metadata mapping; its cursor checksum is refreshed in place. Returns
    None after commit, or raises private-path, SQLite, serialization or I/O
    errors. This is not an interface for accepting unvalidated external state.
    """
    with _state_connection(job.state) as state:
        _write_meta(state, value)
        state.commit()


def _write_meta(connection, value, *, prefix=""):
    value["cursor_sha256"] = _cursor_digest(connection, prefix=prefix)
    connection.execute(f"INSERT OR REPLACE INTO {prefix}meta VALUES (1, ?)", (json.dumps(value, sort_keys=True, allow_nan=False),))


def _cursor_digest(connection, *, prefix=""):
    """Bind selection/retention/version authority without exposing private IDs."""
    digest = hashlib.sha256()
    count = 0
    size = 0
    for table, columns in (("versions", "id,stamp,retained"), ("selected", "id"), ("global_events", "id,digest")):
        digest.update(_cell(table))
        for row in connection.execute(f"SELECT {columns} FROM {prefix}{table} ORDER BY id"):
            key = row[0]
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 16384:
                raise PruneError("staged cursor identifier is malformed")
            if table == "versions" and (type(row[1]) is not int or type(row[2]) is not int or row[2] not in (0, 1)):
                raise PruneError("staged cursor stamps are malformed")
            if table == "global_events" and (not isinstance(row[1], str) or re.fullmatch(r"[0-9a-f]{64}", row[1]) is None):
                raise PruneError("staged event cursor is malformed")
            count += 1
            digest.update(b"R" + _cell(len(row)))
            for item in row:
                encoded = _cell(item)
                size += len(encoded)
                digest.update(encoded)
            if count > 3 * _MAX_SESSIONS or size > 3 * _MAX_METADATA:
                raise PruneError("staged cursor exceeds supported bounds")
        digest.update(b"E")
    return digest.hexdigest()


def _policy_request(job, timeout):
    return PruneRequest(str(job.candidate), **{**job.policy, "vacuum": False, "vacuum_only": job.policy["vacuum_only"]}, yes=True, timeout_seconds=timeout)


def _validate_policy(policy, database):
    from .prune import _normalize_selector
    if any(type(policy[name]) is not bool for name in ("vacuum", "vacuum_only", "estimate_size")):
        raise PruneError("staged policy flags are malformed")
    if any(policy[name] is not None and (not isinstance(policy[name], str) or not 0 < len(policy[name]) <= 256) for name in ("project_id", "oldest", "keep_newest", "target_size")):
        raise PruneError("staged policy selectors are malformed")
    _normalize_selector(PruneRequest(database, **policy))


def _columns(connection, table):
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    if not rows or any(not isinstance(row[1], str) for row in rows):
        raise PruneError("staged table metadata is invalid")
    return tuple(row[1] for row in rows)


def _schema(connection):
    rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    if len(rows) > 512 or sum(len(str(value).encode("utf-8")) for row in rows for value in row) > 2 * 1024 * 1024:
        raise PruneError("staged schema exceeds supported bounds")
    if any(row[0] == "trigger" or row[0] == "table" and isinstance(row[3], str) and re.match(r"CREATE\s+VIRTUAL\s+TABLE", row[3], re.I) for row in rows):
        raise PruneError("staged synchronization does not support triggers or virtual tables")
    if "time_updated" not in _columns(connection, "session"):
        raise PruneError("timestamp synchronization requires session.time_updated")
    _validate_session_schema(connection, archive=False)
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=True, allow_nan=False).encode("ascii"))
    for pragma in ("user_version", "application_id"):
        digest.update(_cell(pragma) + _cell(connection.execute(f"PRAGMA {pragma}").fetchone()[0]))
    tables = {row[1] for row in rows if row[0] == "table"}
    for table in sorted(tables & {"__drizzle_migrations", "_drizzle_migrations", "schema_migrations", "migrations"}):
        columns = _columns(connection, table)
        records = []
        size = 0
        for row in connection.execute(f"SELECT {','.join(map(_quote, columns))} FROM {_quote(table)}"):
            encoded = b"".join(_cell(item) for item in row)
            size += len(encoded)
            if size > 4 * 1024 * 1024 or len(records) >= 10000:
                raise PruneError("staged migration metadata exceeds supported bounds")
            records.append(encoded)
        digest.update(_cell(table) + _cell(len(records)))
        for record in sorted(records):
            digest.update(_cell(record))
    return digest.hexdigest()


def _versions(connection, budget):
    versions = {}
    size = 0
    for key, stamp in connection.execute("SELECT id,time_updated FROM session"):
        budget.check()
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 16384 or type(stamp) is not int or key in versions:
            raise PruneError("staged session IDs/activity stamps are invalid")
        size += len(key.encode("utf-8")) + 16
        if len(versions) >= _MAX_SESSIONS or size > _MAX_METADATA:
            raise PruneError("staged session metadata exceeds supported bounds")
        versions[key] = stamp
    return versions


def _cell(value):
    if value is None:
        return b"N"
    if type(value) is int:
        payload = str(value).encode("ascii")
        tag = b"I"
    elif type(value) is float:
        payload = value.hex().encode("ascii")
        tag = b"F"
    elif isinstance(value, str):
        payload = value.encode("utf-8")
        tag = b"T"
    elif isinstance(value, bytes):
        payload = value
        tag = b"B"
    else:
        raise PruneError("staged metadata has an unsupported SQLite value")
    return tag + struct.pack(">Q", len(payload)) + payload


def _global_markers(globals_):
    """Bind immutable non-session event histories to native sequence metadata."""
    result = {}
    for table, columns, rows in globals_:
        if table != "event_sequence":
            continue
        owner = columns.index("aggregate_id")
        for row in rows:
            result.setdefault(row[owner], hashlib.sha256()).update(b"".join(_cell(value) for value in row))
    return {key: value.hexdigest() for key, value in result.items()}


def _check_owners(connection, budget):
    for table in CHILD_SESSION_TABLES:
        if table in {"event", "event_sequence"}:
            continue
        budget.check()
        owner = _quote(SESSION_SELECTOR_COLUMNS[table])
        if connection.execute(f"SELECT 1 FROM {_quote(table)} AS child LEFT JOIN session AS parent ON parent.id=child.{owner} WHERE parent.id IS NULL LIMIT 1").fetchone():
            raise PruneError("staged source has orphaned session-owned rows")


def _copy_selected(source, target, table, owner, identifiers, budget):
    columns = _columns(source, table)
    query = f"SELECT {','.join(map(_quote, columns))} FROM {_quote(table)} WHERE {_quote(owner)}=?"
    for key in identifiers:
        budget.check()
        _insert_rows(target, table, columns, source.execute(query, (key,)), budget)


def _insert_rows(connection, table, columns, rows, budget):
    query = f"INSERT INTO main.{_quote(table)} ({','.join(map(_quote, columns))}) VALUES ({','.join('?' for _ in columns)})"
    for row in rows:
        budget.check()
        connection.execute(query, row)


def _validate_image(connection):
    _validate_prune_database_health(connection)


@contextmanager
def _source(path, budget, *, require_wal=False):
    _private_regular(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        budget.bind(connection)
        if require_wal and connection.execute("PRAGMA journal_mode").fetchone() != ("wal",):
            raise PruneError("online staged pruning requires an already-WAL source; journal policy was not changed")
        connection.execute("BEGIN")
        connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        yield connection
    except sqlite3.Error:
        budget.check()
        raise
    finally:
        connection.close()


@contextmanager
def _image(path):
    _private(path, 0o600)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise PruneError("private staged files require rollback journaling for atomic synchronization")
        connection.execute("PRAGMA synchronous=FULL")
        yield connection
    finally:
        connection.close()


@contextmanager
def _state_connection(path):
    with _image(path) as state:
        state.isolation_level = "DEFERRED"
        yield state


def _create_file(path):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _private(path, mode):
    if path.is_symlink() or not path.exists() or path.stat().st_mode & 0o777 != mode or (not path.is_dir() if mode == 0o700 else not path.is_file()):
        raise PruneError("staged artifacts must be private regular files/directories")


def _private_regular(path):
    if not path.is_absolute() or not path.is_file():
        raise PruneError("staged source must be an existing absolute regular database")


def _remove_image(path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        value = Path(str(path) + suffix)
        if value.is_symlink() or value.exists() and not value.is_file():
            raise PruneError("partial staged image is unsafe")
        value.unlink(missing_ok=True)
