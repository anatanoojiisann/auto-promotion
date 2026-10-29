"""Synthetic startup/host failures; never opens an existing account or business database."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_persistent_pools as fixtures
from fastapi.testclient import TestClient

from auto_promotion.api import create_app
from auto_promotion.pool_runtime import PoolScheduler, run_pool
from auto_promotion.store import Store


class StartupResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.db"

    def test_reopening_mature_store_needs_no_write_under_another_writer(self):
        Store(self.path)
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with (
                patch.object(Store, "lock_timeout", 0.01),
                patch.object(Store, "lock_retry_delays", (0.01,)),
            ):
                self.assertEqual(
                    Store(self.path).one("SELECT value FROM meta WHERE key='global_stop'")["value"],
                    "1",
                )

    def test_failed_schema_creation_rolls_back_every_schema_statement(self):
        original = sqlite3.connect

        class BrokenDDL(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("CREATE INDEX IF NOT EXISTS tasks_campaign"):
                    raise sqlite3.OperationalError("synthetic migration interruption")
                return super().execute(sql, *args)

        with patch(
            "auto_promotion.store.sqlite3.connect",
            side_effect=lambda *a, **kw: original(*a, **kw, factory=BrokenDDL),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError, "synthetic migration interruption"
            ):
                Store(self.path)
        with closing(original(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='accounts'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            Store(self.path).one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )

    def test_future_schema_revision_is_refused_without_overwriting_it(self):
        store = Store(self.path)
        with store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='2' WHERE key='store_schema_revision'")
        with self.assertRaisesRegex(ValueError, "UnsupportedStoreSchemaRevision"):
            Store(self.path)
        self.assertEqual(
            store.one("SELECT value FROM meta WHERE key='store_schema_revision'")["value"], "2"
        )

    def test_first_pool_registration_lock_is_recovered_without_execution_replay(self):
        fixture = fixtures.PersistentPoolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ready()
        original = fixture.store.connect
        writes = []

        def connect(*args, **kwargs):
            if kwargs.get("write"):
                writes.append(True)
                if len(writes) == 1:
                    raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        from auto_promotion.queue_store import JobQueue

        JobQueue(fixture.engine).sync()
        with patch.object(fixture.store, "connect", side_effect=connect):
            result = run_pool(fixture.engine, "simulation.x", "synthetic", idle_exit=3, max_jobs=1)
        self.assertEqual(result["processed_jobs"], 1)
        self.assertEqual(fixture.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)

    def test_parent_tick_and_error_audit_failures_do_not_kill_background_task(self):
        store = Store(self.path)
        second = threading.Event()
        calls = []
        original = Store.audit

        def tick(scheduler):
            calls.append(True)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            second.set()

        def audit(db, kind, *args, **kwargs):
            if kind == "worker.error":
                raise sqlite3.OperationalError("database is locked")
            return original(db, kind, *args, **kwargs)

        app = create_app(db_path=self.path, worker=True, report_worker=False, pooled_worker=True)
        with (
            patch.object(PoolScheduler, "tick", tick),
            patch.object(Store, "audit", side_effect=audit),
            patch("auto_promotion.api.WORKER_INTERVAL_SECONDS", 0.01, create=True),
        ):
            with TestClient(app) as client:
                self.assertTrue(second.wait(5), "failed error audit killed the scheduler")
                for _ in range(100):
                    health = client.get("/health").json()
                    if health["background"]["sender"].get("last_success_at"):
                        break
                    time.sleep(0.01)
                self.assertTrue(health["background"]["sender"]["alive"])
                self.assertEqual(health["background"]["sender"]["last_error"], "OperationalError")
                self.assertGreaterEqual(
                    health["background"]["sender"]["last_success_at"],
                    health["background"]["sender"]["last_error_at"],
                )
                self.assertNotIn(
                    "database is locked", str(health["background"]["sender"]["last_error_details"])
                )
        self.assertEqual(store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1")
        self.assertFalse(store.rows("SELECT * FROM attempts"))

    def _background_sqlite_error(self, *, reject_audit=False):
        from auto_promotion.interactions import Interactions
        from auto_promotion.meta_browser import MetaBrowser

        completed, calls = threading.Event(), []
        diagnostics = []
        original = Store.audit_best_effort
        original_audit = Store.audit

        def capture(store, kind, details, **kwargs):
            result = original(store, kind, details, **kwargs)
            if kind == "statistics.error":
                diagnostics.append((details.copy(), result))
            return result

        def audit(db, kind, *args, **kwargs):
            if reject_audit and kind == "statistics.error":
                raise sqlite3.OperationalError("PRIVATE_AUDIT_LOCK")
            return original_audit(db, kind, *args, **kwargs)

        def schedule(_):
            calls.append(True)
            if len(calls) == 1:
                error = sqlite3.OperationalError("PRIVATE_SECRET_DO_NOT_LOG")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                error.sqlite_errorname = "SQLITE_BUSY"
                raise error
            completed.set()

        app = create_app(db_path=self.path, worker=True, report_worker=False, pooled_worker=True)
        with (
            patch.object(Interactions, "stages", ()),
            patch.object(Interactions, "schedule", schedule),
            patch.object(MetaBrowser, "sync", return_value=None),
            patch.object(PoolScheduler, "tick", return_value=None),
            patch.object(Store, "audit_best_effort", capture),
            patch.object(Store, "audit", side_effect=audit),
            patch("auto_promotion.api.WORKER_INTERVAL_SECONDS", 0.01),
        ):
            with TestClient(app) as client:
                self.assertTrue(completed.wait(2))
                for _ in range(100):
                    record = client.get("/health").json()["background"]["reports"]["hf_schedule"]
                    if record.get("last_success_at"):
                        break
                    time.sleep(0.01)
                self.assertEqual(record["last_error_details"]["sqlite_errorname"], "SQLITE_BUSY")
                self.assertGreaterEqual(record["last_success_at"], record["last_error_at"])
                self.assertTrue(record["alive"])
                self.assertNotIn("PRIVATE_SECRET", str(record))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0][0]["sqlite_errorname"], "SQLITE_BUSY")
        self.assertNotIn("PRIVATE", str(diagnostics))
        store = Store(self.path)
        self.assertEqual(store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1")
        self.assertFalse(store.rows("SELECT * FROM attempts"))
        # The concurrent sender may hold the write lock: diagnostic persistence is
        # checked separately under deterministic unlocked/locked conditions below.
        return diagnostics[0][1]

    def test_background_sqlite_error_keeps_safe_code_and_later_success(self):
        self._background_sqlite_error()

    def test_background_report_recovers_when_diagnostic_audit_fails(self):
        self.assertFalse(self._background_sqlite_error(reject_audit=True))

    def test_diagnostic_audit_persists_when_unlocked(self):
        store = Store(self.path)
        details = {"reason": "report_cycle_failed", "sqlite_errorname": "SQLITE_BUSY"}
        self.assertTrue(store.audit_best_effort("statistics.error", details, now=123))
        row = store.one("SELECT at,details FROM audit WHERE kind='statistics.error'")
        self.assertEqual(row["at"], 123)
        self.assertEqual(json.loads(row["details"]), details)

    def test_locked_diagnostic_audit_does_not_wait_retry_or_change_state(self):
        store = Store(self.path)
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with (
                patch("auto_promotion.store.time.sleep") as sleep,
                patch("auto_promotion.store.sqlite3.connect", wraps=sqlite3.connect) as connect,
            ):
                self.assertFalse(store.audit_best_effort("statistics.error", {"reason": "busy"}))
            sleep.assert_not_called()
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(connect.call_args.kwargs["timeout"], 0)
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.assertEqual(store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1")
        self.assertFalse(store.rows("SELECT * FROM attempts"))

    def test_required_audit_failure_rolls_back_the_protected_transaction(self):
        store = Store(self.path)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "required audit unavailable"):
            with store.connect(write=True) as db:
                db.execute(
                    "CREATE TEMP TRIGGER reject_audit BEFORE INSERT ON audit "
                    "BEGIN SELECT RAISE(ABORT, 'required audit unavailable'); END"
                )
                db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
                store.audit(db, "synthetic.required", {"reason": "test_only"})
        self.assertEqual(store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1")
        self.assertFalse(store.rows("SELECT * FROM audit"))

    def test_health_freezes_runtime_fingerprint_and_tracks_actual_task_liveness(self):
        app = create_app(db_path=self.path, worker=False, report_worker=False)
        with TestClient(app) as client:
            first = client.get("/health").json()
            time.sleep(0.002)
            second = client.get("/health").json()
        self.assertEqual(first["runtime"], second["runtime"])
        self.assertRegex(first["runtime"]["source_fingerprint"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            first["background"]["sender"], {"enabled": False, "alive": False, "last_error": None}
        )
