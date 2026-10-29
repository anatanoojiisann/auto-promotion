import time
import unittest

import test_meta_legacy_skip as legacy

from auto_promotion.continuation_preflight import _account_blocker, replacement_blocker
from auto_promotion.models import canonical


class ScopedContinuationTests(unittest.TestCase):
    setUp = legacy.LegacyUnknownSkipTests.setUp
    write_source = legacy.LegacyUnknownSkipTests.write_source
    identity = legacy.LegacyUnknownSkipTests.identity
    ready = legacy.LegacyUnknownSkipTests.ready
    set_legacy_unknown = legacy.LegacyUnknownSkipTests.set_legacy_unknown
    authorize = legacy.LegacyUnknownSkipTests.authorize

    def scope(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        task, item = self.ready("https://www.facebook.com/groups/example/posts/7654321/")
        self.authorize(old, item["payload"]["target"])
        return old, task, item

    def check(self, task, item, *, now=None):
        with self.store.connect() as db:
            return _account_blocker(
                db, self.aid, now or time.time(), task_id=task["id"], item_ids=[item["id"]]
            )

    def test_scoped_new_target_passes_without_changing_old_unknown(self):
        old, task, item = self.scope()
        self.assertIsNone(self.check(task, item))
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (old["id"],))["status"], "unknown"
        )
        self.assertEqual(
            replacement_blocker(self.engine, self.aid, old["id"]),
            "prior_item_not_proven_unsubmitted",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_expiry_wrong_task_source_change_and_old_target_still_block(self):
        old, task, item = self.scope()
        self.assertEqual(self.check(task, item, now=self.skip_now + 1201), "meta_source_unknown")
        with self.store.connect() as db:
            self.assertEqual(
                _account_blocker(
                    db, self.aid, time.time(), task_id="another-task", item_ids=[item["id"]]
                ),
                "proposed_item_scope_mismatch",
            )
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=? WHERE id=?",
                (
                    canonical(
                        {**item["payload"], "target": "https://www.facebook.com/reel/123456789/"}
                    ),
                    item["id"],
                ),
            )
        self.assertEqual(self.check(task, item), "meta_source_unknown")
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=? WHERE id=?", (canonical(item["payload"]), item["id"])
            )
            db.execute(
                "UPDATE meta_browser_sources SET fingerprint='changed' WHERE account_id=?",
                (self.aid,),
            )
        self.assertEqual(self.check(task, item), "meta_source_unknown")

    def test_unrelated_unknown_and_out_of_scope_item_do_not_get_hidden(self):
        _, task, item = self.scope()
        self.engine.clock = time.time
        unrelated, other = self.ready("https://www.facebook.com/groups/example/posts/777777/")
        self.assertEqual(self.check(unrelated, other), "meta_source_unknown")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (other["id"],))
        self.assertEqual(self.check(task, item), "meta_outcome_unknown")


if __name__ == "__main__":
    unittest.main()
