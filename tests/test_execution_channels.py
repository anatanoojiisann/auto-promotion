import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.execution_channels import (
    activate_local,
    binding,
    check_standby,
    configure_independent,
    guard_local,
    read_state,
)
from auto_promotion.pool_runtime import PoolScheduler, run_pool
from auto_promotion.queue_store import JobQueue, verify_job_claim


class ExecutionChannelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready()
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('execution_channel_policy','cloud_then_local')")
        self.cloud = CloudControl("http://127.0.0.1:8791", node_id="test", node_token="test")
        self.calls = []
        self.error = "CloudWriteQuotaExceeded"
        self.patch = patch.object(CloudControl, "configured", return_value=self.cloud)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.request = patch.object(self.cloud, "request", side_effect=self.respond).start()
        self.addCleanup(patch.stopall)
        self.scheduler = PoolScheduler(self.engine)
        self.scheduler.enable()
        JobQueue(self.engine).sync()

    def respond(self, path, payload=None, **kwargs):
        self.calls.append(path)
        if path == "/v1/heartbeat" and self.error:
            raise CloudUnavailable(self.error, retry_after_seconds=3600)
        if path == "/v1/standby-check":
            return {
                "allowed": True,
                "node_id": "test",
                "store_id": binding(self.engine),
                "settings": self.engine.execution_settings(),
                "attempts": [],
                "account": {
                    "limits": {
                        "daily": self.account["daily_limit"],
                        "hourly": self.account["hourly_limit"],
                    },
                    "min_interval": self.account["min_interval"],
                },
            }
        return {}

    def activate(self):
        self.assertTrue(self.scheduler._control_ready())
        self.assertEqual(read_state(self.store)["active"], "local")

    def test_quota_switch_executes_same_item_once_without_cloud_claim(self):
        self.activate()
        before = self.engine.task(self.task["id"])["manifest_hash"]
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertEqual(self.engine.task(self.task["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.engine.task(self.task["id"])["manifest_hash"], before)
        self.assertNotIn("/v1/claim", self.calls)
        self.assertNotIn("/v1/authorize", self.calls)
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")["value"],
            "1",
        )
        self.assertEqual(JobQueue(self.engine).pools()["channel"]["active"], "local")
        JobQueue(self.engine).sync()
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "replacement"))

    def test_online_is_preferred_while_refreshing_standby_checkpoint(self):
        self.error = None
        self.assertTrue(self.scheduler._control_ready())
        self.assertEqual(read_state(self.store)["active"], "cloud")
        self.assertIn("/v1/standby-check", self.calls)

    def test_other_errors_never_trigger_fallback(self):
        for error in (
            "CloudReadQuotaExceeded",
            "CloudTransportUnavailable",
            "Unauthorized",
            "QuotaExceeded",
            "CloudRuntimeUnavailable",
        ):
            with self.subTest(error=error):
                with self.store.connect(write=True) as db:
                    db.execute("DELETE FROM meta WHERE key='cloud_retry_after'")
                self.calls.clear()
                self.error = error
                self.assertFalse(PoolScheduler(self.engine)._control_ready())
                self.assertEqual(read_state(self.store)["active"], "cloud")
                self.assertNotIn("/v1/standby-check", self.calls)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_restart_preserves_quota_channel_without_resetting_items(self):
        self.activate()
        state = read_state(self.store)
        self.calls.clear()
        self.assertTrue(PoolScheduler(self.engine)._control_ready())
        self.assertEqual(read_state(self.store), state)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_first_checkpoint_wait_is_durable_without_repeated_quota_requests(self):
        def blocked(path, payload=None, **kwargs):
            self.calls.append(path)
            if path == "/v1/standby-check":
                raise CloudUnavailable("LocalChannelCheckpointRequired")
            raise CloudUnavailable("CloudWriteQuotaExceeded", retry_after_seconds=3600)

        self.request.side_effect = blocked
        self.assertFalse(self.scheduler._control_ready())
        self.assertFalse(self.scheduler._control_ready())
        self.assertFalse(PoolScheduler(self.engine)._control_ready())
        self.assertEqual(self.calls, ["/v1/heartbeat", "/v1/standby-check"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_unknown_result_is_never_replayed_on_switch_or_restart(self):
        self.activate()
        adapter = fixtures.UnknownAdapter()
        self.engine.adapters.overrides["x"] = adapter
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertEqual(self.engine.task(self.task["id"])["summary"], {"unknown": 1})
        self.assertTrue(PoolScheduler(self.engine)._control_ready())
        JobQueue(self.engine).sync()
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "replacement"))
        self.assertEqual(adapter.calls, 1)

    def test_expired_generation_or_forged_local_context_is_rejected(self):
        self.activate()
        job = JobQueue(self.engine).claim("simulation.x", "owner")
        state = read_state(self.store)
        context = {
            "id": job["id"],
            "owner": "owner",
            "channel": "local",
            **{k: state[k] for k in ("node_id", "store_id", "generation")},
        }
        request = {
            "execution_job": context,
            "account_id": job["account_id"],
            "operation": "execute",
            "payload": {"item_id": job["reference_id"]},
        }
        with self.store.connect() as db:
            verify_job_claim(db, request)
        context["generation"] = "forged"
        with self.store.connect() as db:
            with self.assertRaisesRegex(ValueError, "CloudExecutionClaimRequired"):
                verify_job_claim(db, request)
        context["generation"] = state["generation"]
        self.engine.clock = lambda: state["until"] + 1
        with self.assertRaisesRegex(CloudUnavailable, "ClaimExpired"):
            guard_local(self.engine, context, self.cloud)

    def test_recovery_syncs_before_returning_cloud_and_does_not_replay(self):
        self.activate()
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        state = read_state(self.store)
        self.engine.clock = lambda: state["until"] + 1
        self.error = None
        self.assertTrue(self.scheduler._control_ready())
        self.assertEqual(read_state(self.store)["active"], "cloud")
        self.assertIn("/v1/sync", self.calls)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "confirmed")

    def test_incomplete_remote_ledger_or_looser_limits_blocks_takeover(self):
        base = self.respond("/v1/standby-check")
        for response in (
            {
                **base,
                "attempts": [
                    {
                        "kind": "send",
                        "attempt_id": "unmirrored",
                        "reference_id": "absent",
                        "account_id": self.account["id"],
                    }
                ],
            },
            {**base, "historical_attempts": ["unmirrored"]},
            {**base, "settings": {**base["settings"], "global_limit": {"daily": 1, "hourly": 1}}},
            {**base, "store_id": "different-db"},
        ):
            with self.subTest(response=response):
                self.request.side_effect = None
                self.request.return_value = response
                with self.assertRaises(CloudUnavailable):
                    check_standby(self.engine, self.cloud)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_outstanding_cloud_receipt_cannot_be_replayed_locally(self):
        self.activate()
        job = self.store.one("SELECT * FROM execution_jobs")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,updated_at) VALUES(?,?,?)",
                (job["id"], "{}", self.engine.clock()),
            )
        with self.assertRaisesRegex(CloudUnavailable, "ReceiptPending"):
            check_standby(self.engine, self.cloud, job)

    def test_global_stop_remains_effective_in_local_channel(self):
        self.activate()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "owner"))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_no_fallback_after_utc_reset_or_without_explicit_policy(self):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM meta WHERE key='execution_channel_policy'")
        self.assertFalse(
            activate_local(
                self.engine,
                self.cloud,
                {"reason": "CloudWriteQuotaExceeded", "until": self.engine.clock() + 100},
            )
        )
        self.assertEqual(read_state(self.store)["active"], "cloud")

    def test_failed_recovery_does_not_unlock_cloud_workers(self):
        self.activate()
        state = read_state(self.store)
        self.engine.clock = lambda: state["until"] + 1
        self.error = "CloudTransportUnavailable"
        self.assertFalse(self.scheduler._control_ready())
        self.assertEqual(read_state(self.store)["active"], "local")
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_independent_local_never_configures_or_contacts_cloud(self):
        configure_independent(self.engine, [self.account["id"]], self.engine.clock() + 3600)
        self.calls.clear()
        with patch.object(
            CloudControl, "configured", side_effect=AssertionError("Cloud must not be loaded")
        ):
            scheduler = PoolScheduler(self.engine)
            self.assertIsNone(scheduler.cloud)
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.engine.task(self.task["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")["value"],
            "1",
        )

    def test_independent_scope_does_not_run_other_accounts(self):
        from auto_promotion.models import AccountInput

        other = self.engine.add_account(
            AccountInput(
                platform="x",
                label="other",
                backend_label="other",
                mode="simulation",
                min_interval=0,
            )
        )
        other_task, _ = self.fixture.ready(account=other)
        configure_independent(self.engine, [self.account["id"]], self.engine.clock() + 3600)
        JobQueue(self.engine).sync()
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "extra"))
        self.assertEqual(self.engine.task(other_task["id"])["summary"], {"not_attempted": 1})

    def test_independent_local_respects_stop_and_pending_receipts(self):
        configure_independent(self.engine, [self.account["id"]], self.engine.clock() + 3600)
        job = self.store.one("SELECT * FROM execution_jobs")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,updated_at) VALUES(?,?,?)",
                (job["id"], "{}", self.engine.clock()),
            )
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "extra"))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM cloud_receipt_outbox")
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "extra"))

    def test_independent_unknown_stays_once_only_after_restart(self):
        configure_independent(self.engine, [self.account["id"]], self.engine.clock() + 3600)
        self.engine.adapters.overrides["x"] = fixtures.UnknownAdapter()
        run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        PoolScheduler(self.engine)
        JobQueue(self.engine).sync()
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "extra"))
        self.assertEqual(self.engine.task(self.task["id"])["summary"], {"unknown": 1})

    def crossday_task(self, *, start=True):
        from datetime import datetime, timezone

        from auto_promotion.models import ItemInput, TaskInput

        now = self.engine.clock()

        def iso(at):
            return datetime.fromtimestamp(at, timezone.utc).isoformat()

        task = self.engine.create_task(
            TaskInput(
                name="explicit multi-day continuation",
                account_ids=[self.account["id"]],
                policy={"starts_at": iso(now), "ends_at": iso(now + 3 * 86400), "min_interval": 0},
            )
        )
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target="https://x.com/example/status/9000001",
                    text="cross-day reply",
                    scheduled_at=iso(now + 90000),
                )
            ],
        )
        if start:
            self.engine.start_task(task["id"], source="test_explicit_start")
        return self.engine.task(task["id"])

    def test_crossday_continuation_preserves_unknown_after_restart(self):
        task = self.crossday_task()
        now = self.engine.clock()
        configure_independent(
            self.engine,
            [self.account["id"]],
            now + 3 * 86400,
            through_task_ids=[task["id"]],
        )
        self.engine.clock = lambda: now + 90001
        self.engine.adapters.overrides["x"] = fixtures.UnknownAdapter()
        self.calls.clear()
        JobQueue(self.engine).sync()
        run_pool(self.engine, "simulation.x", "test", max_jobs=1, idle_exit=0)
        PoolScheduler(self.engine)
        JobQueue(self.engine).sync()
        self.assertEqual(self.engine.task(task["id"])["summary"], {"unknown": 1})
        self.assertEqual(self.engine.task(task["id"])["manifest_hash"], task["manifest_hash"])
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "replacement"))
        self.assertEqual(
            self.store.one("SELECT count(*) n FROM attempts WHERE task_id=?", (task["id"],))["n"], 1
        )
        self.assertEqual(self.calls, [])

    def test_crossday_authority_rejects_draft_and_excessive_window(self):
        task = self.crossday_task(start=False)
        until = self.engine.clock() + 3 * 86400
        with self.assertRaisesRegex(ValueError, "InvalidLocalContinuationTask"):
            configure_independent(
                self.engine, [self.account["id"]], until, through_task_ids=[task["id"]]
            )
        self.engine.start_task(task["id"], source="test_explicit_start")
        with self.assertRaisesRegex(ValueError, "ExceedsTaskWindow"):
            configure_independent(
                self.engine, [self.account["id"]], until + 1, through_task_ids=[task["id"]]
            )
        with self.assertRaisesRegex(ValueError, "InvalidLocalChannelScope"):
            configure_independent(self.engine, [self.account["id"]], until)

    def test_crossday_authority_keeps_stop_and_expiry_guards(self):
        task = self.crossday_task()
        now = self.engine.clock()
        configure_independent(
            self.engine,
            [self.account["id"]],
            now + 3 * 86400,
            through_task_ids=[task["id"]],
        )
        JobQueue(self.engine).sync()
        self.engine.clock = lambda: now + 90001
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "stopped"))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        self.engine.clock = lambda: now + 3 * 86400
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "expired"))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_quota_failover_for_independent_policy_needs_no_server_checkpoint(self):
        until = self.engine.clock() + 3600
        configure_independent(self.engine, [self.account["id"]], until)
        from auto_promotion.execution_channels import use_cloud

        use_cloud(self.engine)
        self.calls.clear()
        self.assertTrue(
            activate_local(
                self.engine, self.cloud, {"reason": "CloudWriteQuotaExceeded", "until": until}
            )
        )
        self.assertEqual(self.calls, [])

    def test_independent_policy_ignores_old_server_initialization_wait(self):
        configure_independent(self.engine, [self.account["id"]], self.engine.clock() + 3600)
        from auto_promotion.execution_channels import use_cloud

        use_cloud(self.engine)
        self.scheduler._cloud_health(
            "LocalChannelCheckpointRequired", retry_at=self.engine.clock() + 3600
        )
        self.calls.clear()
        self.assertTrue(
            self.scheduler._try_local(
                {"reason": "CloudWriteQuotaExceeded", "until": self.engine.clock() + 3600}
            )
        )
        self.assertEqual(self.calls, [])

    def test_live_provider_rechecks_independent_claim_without_server(self):
        import os
        import tempfile
        import time
        from datetime import datetime, timezone
        from pathlib import Path

        from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
        from auto_promotion.backends.common import verify_claim
        from auto_promotion.core import Engine
        from auto_promotion.models import AccountInput, ItemInput, TaskInput
        from auto_promotion.queue_store import job_scope
        from auto_promotion.store import Store

        def iso(at):
            return datetime.fromtimestamp(at, timezone.utc).isoformat()

        with tempfile.TemporaryDirectory(dir=Path(self.store.path).parent) as folder:
            store = Store(Path(folder) / "native.db")
            registry = AdapterRegistry(Path(folder), overrides={"telegram": SimulationAdapter()})
            engine = Engine(store, registry, live_enabled=True)
            a = engine.add_account(
                AccountInput(platform="telegram", label="local", backend_label="local")
            )
            engine.check_account(a["id"])
            now = time.time()
            t = engine.create_task(
                TaskInput(
                    name="native local",
                    account_ids=[a["id"]],
                    policy={"starts_at": iso(now - 60), "ends_at": iso(now + 3600)},
                )
            )
            engine.start_task(t["id"], sending=False)
            engine.set_items(
                t["id"],
                [
                    ItemInput(
                        account_id=a["id"],
                        action="send_message",
                        target="@public_group",
                        text="synthetic",
                        scheduled_at=iso(now),
                    )
                ],
            )
            engine.prepare(t["id"])
            engine.start_task(t["id"])
            engine.set_stop(False)
            configure_independent(engine, [a["id"]], now + 3600)
            queue = JobQueue(engine)
            queue.sync()
            job = queue.claim("telegram.mtproto", "native-owner")
            state = read_state(store)
            context = {
                "id": job["id"],
                "owner": job["owner"],
                "channel": "local",
                **{k: state[k] for k in ("node_id", "store_id", "generation")},
            }
            with (
                job_scope(job),
                patch.dict(
                    os.environ,
                    {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": "auto-promotion"},
                ),
            ):
                payload = engine._claim(job["reference_id"], {"allowed": True})
            self.assertIsNotNone(payload)
            request = {
                "platform": "telegram",
                "account": "local",
                "account_id": a["id"],
                "database": store.path,
                "operation": "execute",
                "payload": payload,
                "execution_job": context,
            }
            with (
                patch.dict(
                    os.environ,
                    {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": "auto-promotion"},
                ),
                patch.object(
                    CloudControl, "configured", side_effect=AssertionError("No server dependency")
                ),
            ):
                verify_claim(request)


if __name__ == "__main__":
    unittest.main()
