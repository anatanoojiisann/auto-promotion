import unittest

import test_persistent_pools as fixtures

from auto_promotion.account_daily_local import enable
from auto_promotion.cloud_control import export_snapshot
from auto_promotion.quotas import scopes


class LocalDailySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready(target_id=123)

    def test_enable_is_local_idempotent_and_leaves_remote_snapshot_and_manifest_unchanged(self):
        before = self.engine.task(self.task["id"])
        original = export_snapshot(self.engine, task_ids=[self.task["id"]])
        self.assertFalse(enable(self.engine)["already_enabled"])
        self.assertTrue(enable(self.engine)["already_enabled"])
        after = export_snapshot(self.engine, task_ids=[self.task["id"]])
        self.assertEqual(original, after)
        self.assertTrue(self.engine.account(self.account["id"])["daily_limit_unlimited_locally"])
        current = self.engine.task(self.task["id"])
        self.assertEqual(current["manifest_hash"], before["manifest_hash"])
        with self.store.connect() as db:
            raw = db.execute("SELECT * FROM tasks WHERE id=?", (self.task["id"],)).fetchone()
            self.engine._integrity(db, raw, "outbound")
            account_scope = next(
                v
                for k, _, v in scopes(db, self.account, raw, current["items"][0]["payload"])
                if k == "account"
            )
            self.assertIsNone(account_scope["daily"])


if __name__ == "__main__":
    unittest.main()
