import unittest

import test_engine as fixtures

from auto_promotion.core import Conflict
from auto_promotion.readiness import DELIVERY_GRACE_SECONDS


class FrozenXResumeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.engine, self.store = self.f.engine, self.f.store

    def paused_task(self, platform="x"):
        task = self.f.task(platform)
        first = self.f.item(platform)
        changes = {"scheduled_at": fixtures.iso(self.f.now + 3600)}
        if platform == "x":
            changes["target"] = "https://x.com/example/status/98765"
        else:
            changes["target"] = "@other_group"
        second = self.f.item(platform, **changes)
        self.engine.set_items(task["id"], [first, second])
        task = self.engine.start_task(task["id"])
        self.engine.change_state(task["id"], "paused")
        return task

    def advance_past_first(self):
        self.f.now += DELIVERY_GRACE_SECONDS + 1

    def test_overdue_never_attempted_is_marked_missed_not_sent(self):
        task = self.paused_task()
        self.advance_past_first()
        resumed = self.engine.start_task(task["id"])
        self.assertEqual(resumed["manifest_hash"], task["manifest_hash"])
        self.engine.run_once(task["id"])
        items = self.engine.task(task["id"])["items"]
        self.assertEqual([item["status"] for item in items], ["missed", "not_attempted"])
        self.assertEqual(self.f.provider.sends, 0)
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        self.f.now += 3600 - (DELIVERY_GRACE_SECONDS + 1)
        self.engine.run_once(task["id"])
        self.assertEqual(self.f.provider.sends, 1)
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "missed")

    def test_prior_unknown_and_confirmed_remain_unchanged(self):
        for status in ("unknown", "confirmed"):
            with self.subTest(status=status):
                task = self.paused_task()
                item_id = task["items"][0]["id"]
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
                self.advance_past_first()
                resumed = self.engine.start_task(task["id"])
                self.assertEqual(resumed["manifest_hash"], task["manifest_hash"])
                self.engine.run_once(task["id"])
                self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], status)
                self.assertEqual(self.f.provider.sends, 0)

    def test_pending_with_evidence_is_rejected(self):
        task = self.paused_task()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET attempt_id='stray' WHERE id=?", (task["items"][0]["id"],))
        self.advance_past_first()
        with self.assertRaisesRegex(Conflict, "尝试或回执"):
            self.engine.start_task(task["id"])

    def test_inflight_and_native_scheduled_items_are_rejected(self):
        for status in ("executing", "scheduled"):
            with self.subTest(status=status):
                task = self.paused_task()
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status=? WHERE id=?", (status, task["items"][0]["id"])
                    )
                self.advance_past_first()
                with self.assertRaisesRegex(Conflict, "执行中或平台排期"):
                    self.engine.start_task(task["id"])

    def test_non_x_expired_still_rejected(self):
        task = self.paused_task("telegram")
        self.advance_past_first()
        with self.assertRaisesRegex(Conflict, "已过期的计划时间"):
            self.engine.start_task(task["id"])

    def test_expired_task_window_still_rejected(self):
        task = self.paused_task()
        self.f.now += 86401
        with self.assertRaisesRegex(Conflict, "执行窗口已过期"):
            self.engine.start_task(task["id"])

    def test_manifest_tamper_still_rejected(self):
        task = self.paused_task()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload_hash='tampered' WHERE id=?", (task["items"][0]["id"],)
            )
        self.advance_past_first()
        with self.assertRaises(Conflict):
            self.engine.start_task(task["id"])


if __name__ == "__main__":
    unittest.main()
