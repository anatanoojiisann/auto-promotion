"""Offline lock, snapshot and once-only recovery regression tests."""

import json
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable, export_snapshot
from auto_promotion.execution_channels import binding
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue
from auto_promotion.routing import route_for
from auto_promotion.target_checks import TargetChecks


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.store.lock_timeout = 0.01
        self.store.lock_retry_delays = (0.02, 0.04, 0.08)

    def test_write_waits_for_lock_release_and_runs_body_once(self):
        locked, release = threading.Event(), threading.Event()

        def holder():
            with closing(sqlite3.connect(self.store.path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(locked.wait(2))
        timer = threading.Timer(0.06, release.set)
        timer.start()
        self.addCleanup(timer.join)
        calls = []
        with self.store.connect(write=True) as db:
            calls.append(True)
            db.execute("INSERT INTO meta VALUES('recovered','1')")
        self.assertEqual(calls, [True])
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='recovered'")["value"], "1"
        )

    def test_exhausted_lock_is_bounded_without_entering_body(self):
        db = sqlite3.connect(self.store.path)
        db.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.connect(write=True):
                    self.fail("A locked transaction body must not run")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            db.close()
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('after_release','1')")

    def test_transaction_body_is_never_retried(self):
        calls = []
        with self.assertRaises(sqlite3.OperationalError):
            with self.store.connect(write=True) as db:
                calls.append(True)
                db.execute("INSERT INTO meta VALUES('rolled_back','1')")
                raise sqlite3.OperationalError("database is locked")
        self.assertEqual(calls, [True])
        self.assertIsNone(self.store.one("SELECT value FROM meta WHERE key='rolled_back'"))

    def test_existing_binding_is_read_only_under_other_writer(self):
        expected = binding(self.engine)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(binding(self.engine), expected)

    def test_concurrent_binding_initialization_is_stable(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            values = list(workers.map(lambda _: binding(self.engine), range(4)))
        self.assertEqual(len(set(values)), 1)

    def test_pool_recovers_from_preparation_lock_without_replaying_send(self):
        self.fixture.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        original = JobQueue.claim
        calls = []

        def claim(queue, *args):
            calls.append(True)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(queue, *args)

        with patch.object(JobQueue, "claim", claim):
            result = run_pool(self.engine, "simulation.x", "test", idle_exit=3, max_jobs=1)
        self.assertEqual(result["processed_jobs"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "confirmed")

    def test_pool_heartbeat_lock_failure_does_not_kill_consumer(self):
        self.fixture.ready()
        JobQueue(self.engine).sync()
        original = self.store.connect
        writes = []

        def connect(*args, **kwargs):
            if kwargs.get("write"):
                writes.append(True)
                if len(writes) == 2:  # after registration, at the first heartbeat
                    raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        with patch.object(self.store, "connect", side_effect=connect):
            result = run_pool(self.engine, "simulation.x", "test", idle_exit=3, max_jobs=1)
        self.assertEqual(result["processed_jobs"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)

    def test_receipt_settle_lock_does_not_kill_pool_or_reopen_attempt(self):
        self.fixture.ready()
        JobQueue(self.engine).sync()
        with patch.object(
            JobQueue, "settle", side_effect=sqlite3.OperationalError("database is locked")
        ):
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        JobQueue(self.engine).recover()
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "replacement"))
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "confirmed")

    def test_send_success_then_finish_failure_is_never_replayed(self):
        self.fixture.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        original = self.engine.adapters.call
        sends = []

        def call(account, operation, payload):
            if operation == "execute":
                sends.append(payload["item_id"])
            return original(account, operation, payload)

        with (
            patch.object(self.engine.adapters, "call", side_effect=call),
            patch.object(
                self.engine, "finish", side_effect=sqlite3.OperationalError("database is locked")
            ),
        ):
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.engine.clock = lambda: 1800000301
        self.engine.recover()
        queue.recover()
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertIsNone(queue.claim("simulation.x", "replacement"))
        self.assertEqual(self.engine.run_once()["processed"], [])
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)

    def test_snapshot_uses_one_read_version_during_concurrent_creation(self):
        _, account = self.fixture.ready()
        created = []

        def route(*args, **kwargs):
            if not created:
                created.append(True)
                self.fixture.ready(account=account)
            return route_for(*args, **kwargs)

        with patch("auto_promotion.cloud_control.route_for", side_effect=route):
            first = export_snapshot(self.engine)
        self.assertEqual(len(first["tasks"]), 1)
        self.assertEqual(len(first["jobs"]), 1)
        self.assertEqual(len(export_snapshot(self.engine)["jobs"]), 2)

    def test_unchanged_sync_does_not_advance_local_or_remote_revision(self):
        self.fixture.ready()
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="synthetic")
        with patch.object(cloud, "request", return_value={}) as request:
            cloud.sync(self.engine)
            previous = self.store.one("SELECT value FROM meta WHERE key='cloud_snapshot_revision'")
            request.reset_mock()
            for elapsed in (1, 60, 300):
                self.engine.clock = lambda elapsed=elapsed: 1800000000 + elapsed
                self.assertTrue(cloud.sync(self.engine)["unchanged"])
            request.assert_not_called()
            self.assertEqual(
                previous,
                self.store.one("SELECT value FROM meta WHERE key='cloud_snapshot_revision'"),
            )

    def test_historical_orphan_fails_closed_without_exporting_partial_snapshot(self):
        self.fixture.ready()
        # Reproduce an old/imported database with unenforced foreign keys.
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("DELETE FROM tasks")
        with self.assertRaisesRegex(CloudUnavailable, "CloudSnapshotInconsistent"):
            export_snapshot(self.engine)

    def test_negative_eligibility_is_account_identity_and_target_scoped(self):
        account = {
            "id": "synthetic-a",
            "backend_label": "a",
            "identity_hash": "identity-a",
            "platform": "telegram",
            "mode": "live",
        }
        payload = {"payload": {"target": "@group", "media": []}}
        check = TargetChecks(self.engine, account, "preflight", payload)
        check.save(check.failure("ChannelPrivateError"))
        self.assertFalse(check.read()["allowed"])
        for other in ({**account, "id": "synthetic-b"}, {**account, "identity_hash": "identity-b"}):
            self.assertIsNone(TargetChecks(self.engine, other, "preflight", payload).read())
        self.assertIsNone(
            TargetChecks(
                self.engine, account, "preflight", {"payload": {"target": "@different"}}
            ).read()
        )
        self.engine.clock = lambda: 1800000301
        self.assertIsNone(check.read())
        check.save({"allowed": True})
        self.assertIsNone(check.read())

    def test_transient_eligibility_failure_expires_and_floodwait_is_respected(self):
        account = {
            "id": "synthetic-a",
            "backend_label": "a",
            "platform": "telegram",
            "mode": "live",
        }
        check = TargetChecks(self.engine, account, "verify_join", {"target": "@group"})
        check.save(check.failure("ConnectionError"))
        self.assertEqual(check.read()["eligibility_retry_at"], 1800000030)
        self.engine.clock = lambda: 1800000031
        self.assertIsNone(check.read())
        check.save({"rate_limited": True, "retry_after_seconds": 900})
        self.assertEqual(check.read()["eligibility_retry_at"], 1800000931)

    def test_repeated_preflight_calls_are_deferred_then_read_again(self):
        _, account = self.fixture.ready()
        account.update(platform="telegram", mode="live", identity_hash="synthetic")
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET platform='telegram',mode='live',identity_hash='synthetic' WHERE id=?",
                (account["id"],),
            )
        payload = {"payload": {"target": "@denied", "media": []}}
        with patch.object(
            self.engine,
            "_call_unlocked",
            return_value={"allowed": False, "reason": "TargetWriteForbidden"},
        ) as call:
            self.engine.call(account, "preflight", payload)
            self.engine.call(account, "preflight", payload)
            self.assertEqual(call.call_count, 1)
            self.engine.clock = lambda: 1800000301
            call.return_value = {"allowed": True}
            self.assertTrue(self.engine.call(account, "preflight", payload)["allowed"])
            self.assertEqual(call.call_count, 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM telegram_target_checks")["n"], 0)

    def test_settle_defers_unattempted_job_until_target_recheck(self):
        _, account = self.fixture.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        job = queue.claim("simulation.x", "worker")
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET platform='telegram',mode='live' WHERE id=?", (account["id"],)
            )
        live = self.engine.account(account["id"])
        item = self.store.one("SELECT payload FROM items")
        check = TargetChecks(
            self.engine, live, "preflight", {"payload": json.loads(item["payload"])}
        )
        check.save(check.failure("UserNotParticipantError"))
        queue.settle(job)
        self.assertEqual(
            self.store.one("SELECT available_at FROM execution_jobs")["available_at"], 1800000300
        )
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
