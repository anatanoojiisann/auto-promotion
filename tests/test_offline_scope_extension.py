import json
import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.execution_channels import (
    binding,
    configure_independent,
    extend_offline_scope,
    read_state,
)
from auto_promotion.models import AccountInput
from auto_promotion.store import Store
from auto_promotion.task_channels import save_binding


class OfflineScopeExtensionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.now = self.engine.clock()
        self.node = "test-node"
        self.env = patch.dict("os.environ", {"AP_NODE_ID": self.node})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.first_task, self.first = self.fixture.ready(channel="local", target_id=1001)
        self.store_id = binding(self.engine)
        self.seed_prior(self.first, self.first_task, "first-gen")
        configure_independent(
            self.engine, [self.first["id"]], self.now + 4000, offline_only_requested=True
        )
        self.other = self.engine.add_account(
            AccountInput(
                platform="x",
                label="second",
                backend_label="second",
                mode="simulation",
                min_interval=0,
            )
        )
        self.other_task, _ = self.fixture.ready(
            account=self.other, target_id=1002, start=False, channel="local"
        )
        self.seed_prior(self.other, self.other_task, "other-gen")
        # The extension is restricted to real accounts in production; simulate an otherwise healthy registered X account.
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET mode='live',status='healthy' WHERE id=?", (self.other["id"],)
            )

    def seed_prior(self, account, task, generation, *, until=None, error=None, retry_at=0):
        until = self.now - 1 if until is None else until
        prior = {
            "account_id": account["id"],
            "task_id": task["id"],
            "channel": "cloud",
            "generation": generation,
            "until_at": until,
            "store_id": self.store_id,
            "node_id": self.node,
            "fence_source": "cloud_acknowledged",
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,?,?)",
                (
                    account["id"],
                    "cloud",
                    generation,
                    until,
                    task["id"],
                    self.now - 10,
                    error,
                    retry_at,
                ),
            )
            save_binding(db, prior)
            Store.audit(
                db,
                "execution.account_channel_changed",
                {"account_id": account["id"], "channel": "cloud", "generation": generation},
                task["id"],
                self.now - 10,
            )

    def test_add_preserves_existing_scope_and_never_connects(self):
        before = read_state(self.store)
        self.seed_prior(
            self.other,
            self.other_task,
            "other-gen",
            error="OperationalError",
            retry_at=self.now - 2,
        )
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            result = extend_offline_scope(self.engine, [self.other["id"]])
        after = read_state(self.store)
        self.assertEqual(result["added"], [self.other["id"]])
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["until"], before["until"])
        self.assertEqual(
            after["offline_fences"][self.first["id"]], before["offline_fences"][self.first["id"]]
        )
        self.assertEqual(set(after["account_ids"]), {self.first["id"], self.other["id"]})
        scope = json.loads(
            self.store.one("SELECT value FROM meta WHERE key='local_channel_authority'")["value"]
        )
        self.assertEqual(sorted(scope["account_ids"]), sorted(after["account_ids"]))

    def test_unexpired_cloud_or_pending_handoff_is_blocked(self):
        self.seed_prior(self.other, self.other_task, "later-gen", until=self.now + 100)
        before = read_state(self.store)
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorCloudStillActive"):
            extend_offline_scope(self.engine, [self.other["id"]])
        self.assertEqual(read_state(self.store), before)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE account_task_channels SET state='handoff' WHERE account_id=?",
                (self.other["id"],),
            )
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorOwnershipUnverified"):
            extend_offline_scope(self.engine, [self.other["id"]])

    def test_unresolved_error_or_receipt_stays_blocked(self):
        self.seed_prior(
            self.other,
            self.other_task,
            "err-gen",
            error="CloudTransportUnavailable",
            retry_at=self.now - 1,
        )
        with self.assertRaisesRegex(CloudUnavailable, "OfflinePriorOwnershipUnverified"):
            extend_offline_scope(self.engine, [self.other["id"]])

    def test_explicit_new_local_task_preserves_scope_and_audits_source(self):
        before = read_state(self.store)
        with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud access")):
            result = extend_offline_scope(
                self.engine, [self.other["id"]], source="explicit_user_local_task"
            )
        after = read_state(self.store)
        self.assertEqual(result["added"], [self.other["id"]])
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(
            after["account_authorizations"][self.other["id"]]["source"], "explicit_user_local_task"
        )

    def test_non_x_or_without_explicit_user_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ExplicitUser"):
            extend_offline_scope(self.engine, [self.other["id"]], source="automatic")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET platform='telegram' WHERE id=?", (self.other["id"],))
        with self.assertRaisesRegex(CloudUnavailable, "OfflineAccountUnavailable"):
            extend_offline_scope(self.engine, [self.other["id"]])


if __name__ == "__main__":
    unittest.main()
