"""Exercise online snapshot pruning and the explicitly accepted timestamp sync."""

from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from opencode_db import cli, staged_install, staged_prune as stage
from opencode_db.prune import PruneError, PruneRequest
import test_prune as fixtures

_ROOT = Path("/tmp/opencode-db-v1")
_CUTOVER_CHILD = """
import os,sys
from opencode_db import staged_prune as stage, staged_install as install
source,work,identifier,ordinal,action = sys.argv[1:]
job = stage.load_job(source,work,identifier)
def stop(index):
    if index == int(ordinal): os._exit(71)
with stage.job_lock(job):
    operation = install.rollback_job if action == 'rollback' else install.install_job
    operation(job,30,progress=lambda _:None,after_move=stop)
"""
_UPDATE_CHILD = """
import os,sys
from contextlib import contextmanager
from opencode_db import staged_prune as stage
source,work,identifier,boundary=sys.argv[1:]
job=stage.load_job(source,work,identifier)
image=stage._image
@contextmanager
def small(path):
    with image(path) as connection:
        connection.execute('PRAGMA cache_size=1')
        yield connection
stage._image=small
if boundary=='uncommitted': stage._validate_image=lambda _:os._exit(71)
count=0
def stop(phase):
    global count
    if phase.startswith('seal:'):
        count+=1
        if count==2: os._exit(71)
with stage.job_lock(job): stage.update_job(job,30,progress=stop)
"""


class StagedPruneTests(unittest.TestCase):
    """Use isolated WAL sources; never access installed/private session databases."""

    def _fixture(self, root):
        database = root / "opencode.db"
        work = root / "work"
        work.mkdir(mode=0o700)
        fixtures.SessionPruneTests._create_database(database)
        fixtures.SessionPruneTests._insert_sessions(database)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
        return database, work

    def _job(self, database, work, **policy):
        request = PruneRequest(str(database), oldest="1", staged=True, work_dir=str(work), **policy)
        job = stage.create_job(request)
        with stage.job_lock(job):
            stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
        return job

    def _run(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _ids(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return {row[0] for row in connection.execute("SELECT id FROM session")}

    def _child(self, script, args):
        result = subprocess.run(
            [sys.executable, "-c", script, *map(str, args)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 71, result.stderr)

    def _global_schema(self, database):
        with closing(sqlite3.connect(database)) as source:
            for table in ("event", "event_sequence"):
                rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                source.execute(f'DROP TABLE "{table}"')
                source.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, payload BLOB)')
                source.executemany(f'INSERT INTO "{table}" VALUES (?,?,?)', rows)
            source.commit()

    def test_prepare_prunes_only_a_consistent_private_copy_without_source_tracking(self):
        """The heavy prune never deletes source rows or adds source schema objects."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            with closing(sqlite3.connect(database)) as active:
                before_schema = active.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
                before_rows = active.execute("SELECT * FROM session ORDER BY id").fetchall()
                job = self._job(database, work)
                self.assertEqual(active.execute("SELECT * FROM session ORDER BY id").fetchall(), before_rows)
                self.assertEqual(active.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name").fetchall(), before_schema)
                self.assertEqual(active.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            self.assertEqual(len(self._ids(job.candidate)), 3)
            self.assertEqual(stage.read_meta(job)["phase"], "ready")
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                self.assertFalse(image.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE '%stage%' OR name LIKE '%tracking%'").fetchone())

    def test_source_writer_can_commit_while_its_backup_snapshot_is_read(self):
        """A concurrent WAL writer is not stopped and its later session is caught up."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = stage.create_job(PruneRequest(str(database), oldest="1", staged=True, work_dir=str(work)))
            original = stage.Budget.check
            wrote = False
            def mutate(budget):
                nonlocal wrote
                if not wrote:
                    wrote = True
                    fixtures.SessionPruneTests._insert_session(database, "during-backup", "a", 99, b"new-body")
                original(budget)
            with stage.job_lock(job), patch.object(stage.Budget, "check", mutate):
                stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
            self.assertTrue(wrote)
            self.assertNotIn("during-backup", self._ids(job.candidate))
            with stage.job_lock(job):
                result = stage.update_job(job, 30, progress=lambda _: None)
            self.assertEqual(result["result"]["new_sessions"], 1)
            self.assertIn("during-backup", self._ids(job.candidate))

    def test_updates_new_changed_deleted_and_restored_sessions_without_reapplying_prune(self):
        """Whole families replace, pruned changes restore, and unchanged pruned IDs stay absent."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            removed_by_prune = (self._ids(database) - self._ids(job.candidate)).pop()
            kept = sorted(self._ids(job.candidate))
            with closing(sqlite3.connect(database)) as source:
                source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id IN (?,?)", (removed_by_prune, kept[0]))
                source.execute("UPDATE message SET payload=? WHERE session_id=?", (b"edited child", kept[0]))
                source.execute("DELETE FROM todo WHERE session_id=?", (kept[0],))
                for table in stage.CHILD_SESSION_TABLES:
                    owner = stage.SESSION_SELECTOR_COLUMNS[table]
                    source.execute(f'DELETE FROM "{table}" WHERE "{owner}"=?', (kept[1],))
                source.execute("DELETE FROM session WHERE id=?", (kept[1],))
                source.execute("UPDATE project SET name='renamed project' WHERE id='a'")
                source.commit()
            fixtures.SessionPruneTests._insert_session(database, "new-session", "a", 1000, b"new-body")
            with stage.job_lock(job), patch.object(stage, "apply_prune_plan", side_effect=AssertionError("pruning repeated")):
                result = stage.update_job(job, 30, progress=lambda _: None)
            self.assertEqual(result["result"]["new_sessions"], 1)
            self.assertEqual(result["result"]["restored_pruned_sessions"], 1)
            self.assertEqual(result["result"]["deleted_source_sessions"], 1)
            self.assertEqual(self._ids(job.candidate), self._ids(database))
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("SELECT payload FROM message WHERE session_id=?", (kept[0],)).fetchall(), [(b"edited child",)])
                self.assertEqual(image.execute("SELECT * FROM todo WHERE session_id=?", (kept[0],)).fetchall(), [])
                self.assertEqual(image.execute("SELECT name FROM project WHERE id='a'").fetchone(), ("renamed project",))
                before = image.execute("SELECT * FROM message ORDER BY id").fetchall()
            with stage.job_lock(job):
                again = stage.update_job(job, 30, progress=lambda _: None)
            self.assertTrue(all(count == 0 for count in again["result"].values()))
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("SELECT * FROM message ORDER BY id").fetchall(), before)

    def test_equal_timestamps_intentionally_do_not_detect_child_only_edits(self):
        """Keep the accepted limitation explicit instead of claiming full content sync."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            key = sorted(self._ids(job.candidate))[0]
            with closing(sqlite3.connect(database)) as source:
                source.execute("UPDATE message SET payload=? WHERE session_id=?", (b"unreflected child edit", key))
                source.commit()
            with stage.job_lock(job):
                result = stage.update_job(job, 30, progress=lambda _: None)
            self.assertEqual(result["result"]["updated_sessions"], 0)
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertNotEqual(image.execute("SELECT payload FROM message WHERE session_id=?", (key,)).fetchone(), (b"unreflected child edit",))

    def test_timestamp_regression_is_a_change_and_unchanged_pruned_sessions_stay_deleted(self):
        """Compare stamps for difference, not just increasing wall-clock time."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            pruned = self._ids(database) - self._ids(job.candidate)
            key = sorted(self._ids(job.candidate))[0]
            with closing(sqlite3.connect(database)) as source:
                source.execute("UPDATE session SET time_updated=-1 WHERE id=?", (key,))
                source.commit()
            with stage.job_lock(job):
                stage.update_job(job, 30, progress=lambda _: None)
            self.assertFalse(pruned & self._ids(job.candidate))
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("SELECT time_updated FROM session WHERE id=?", (key,)).fetchone(), (-1,))

    def test_globals_and_non_session_event_aggregates_follow_the_same_snapshot(self):
        """Preserve fresh global metadata rather than syncing session rows alone."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            # Event aggregates in OpenCode include projects as well as sessions.
            self._global_schema(database)
            job = self._job(database, work)
            with closing(sqlite3.connect(database)) as source:
                source.execute("INSERT INTO project VALUES ('c','C')")
                source.execute("INSERT INTO event_sequence VALUES ('global-seq','project:c',?)", (b"counter",))
                source.execute("INSERT INTO event VALUES ('global-event','project:c',?)", (b"project event",))
                source.commit()
            with stage.job_lock(job):
                stage.update_job(job, 30, progress=lambda _: None)
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("SELECT name FROM project WHERE id='c'").fetchone(), ("C",))
                self.assertEqual(image.execute("SELECT payload FROM event WHERE id='global-event'").fetchone(), (b"project event",))

    def test_update_failure_rolls_back_both_image_and_sync_cursor(self):
        """Failure after row replacement cannot acknowledge changes without committing them."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            before = job.candidate.read_bytes()
            meta = stage.read_meta(job)
            fixtures.SessionPruneTests._insert_session(database, "new", "a", 100, b"new")
            with stage.job_lock(job), patch.object(stage, "_validate_image", side_effect=PruneError("injected validation refusal")):
                with self.assertRaises(PruneError):
                    stage.update_job(job, 30, progress=lambda _: None)
            self.assertEqual(job.candidate.read_bytes(), before)
            self.assertEqual(stage.read_meta(job), meta)
            with stage.job_lock(job):
                self.assertEqual(stage.update_job(job, 30, progress=lambda _: None)["result"]["new_sessions"], 1)

    def test_changed_schema_candidate_and_unknown_job_state_refuse(self):
        """Do not trust altered images or future persistence formats."""
        for mutation in ("schema", "candidate", "job", "state"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, work = self._fixture(Path(directory))
                job = self._job(database, work)
                if mutation == "schema":
                    with closing(sqlite3.connect(database)) as source:
                        source.execute("CREATE TABLE future_table(id TEXT PRIMARY KEY)")
                elif mutation == "candidate":
                    with job.candidate.open("ab") as writer:
                        writer.write(b"changed")
                elif mutation == "job":
                    value = json.loads((job.directory / "job.json").read_text())
                    value["schema_version"] = 999
                    stage._write_json(job.directory / "job.json", value)
                else:
                    value = stage.read_meta(job)
                    stage.write_meta(job, {**value, "phase": "future"})
                code, _, error = self._run(["prune", "update", "--db", str(database), "--work-dir", str(work), "--operation", job.operation_id])
                self.assertEqual(code, 4, error)

    def test_nonwal_source_refuses_without_changing_its_journal_policy(self):
        """Online snapshot support does not silently alter source journal configuration."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            with closing(sqlite3.connect(database)) as source:
                source.execute("PRAGMA journal_mode=DELETE")
            job = stage.create_job(PruneRequest(str(database), oldest="1", staged=True, work_dir=str(work)))
            with stage.job_lock(job), self.assertRaisesRegex(PruneError, "already-WAL"):
                stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
            with closing(sqlite3.connect(database)) as source:
                self.assertEqual(source.execute("PRAGMA journal_mode").fetchone(), ("delete",))

    def test_cli_staged_prepare_update_install_and_explicit_rollback(self):
        """The two-stage CLI preserves originals and installs only with --install."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            original_ids = self._ids(database)
            code, output, error = self._run(["prune", "--db", str(database), "--oldest", "1", "--staged", "--work-dir", str(work), "--yes", "--progress"])
            self.assertEqual(code, 0, output + error)
            identifier = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("operation:"))
            self.assertIn("planning:", error)
            self.assertEqual(self._ids(database), original_ids)
            args = ["--db", str(database), "--work-dir", str(work), "--operation", identifier]
            code, output, error = self._run(["prune", "update", *args, "--install"])
            self.assertEqual(code, 0, output + error)
            self.assertIn("state: installed", output)
            self.assertEqual(len(self._ids(database)), 3)
            code, output, error = self._run(["prune", "rollback", *args])
            self.assertEqual(code, 0, output + error)
            self.assertEqual(self._ids(database), original_ids)
            code, _, error = self._run(["prune", "discard", *args])
            self.assertEqual(code, 0, error)

    def test_restore_parent_dependency_when_new_session_uses_a_pruned_parent(self):
        """Keep an unchanged pruned ancestor needed by a newly created child."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            with closing(sqlite3.connect(database)) as source:
                source.execute("ALTER TABLE session ADD COLUMN parent_id TEXT")
            job = self._job(database, work)
            parent = (self._ids(database) - self._ids(job.candidate)).pop()
            fixtures.SessionPruneTests._insert_session(database, "new-child", "a", 1000, b"child", parent_id=parent)
            with stage.job_lock(job):
                result = stage.update_job(job, 30, progress=lambda _: None)
            self.assertEqual(result["result"]["dependency_sessions_restored"], 1)
            self.assertIn(parent, self._ids(job.candidate))

    def test_resume_after_private_prune_commit_does_not_delete_another_oldest_batch(self):
        """A frozen exact selection distinguishes committed work from an interrupted transaction."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = stage.create_job(PruneRequest(str(database), oldest="1", staged=True, work_dir=str(work)))
            write = stage.write_meta
            def interrupted(job, value):
                if value["phase"] == "pruned":
                    raise OSError("interrupted after commit")
                return write(job, value)
            with stage.job_lock(job), patch.object(stage, "write_meta", side_effect=interrupted):
                with self.assertRaises(OSError):
                    stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
            self.assertEqual(len(self._ids(job.candidate)), 3)
            with stage.job_lock(job), patch.object(stage, "apply_prune_plan", side_effect=AssertionError("pruned twice")):
                stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
            self.assertEqual(len(self._ids(job.candidate)), 3)
            self.assertEqual(len(self._ids(database)), 4)

    def test_fresh_process_update_is_atomic_before_and_after_attached_commit(self):
        """Image/metadata survive hard death without false acknowledgement or duplicate families."""
        for boundary in ("uncommitted", "committed"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, work = self._fixture(Path(directory))
                job = self._job(database, work)
                fixtures.SessionPruneTests._insert_session(database, "after-snapshot", "a", 10000, b"x" * 100000)
                self._child(_UPDATE_CHILD, (database, work, job.operation_id, boundary))
                with stage.job_lock(job):
                    result = stage.update_job(job, 30, progress=lambda _: None)
                self.assertEqual(result["phase"], "ready")
                self.assertIn("after-snapshot", self._ids(job.candidate))
                self.assertEqual(result["generation"], 1)
                with closing(sqlite3.connect(job.candidate)) as image:
                    self.assertEqual(image.execute("SELECT count(*) FROM message WHERE session_id='after-snapshot'").fetchone(), (1,))

    def test_install_and_rollback_resume_at_every_rename_boundary(self):
        """A dead process leaves enough exact intent to finish or restore without overwrites."""
        for recovery in ("install", "rollback"):
            for ordinal in range(1, 5):
                with self.subTest(recovery=recovery, ordinal=ordinal), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                    database, work = self._fixture(Path(directory))
                    job = self._job(database, work)
                    stage.source_digest(database, 30)
                    steps = sum(Path(str(database) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")) + 1
                    if ordinal > steps:
                        continue
                    self._child(_CUTOVER_CHILD, (database, work, job.operation_id, ordinal, "install"))
                    loaded = stage.load_job(str(database), str(work), job.operation_id)
                    with stage.job_lock(loaded):
                        operation = staged_install.install_job if recovery == "install" else staged_install.rollback_job
                        result = operation(loaded, 30, progress=lambda _: None)
                    self.assertEqual(result["phase"], "installed" if recovery == "install" else "rolled_back")
                    self.assertEqual(len(self._ids(database)), 3 if recovery == "install" else 4)

    def test_explicit_rollback_itself_resumes_after_a_hard_exit(self):
        """Recover an interrupted restoration while preserving both old and pruned images."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            with stage.job_lock(job):
                staged_install.install_job(job, 30, progress=lambda _: None)
            self._child(_CUTOVER_CHILD, (database, work, job.operation_id, 1, "rollback"))
            with stage.job_lock(job):
                staged_install.rollback_job(job, 30, progress=lambda _: None)
            self.assertEqual(len(self._ids(database)), 4)

    def test_changed_source_before_cutover_and_new_active_work_before_rollback_refuse(self):
        """Do not replace a newly advanced source or roll back over newer installed work."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            copy = staged_install._copy_image
            def source_advance(*args):
                copy(*args)
                with closing(sqlite3.connect(database)) as source:
                    source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='a-3'")
                    source.commit()
            with stage.job_lock(job), patch.object(staged_install, "_copy_image", side_effect=source_advance):
                with self.assertRaisesRegex(PruneError, "source changed"):
                    staged_install.install_job(job, 30, progress=lambda _: None)
            self.assertEqual(len(self._ids(database)), 4)
            with stage.job_lock(job):
                stage.update_job(job, 30, progress=lambda _: None)
                staged_install.install_job(job, 30, progress=lambda _: None)
            fixtures.SessionPruneTests._insert_session(database, "new-live-work", "a", 1000, b"keep this")
            with stage.job_lock(job), self.assertRaises(PruneError):
                staged_install.rollback_job(job, 30, progress=lambda _: None)
            self.assertIn("new-live-work", self._ids(database))

    def test_discard_refuses_unlisted_files_or_nonterminal_cutover(self):
        """Exact job retirement must not delete unrelated files or needed recovery authority."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            unrelated = job.directory / "unrelated.txt"
            unrelated.write_text("preserve")
            with stage.job_lock(job), self.assertRaises(PruneError):
                staged_install.discard_job(job)
            self.assertEqual(unrelated.read_text(), "preserve")
            unrelated.unlink()
            with stage.job_lock(job):
                with self.assertRaises(OSError):
                    staged_install.install_job(job, 30, progress=lambda _: None, after_move=lambda _: (_ for _ in ()).throw(OSError("stop")))
                with self.assertRaises(PruneError):
                    staged_install.discard_job(job)
                staged_install.rollback_job(job, 30, progress=lambda _: None)
                staged_install.discard_job(job)
            self.assertFalse(job.directory.exists())

    def test_update_reads_only_selected_session_payloads(self):
        """Activity detection scans metadata, not every message/part payload in the source."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            key = sorted(self._ids(job.candidate))[0]
            with closing(sqlite3.connect(database)) as source:
                source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id=?", (key,))
                source.commit()
            queries = []
            original = stage._source
            @contextmanager
            def traced(path, *args, **kwargs):
                with original(path, *args, **kwargs) as connection:
                    if path == database:
                        connection.set_trace_callback(queries.append)
                    yield connection
            with stage.job_lock(job), patch.object(stage, "_source", traced):
                stage.update_job(job, 30, progress=lambda _: None)
            payload_queries = [query for query in queries if query.startswith("SELECT") and ('FROM "message"' in query or 'FROM "part"' in query)]
            self.assertTrue(payload_queries)
            self.assertTrue(all('WHERE "session_id"=' in query for query in payload_queries))

    def test_native_global_event_markers_avoid_scanning_unchanged_history(self):
        """Large immutable global event payloads do not become unbounded metadata buffers."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            self._global_schema(database)
            with closing(sqlite3.connect(database)) as source:
                source.execute("INSERT INTO event_sequence VALUES ('global-seq','project:a',?)", (b"one",))
                source.execute("INSERT INTO event VALUES ('global-event','project:a',?)", (b"x" * 200000,))
                source.commit()
            with patch.object(stage, "_MAX_GLOBAL", 1024):
                job = self._job(database, work)
                with closing(sqlite3.connect(database)) as source:
                    source.execute("UPDATE event_sequence SET payload=? WHERE aggregate_id='project:a'", (b"two",))
                    source.execute("UPDATE event SET payload=? WHERE aggregate_id='project:a'", (b"new" * 100000,))
                    source.commit()
                with stage.job_lock(job):
                    stage.update_job(job, 30, progress=lambda _: None)
            with closing(sqlite3.connect(job.candidate)) as image:
                self.assertEqual(image.execute("SELECT payload FROM event WHERE id='global-event'").fetchone(), (b"new" * 100000,))

    def test_staged_vacuum_runs_once_without_repeating_retention(self):
        """Pruning and compaction are separately durable stages of the private copy."""
        for vacuum_only in (False, True):
            with self.subTest(vacuum_only=vacuum_only), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, work = self._fixture(Path(directory))
                request = PruneRequest(str(database), oldest=None if vacuum_only else "1", vacuum=not vacuum_only, vacuum_only=vacuum_only, staged=True, work_dir=str(work))
                job = stage.create_job(request)
                with stage.job_lock(job), patch.object(stage, "_vacuum", wraps=stage._vacuum) as vacuum:
                    stage.prepare_job(job, 30, authorize=lambda _: None, progress=lambda _: None)
                self.assertEqual(vacuum.call_count, 1)
                self.assertEqual(len(self._ids(job.candidate)), 4 if vacuum_only else 3)
                self.assertEqual(len(self._ids(database)), 4)

    def test_changed_cursor_and_policy_refuse_before_source_reads(self):
        """Selection/version authority is checksummed, not blindly trusted on restart."""
        for mutation in ("cursor", "policy"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, work = self._fixture(Path(directory))
                job = self._job(database, work)
                before = job.candidate.read_bytes()
                if mutation == "cursor":
                    with closing(sqlite3.connect(job.state)) as state:
                        state.execute("UPDATE versions SET stamp=stamp+1")
                        state.commit()
                else:
                    value = json.loads((job.directory / "job.json").read_text())
                    value["policy"]["oldest"] = "2"
                    stage._write_json(job.directory / "job.json", value)
                with stage.job_lock(job), patch.object(stage, "_source", side_effect=AssertionError("source was opened")):
                    with self.assertRaises(PruneError):
                        stage.update_job(job, 30, progress=lambda _: None)
                self.assertEqual(job.candidate.read_bytes(), before)

    def test_busy_job_refuses_mutation_but_status_remains_available(self):
        """Concurrent callers cannot sync/discard a live job or erase its candidate."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            args = ["--db", str(database), "--work-dir", str(work), "--operation", job.operation_id]
            with stage.job_lock(job):
                code, output, error = self._run(["prune", "status", *args])
                self.assertEqual(code, 0, error)
                self.assertIn("state: ready", output)
                code, _, error = self._run(["prune", "update", *args])
                self.assertEqual(code, 4)
                self.assertIn("already running", error)
            self.assertTrue(job.candidate.exists())

    def test_source_locks_exclude_cross_job_cutover_and_snapshot_races(self):
        """Independent jobs share source read authority and serialize all source renames."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            first = self._job(database, work)
            second = self._job(database, work)
            with stage.source_lock(database, exclusive=False), stage.job_lock(second):
                with self.assertRaisesRegex(PruneError, "another staged source"):
                    staged_install.install_job(second, 30, progress=lambda _: None)
            with stage.source_lock(database, exclusive=True), stage.job_lock(first):
                with self.assertRaisesRegex(PruneError, "another staged source"):
                    stage.update_job(first, 30, progress=lambda _: None)
            self.assertEqual(len(self._ids(database)), 4)
            self.assertEqual(len(self._ids(first.candidate)), 3)
            self.assertEqual(len(self._ids(second.candidate)), 3)

    def test_data_only_migration_change_is_not_acknowledged_by_timestamp_sync(self):
        """Do not copy a new migration receipt while leaving unchanged-stamp payloads unmigrated."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            with closing(sqlite3.connect(database)) as source:
                source.execute("CREATE TABLE __drizzle_migrations(id INTEGER PRIMARY KEY, hash TEXT)")
                source.execute("INSERT INTO __drizzle_migrations VALUES(1,'old')")
                source.commit()
            job = self._job(database, work)
            with closing(sqlite3.connect(database)) as source:
                source.execute("INSERT INTO __drizzle_migrations VALUES(2,'new')")
                source.commit()
            with stage.job_lock(job), self.assertRaisesRegex(PruneError, "schema changed"):
                stage.update_job(job, 30, progress=lambda _: None)
            with closing(sqlite3.connect(job.candidate)) as candidate:
                self.assertEqual(candidate.execute("SELECT count(*) FROM __drizzle_migrations").fetchone(), (1,))

    def test_project_scoped_prune_still_catches_up_other_projects_before_whole_file_cutover(self):
        """The retention scope must not discard newer work outside that project."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work, project_id="a")
            with closing(sqlite3.connect(database)) as source:
                source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='b-1'")
                source.execute("UPDATE message SET payload=? WHERE session_id='b-1'", (b"other project work",))
                source.commit()
            with stage.job_lock(job):
                stage.update_job(job, 30, progress=lambda _: None)
                staged_install.install_job(job, 30, progress=lambda _: None)
            with closing(sqlite3.connect(database)) as source:
                self.assertEqual(source.execute("SELECT payload FROM message WHERE session_id='b-1'").fetchone(), (b"other project work",))

    def test_fractional_hash_budget_is_not_rounded_up_to_a_second(self):
        """Honor the same explicit fractional timeout contract as native prune."""
        with patch.object(stage.time, "monotonic", side_effect=(10.0, 11.0)):
            budget = stage.Budget(0.00001)
            with self.assertRaisesRegex(PruneError, "timed out"):
                budget.digest(Path("/not-created.sqlite"))

    def test_unsealed_image_cannot_acknowledge_an_unexpected_session_set(self):
        """A resumed seal validates committed selection/cursor membership, not just SQLite health."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            stage.write_meta(job, {**stage.read_meta(job), "phase": "sealing"})
            fixtures.SessionPruneTests._insert_session(job.candidate, "unexpected", "a", 1, b"unexpected")
            with stage.job_lock(job), self.assertRaisesRegex(PruneError, "session set differs"):
                stage.seal_job(job, 30, progress=lambda _: None)
            self.assertEqual(len(self._ids(database)), 4)

    def test_cutover_rejects_new_unrecorded_sidecars_before_promotion(self):
        """Never apply an old/new WAL to the differently laid-out pruned database."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            job = self._job(database, work)
            def unexpected(index):
                intent = staged_install.read_intent(job)
                if index == len(intent["files"]):
                    Path(str(database) + "-wal").write_bytes(b"unexpected sidecar")
            with stage.job_lock(job), self.assertRaisesRegex(PruneError, "sidecars appeared"):
                staged_install.install_job(job, 30, progress=lambda _: None, after_move=unexpected)
            self.assertFalse(database.exists())
            self.assertEqual(Path(str(database) + "-wal").read_bytes(), b"unexpected sidecar")
            self.assertTrue((staged_install.cutover_directory(job) / "original" / "original.db").exists())

    def test_recorded_canonical_source_supports_alias_selection_and_missing_main(self):
        """Exact job selection resolves initial aliases while interrupted cutover remains recoverable."""
        with tempfile.TemporaryDirectory(dir=_ROOT) as directory:
            database, work = self._fixture(Path(directory))
            alias = Path(directory) / "alias.db"
            alias.symlink_to(database)
            job = self._job(alias, work)
            self.assertEqual(stage.load_job(str(alias), str(work), job.operation_id).target, database)
            with stage.job_lock(job), self.assertRaises(OSError):
                staged_install.install_job(job, 30, progress=lambda _: None, after_move=lambda _: (_ for _ in ()).throw(OSError("stop")))
            self.assertFalse(database.exists())
            loaded = stage.load_job(str(alias), str(work), job.operation_id)
            with stage.job_lock(loaded):
                staged_install.rollback_job(loaded, 30, progress=lambda _: None)
            self.assertEqual(len(self._ids(database)), 4)

    def test_rollback_checks_all_sidecars_before_restoring_the_original_main(self):
        """A new sidecar must never be paired with the retained original by rollback."""
        for suffix in ("-wal", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(dir=_ROOT) as directory:
                database, work = self._fixture(Path(directory))
                job = self._job(database, work)
                def unexpected(index):
                    if index == len(staged_install.read_intent(job)["files"]):
                        Path(str(database) + suffix).write_bytes(b"unexpected sidecar")
                with stage.job_lock(job), self.assertRaisesRegex(PruneError, "sidecars appeared"):
                    staged_install.install_job(job, 30, progress=lambda _: None, after_move=unexpected)
                original = staged_install.cutover_directory(job) / "original" / "original.db"
                before = original.read_bytes()
                with stage.job_lock(job), self.assertRaises(PruneError):
                    staged_install.rollback_job(job, 30, progress=lambda _: None)
                self.assertFalse(database.exists())
                self.assertEqual(original.read_bytes(), before)
                self.assertEqual(Path(str(database) + suffix).read_bytes(), b"unexpected sidecar")

    def test_closed_cli_grammar_and_native_api_cannot_accidentally_prune_the_source(self):
        """Parse staged actions without I/O and reject staged requests at native mutation APIs."""
        arguments = ["prune", "--oldest", "1", "--staged", "--work-dir", "/tmp/opencode-db-v1", "--yes"]
        with patch.object(sqlite3, "connect", side_effect=AssertionError("parser opened SQLite")):
            request = cli.parse_command(arguments)
        self.assertTrue(request.staged)
        self.assertEqual(request.timeout_seconds, 86400)
        with self.assertRaises(PruneError):
            stage.plan_prune(request)
        for args in (
            ["prune", "update"],
            ["prune", "update", "--operation", "wrong"],
            ["prune", "status", "--operation", "prune-20261102T000000Z-" + "a" * 24, "--install"],
            ["prune", "--oldest", "1", "--progress"],
        ):
            with self.subTest(args=args), self.assertRaises(cli.CliUsageError):
                cli.parse_command(args)


if __name__ == "__main__":
    unittest.main()
