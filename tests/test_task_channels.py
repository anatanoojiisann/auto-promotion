import sqlite3
import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.execution_channels import configure_independent, read_state
from auto_promotion.models import AccountInput
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.store import Store
from auto_promotion.task_channels import TaskChannelRouter, guard_context


class TaskChannelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.now = 1800000000
        self.engine.clock = lambda: self.now
        self.queue = JobQueue(self.engine)
        self.cloud_patch = patch.object(CloudControl, "configured", return_value=None)
        self.cloud_config = self.cloud_patch.start()
        self.addCleanup(self.cloud_patch.stop)
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")

    def claim(self, task):
        self.queue.sync(task["id"])
        job = self.queue.claim("simulation.x", "owner")
        self.assertIsNotNone(job)
        return job

    def fake_cloud(self, *, lose_response=False):
        cloud = CloudControl("http://127.0.0.1:8791", node_id="node", node_token="synthetic")
        remote = {}
        self.cloud_config.return_value = cloud

        def respond(path, payload=None, **kwargs):
            if path == "/v1/claim":
                row = self.store.one(
                    "SELECT * FROM execution_jobs WHERE id=?", (payload["expected_id"],)
                )
                return {
                    "job": {
                        **row,
                        "lease_token": "synthetic",
                        "lease_until": self.now + 180,
                        "renew_after_seconds": 120,
                    }
                }
            if path == "/v1/authorize":
                return {"allowed": True}
            if path == "/v1/account-channel/status":
                return {"channel": remote.get(payload["account_id"])}
            if path == "/v1/account-channel":
                state = {
                    key: payload[key]
                    for key in ("generation", "channel", "account_id", "node_id", "task_id")
                }
                state.update(until_at=payload["until"], store_id=payload["local_store_id"])
                remote[payload["account_id"]] = state
                if lose_response:
                    raise CloudUnavailable("CloudTransportUnavailable")
                return {"channel": state}
            return {}

        request = patch.object(cloud, "request", side_effect=respond).start()
        sync = patch.object(cloud, "sync", return_value={"changed_records": 1}).start()
        self.addCleanup(patch.stopall)
        return cloud, remote, request, sync

    def test_explicit_local_executes_once_without_server_initialization(self):
        task, _ = self.fixture.ready(channel="local")
        before = self.engine.task(task["id"])["manifest_hash"]
        self.queue.sync()
        run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        result = self.engine.task(task["id"])
        self.assertEqual(result["summary"], {"confirmed": 1})
        self.assertEqual(result["manifest_hash"], before)
        self.assertEqual(result["channel"]["requested"], "local")
        self.assertEqual(result["channel"]["accounts"][0]["state"], "ready")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertIsNone(self.queue.claim("simulation.x", "replacement"))
        self.assertEqual(read_state(self.store), {"active": "cloud"})

    def test_cloud_task_is_claimable_outside_legacy_local_account_scope(self):
        old, local_account = self.fixture.ready()
        online_account = self.engine.add_account(
            AccountInput(
                platform="x",
                label="online",
                backend_label="online",
                mode="simulation",
                min_interval=0,
            )
        )
        online, _ = self.fixture.ready(account=online_account, channel="cloud", target_id=999)
        configure_independent(self.engine, [local_account["id"]], self.now + 4000)
        self.queue.sync()
        # Deliberately scope the consumer to the online task, preserving old work.
        with patch.dict("os.environ", {"AP_EXECUTOR_TASK_IDS": online["id"]}):
            job = self.queue.claim("simulation.x", "online-owner")
        self.assertEqual(job["task_id"], online["id"])
        cloud, _, _, sync = self.fake_cloud()
        context, selected = TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(context["channel"], "cloud")
        self.assertIs(selected, cloud)
        sync.assert_called_once_with(self.engine, task_ids=[online["id"]])
        self.assertEqual(read_state(self.store)["account_ids"], [local_account["id"]])
        self.assertEqual(self.engine.task(old["id"])["summary"], {"not_attempted": 1})

    def test_lost_handoff_response_fences_both_channels_and_reconciles_same_generation(self):
        task, account = self.fixture.ready(channel="cloud")
        _, _, request, _ = self.fake_cloud(lose_response=True)
        job = self.claim(task)
        router = TaskChannelRouter(self.engine)
        with self.assertRaisesRegex(CloudUnavailable, "CloudTransportUnavailable"):
            router.prepare(job)
        state = self.store.one("SELECT * FROM account_task_channels")
        self.assertEqual(state["state"], "handoff")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.queue.settle(job, CloudUnavailable("CloudTransportUnavailable"))
        other, _ = self.fixture.ready(account=account, channel="local", target_id=999)
        self.queue.sync(other["id"])
        self.assertIsNone(self.queue.claim("simulation.x", "too-soon"))
        self.now += 31
        next_job = self.queue.claim("simulation.x", "recover")
        self.assertEqual(next_job["task_id"], task["id"])
        context, _ = router.prepare(next_job)
        self.assertEqual(context["generation"], state["generation"])
        transitions = [c for c in request.call_args_list if c.args[0] == "/v1/account-channel"]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(self.store.one("SELECT * FROM account_task_channels")["state"], "ready")

    def test_cloud_unavailable_never_sends_explicit_online_task_locally(self):
        task, _ = self.fixture.ready(channel="cloud")
        self.queue.sync()
        run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        self.assertEqual(self.engine.task(task["id"])["summary"], {"not_attempted": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(read_state(self.store), {"active": "cloud"})

    def test_online_and_legacy_local_accounts_execute_independently_through_the_queue(self):
        local_task, local_account = self.fixture.ready()
        online_account = self.engine.add_account(
            AccountInput(
                platform="x",
                label="online",
                backend_label="online",
                mode="simulation",
                min_interval=0,
            )
        )
        online_task, _ = self.fixture.ready(account=online_account, channel="cloud", target_id=999)
        original = self.engine.task(local_task["id"])["manifest_hash"]
        configure_independent(self.engine, [local_account["id"]], self.now + 4000)
        self.queue.sync()
        _, _, request, _ = self.fake_cloud()
        with patch.dict("os.environ", {"AP_EXECUTOR_TASK_IDS": online_task["id"]}):
            run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        self.assertEqual(self.engine.task(online_task["id"])["summary"], {"confirmed": 1})
        self.assertTrue(any(c.args[0] == "/v1/authorize" for c in request.call_args_list))
        with patch.dict("os.environ", {"AP_EXECUTOR_TASK_IDS": local_task["id"]}):
            run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        self.assertEqual(self.engine.task(local_task["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.engine.task(local_task["id"])["manifest_hash"], original)
        self.assertEqual(read_state(self.store)["account_ids"], [local_account["id"]])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 2)

    def test_pending_receipts_prevent_handoff(self):
        task, _ = self.fixture.ready(channel="cloud")
        _, _, request, _ = self.fake_cloud()
        job = self.claim(task)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,updated_at) VALUES(?,?,?)",
                (job["id"], "{}", self.now),
            )
        with self.assertRaisesRegex(CloudUnavailable, "LocalChannelReceiptPending"):
            TaskChannelRouter(self.engine).prepare(job)
        request.assert_not_called()

    def test_stale_generation_and_old_pool_attempt_are_rejected(self):
        task, _ = self.fixture.ready(channel="local")
        job = self.claim(task)
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_task_channels SET state='handoff'")
        with self.assertRaisesRegex(CloudUnavailable, "TaskChannelClaimExpired"):
            guard_context(self.engine, context)
        with (
            job_scope(job),
            self.engine.adapters.execution_job({"id": job["id"], "owner": job["owner"]}),
        ):
            # Even code that only knows the old pooled lease cannot create an attempt.
            with self.assertRaises(Exception):
                self.engine.run_reference_job(job)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_same_account_does_not_get_parallel_channel_leases(self):
        first, account = self.fixture.ready(channel="local")
        self.fixture.ready(account=account, channel="cloud", target_id=999)
        self.queue.sync()
        job = self.queue.claim("simulation.x", "first")
        self.assertIsNotNone(job)
        self.assertIsNone(self.queue.claim("simulation.x", "second"))

    def test_final_guard_checks_explicit_task_channel_even_if_claim_metadata_is_wrong(self):
        task, _ = self.fixture.ready(channel="cloud")
        self.fake_cloud()
        job = self.claim(task)
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_task_channels SET channel='local'")
            db.execute("UPDATE task_channel_claims SET channel='local'")
        with self.assertRaisesRegex(CloudUnavailable, "TaskChannelClaimExpired"):
            guard_context(self.engine, {**context, "channel": "local"})

    def test_returning_online_includes_prior_local_outcomes_before_releasing_fence(self):
        old, account = self.fixture.ready(channel="local")
        self.queue.sync()
        run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        next_task, _ = self.fixture.ready(account=account, channel="cloud", target_id=999)
        _, _, _, sync = self.fake_cloud()
        job = self.claim(next_task)
        TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(set(sync.call_args.kwargs["task_ids"]), {old["id"], next_task["id"]})
        self.assertEqual(self.engine.task(old["id"])["summary"], {"confirmed": 1})

    def test_legacy_direct_execution_cannot_publish_an_explicit_cloud_task(self):
        task, _ = self.fixture.ready(channel="cloud")
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM meta WHERE key='execution_runtime'")
        self.assertTrue(self.engine.run_once(task["id"])["queued"])
        self.engine.run_once()
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.engine.task(task["id"])["summary"], {"not_attempted": 1})

    def test_upgrade_installs_stronger_guards_without_changing_frozen_data(self):
        task, _ = self.fixture.ready(channel="cloud")
        before = self.engine.task(task["id"])["manifest_hash"]
        with self.store.connect(write=True) as db:
            db.execute("DROP TRIGGER task_channel_send_attempt_v2")
            db.execute(
                "CREATE TRIGGER task_channel_send_attempt BEFORE INSERT ON attempts BEGIN SELECT 1; END"
            )
        Store(self.store.path)
        self.assertEqual(self.engine.task(task["id"])["manifest_hash"], before)
        job = self.claim(task)
        with (
            job_scope(job),
            self.engine.adapters.execution_job({"id": job["id"], "owner": job["owner"]}),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "TaskChannelClaimRequired"):
                self.engine.run_reference_job(job)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_reusing_online_account_syncs_new_task_without_another_handoff(self):
        first, account = self.fixture.ready(channel="cloud")
        _, _, request, sync = self.fake_cloud()
        job = self.claim(first)
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        self.queue.settle(job)
        second, _ = self.fixture.ready(account=account, channel="cloud", target_id=999)
        self.queue.sync(second["id"])
        with patch.dict("os.environ", {"AP_EXECUTOR_TASK_IDS": second["id"]}):
            next_job = self.queue.claim("simulation.x", "next")
        next_context, _ = TaskChannelRouter(self.engine).prepare(next_job)
        self.assertEqual(next_context["generation"], context["generation"])
        self.assertEqual(sync.call_args.kwargs, {"task_ids": [second["id"]]})
        self.assertEqual(sum(c.args[0] == "/v1/account-channel" for c in request.call_args_list), 1)

    def test_cancelled_unknown_handoff_can_be_superseded_without_reusing_generation(self):
        task, account = self.fixture.ready(channel="cloud")
        self.fake_cloud(lose_response=True)
        job = self.claim(task)
        with self.assertRaises(CloudUnavailable):
            TaskChannelRouter(self.engine).prepare(job)
        original = self.store.one("SELECT generation FROM account_task_channels")["generation"]
        self.queue.settle(job, CloudUnavailable("CloudTransportUnavailable"))
        self.engine.change_state(task["id"], "cancelled")
        self.now += 31
        next_task, _ = self.fixture.ready(account=account, channel="cloud", target_id=999)
        self.queue.sync(next_task["id"])
        next_job = self.queue.claim("simulation.x", "next")
        # The old remote transition is read first; a new CAS owns the new task.
        cloud = self.cloud_config.return_value
        prior_request = cloud.request.side_effect

        def respond(path, payload=None, **kwargs):
            if path == "/v1/account-channel":
                return {
                    "channel": {
                        **{
                            key: payload[key]
                            for key in ("channel", "generation", "account_id", "node_id", "task_id")
                        },
                        "until_at": payload["until"],
                        "store_id": payload["local_store_id"],
                    }
                }
            return prior_request(path, payload, **kwargs)

        cloud.request.side_effect = respond
        context, _ = TaskChannelRouter(self.engine).prepare(next_job)
        self.assertNotEqual(context["generation"], original)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
