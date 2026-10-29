import time
import unittest

import test_meta_browser as fixtures

from auto_promotion.meta_browser import claim_blocker
from auto_promotion.meta_legacy_skip import record_legacy_skip


class LegacyUnknownSkipTests(unittest.TestCase):
    setUp = fixtures.MetaBrowserTests.setUp
    write_source = fixtures.MetaBrowserTests.write_source
    identity = fixtures.MetaBrowserTests.identity
    ready = fixtures.MetaBrowserTests.ready
    proof = fixtures.MetaBrowserTests.proof

    def set_legacy_unknown(self):
        self.write_source(
            {
                "status": "stopped_by_user",
                "items": [
                    {
                        "id": "L03",
                        "account": "Existing Facebook",
                        "status": "unknown",
                        "url": "https://www.facebook.com/reel/123456789/",
                        "draft": "A previous comment",
                        "attempts": [{"status": "unknown"}],
                    }
                ],
            }
        )
        self.browser.sync()
        return self.store.one(
            "SELECT i.* FROM items i JOIN meta_browser_history h ON h.item_id=i.id WHERE h.external_id='L03'"
        )

    def authorize(self, item, target):
        return record_legacy_skip(
            self.store,
            item["id"],
            "Explicitly skip the old unknown",
            [{"target": target, "not_after": self.skip_now + 1200}],
            "Explicitly started Meta task",
            now=self.skip_now,
        )

    def blocker(self, item):
        with self.store.connect() as db:
            return claim_blocker(
                db, self.engine.account(self.aid), item["payload"], item["id"], self.browser.clock()
            )

    def test_skip_is_scoped_and_keeps_original_unknown_and_deduplication(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        task, item = self.ready(target)
        self.assertEqual(self.blocker(item), "meta_source_unknown")
        before = self.engine.account(self.aid)
        self.authorize(old, target)
        self.assertIsNone(self.blocker(item))
        old_after = self.store.one(
            "SELECT status,attempt_id,external_reference FROM items WHERE id=?", (old["id"],)
        )
        self.assertEqual(old_after["status"], "unknown")
        self.assertIsNone(old_after["external_reference"])
        self.assertEqual(
            self.store.one("SELECT state FROM meta_browser_contacts WHERE item_id=?", (old["id"],))[
                "state"
            ],
            "unknown",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        for key in (
            "disabled",
            "daily_limit",
            "hourly_limit",
            "min_interval",
            "cooldown_until",
            "next_at",
        ):
            self.assertEqual(before[key], self.engine.account(self.aid)[key])
        other = {
            **item["payload"],
            "target": "https://www.facebook.com/groups/example/posts/11111/",
        }
        with self.store.connect() as db:
            self.assertEqual(
                claim_blocker(db, before, other, item["id"], self.browser.clock()),
                "meta_source_unknown",
            )
            old_target = {**item["payload"], "target": "https://www.facebook.com/reel/123456789/"}
            self.assertEqual(
                claim_blocker(db, before, old_target, item["id"], self.browser.clock()),
                "meta_source_unknown",
            )

    def test_changed_source_or_extra_uncertainty_reblocks(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        _, item = self.ready(target)
        self.authorize(old, target)
        self.assertIsNone(self.blocker(item))
        self.write_source(
            {
                "status": "stopped_by_user",
                "items": [
                    {
                        "id": "L03",
                        "account": "Existing Facebook",
                        "status": "unknown",
                        "url": "https://www.facebook.com/reel/123456789/",
                    },
                    {
                        "id": "L04",
                        "account": "Existing Facebook",
                        "status": "unknown",
                        "url": "https://www.facebook.com/reel/987654321/",
                    },
                ],
            }
        )
        self.assertEqual(self.blocker(item), "meta_source_unknown")

    def test_explicit_scope_and_instruction_are_required(self):
        old = self.set_legacy_unknown()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        good = {"target": target, "not_after": time.time() + 1200}
        old_target = {
            "target": "https://www.facebook.com/reel/123456789/",
            "not_after": time.time() + 1200,
        }
        for text, targets, prefix in [
            ("", [good], "task"),
            ("skip", [], "task"),
            ("skip", [good], ""),
            ("skip", [old_target], "task"),
            ("skip", [{"target": target, "not_after": time.time() - 1}], "task"),
        ]:
            with (
                self.subTest(text=text, targets=targets, prefix=prefix),
                self.assertRaises(ValueError),
            ):
                record_legacy_skip(self.store, old["id"], text, targets, prefix)

    def test_task_name_and_local_channel_are_required(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        task, item = self.ready(target)
        self.authorize(old, target)
        self.assertIsNone(self.blocker(item))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET name='unrelated task' WHERE id=?", (task["id"],))
        self.assertEqual(self.blocker(item), "meta_source_unknown")

    def test_different_unknown_item_still_blocks(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        _, item = self.ready(target)
        self.authorize(old, target)
        self.assertIsNone(self.blocker(item))
        _, other = self.ready("https://www.facebook.com/groups/example/posts/99999/")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (other["id"],))
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")

    def test_skip_is_idempotent_and_original_source_is_unchanged(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        before = self.source.read_bytes()
        first = self.authorize(old, target)
        second = self.authorize(old, target)
        self.assertEqual(first, second)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='meta_browser.legacy_unknown_user_skipped'"
            )["n"],
            1,
        )

    def test_target_authorization_expires_without_clearing_history(self):
        old = self.set_legacy_unknown()
        self.skip_now = time.time()
        target = "https://www.facebook.com/groups/example/posts/7654321/"
        _, item = self.ready(target)
        self.authorize(old, target)
        with self.store.connect() as db:
            self.assertEqual(
                claim_blocker(
                    db,
                    self.engine.account(self.aid),
                    item["payload"],
                    item["id"],
                    self.skip_now + 1201,
                ),
                "meta_source_unknown",
            )
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (old["id"],))["status"], "unknown"
        )


if __name__ == "__main__":
    unittest.main()
