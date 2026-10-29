import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.execution_channels import binding, configure_independent, read_state
from auto_promotion.models import AccountInput, epoch
from auto_promotion.pool_runtime import PoolScheduler, run_pool
from auto_promotion.queue_store import JobQueue
from auto_promotion.task_channels import TaskChannelRouter, guard_context, save_binding


class OfflineResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.now = self.engine.clock()
        self.task, self.account = self.fixture.ready(channel="local")
        self.store_id = binding(self.engine)
        self.node = "offline-node"
        self.env = patch.dict("os.environ", {"AP_NODE_ID": self.node})
        self.env.start()
        self.addCleanup(self.env.stop)

    def prior_cloud(self, *, until=None, generation="last-ack"):
        until = self.now - 1 if until is None else until
        prior = {
            "account_id": self.account["id"],
            "task_id": self.task["id"],
            "channel": "cloud",
            "generation": generation,
            "until_at": until,
            "store_id": self.store_id,
            "node_id": self.node,
            "fence_source": "cloud_acknowledged",
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                (self.account["id"], "cloud", generation, until, self.task["id"], self.now - 10),
            )
            save_binding(db, prior)

    def enable(self):
        return configure_independent(
            self.engine, [self.account["id"]], self.now + 4000, offline_only_requested=True
        )

    def test_expired_ack_allows_only_local_execution_without_cloud(self):
        self.prior_cloud()
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            state = self.enable()
            self.assertTrue(state["offline_only"])
            self.assertEqual(state["offline_fences"][self.account["id"]]["generation"], "last-ack")
            JobQueue(self.engine).sync()
            run_pool(self.engine, "simulation.x", self.node, max_jobs=1)
        result = self.engine.task(self.task["id"])
        self.assertEqual(result["summary"], {"confirmed": 1})
        self.assertEqual(result["channel"]["accounts"][0]["channel"], "local")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_active_or_changed_cloud_ownership_fails_closed(self):
        self.prior_cloud(until=self.now + 1)
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorCloudStillActive"):
            self.enable()
        self.assertNotEqual(read_state(self.store).get("offline_only"), True)
        self.prior_cloud()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_task_channels SET generation='changed'")
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorOwnershipUnverified"):
            self.enable()

    def test_expiry_never_reconnects_or_uploads(self):
        self.prior_cloud()
        self.enable()
        self.engine.clock = lambda: self.now + 5000
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            scheduler = PoolScheduler(self.engine)
            scheduler.tick()
        cloud = CloudControl("https://unused.invalid", node_id="synthetic", node_token="synthetic")
        with patch.object(cloud, "request", side_effect=AssertionError("cloud access")):
            with self.assertRaisesRegex(CloudUnavailable, "LocalOfflineModeActive"):
                cloud.sync(self.engine)
        self.assertTrue(read_state(self.store)["offline_only"])

    def test_changed_prior_route_after_authorization_cannot_claim(self):
        self.prior_cloud()
        self.enable()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_task_channels SET generation='later-generation'")
        queue = JobQueue(self.engine)
        queue.sync(self.task["id"])
        job = queue.claim("simulation.x", "owner")
        self.assertIsNotNone(job)
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorOwnershipUnverified"):
                TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_no_other_task_or_receipt_is_modified(self):
        self.prior_cloud()
        old_task = self.engine.task(self.task["id"])
        self.enable()
        new_task = self.engine.task(self.task["id"])
        self.assertEqual(old_task["manifest_hash"], new_task["manifest_hash"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "0"
        )

    def test_existing_other_account_local_route_remains_usable(self):
        self.prior_cloud()
        other = self.engine.add_account(
            AccountInput(
                platform="x",
                label="Other",
                backend_label="other-local",
                mode="simulation",
                min_interval=0,
            )
        )
        other_task, _ = self.fixture.ready(account=other, channel="local", target_id=456)
        until = epoch(other_task["policy"]["ends_at"])
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                (other["id"], "local", "local-gen", until, other_task["id"], self.now),
            )
            save_binding(
                db,
                {
                    "account_id": other["id"],
                    "task_id": other_task["id"],
                    "channel": "local",
                    "generation": "local-gen",
                    "until_at": until,
                    "store_id": self.store_id,
                    "node_id": self.node,
                    "fence_source": "cloud_acknowledged",
                },
            )
        self.enable()
        queue = JobQueue(self.engine)
        queue.sync(other_task["id"])
        item_id = self.engine.task(other_task["id"])["items"][0]["id"]
        job = queue.claim("simulation.x", "other-owner", reference_id=item_id)
        self.assertIsNotNone(job)
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            context, cloud = TaskChannelRouter(self.engine).prepare(job)
            guard_context(self.engine, context)
        self.assertEqual(context["channel"], "local")
        self.assertIsNone(cloud)


if __name__ == "__main__":
    unittest.main()
