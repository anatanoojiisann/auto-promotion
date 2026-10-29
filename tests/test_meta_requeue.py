import json
import unittest

import test_meta_browser as meta_fixture
from test_meta_browser import iso

from auto_promotion.backends.common import BackendError
from auto_promotion.core import Conflict
from auto_promotion.meta_requeue import requeue_unsubmitted
from auto_promotion.models import ItemInput, TaskInput


class MetaRequeueTests(unittest.TestCase):
    setUp = meta_fixture.MetaBrowserTests.setUp
    write_source = meta_fixture.MetaBrowserTests.write_source
    identity = meta_fixture.MetaBrowserTests.identity
    ready = meta_fixture.MetaBrowserTests.ready
    proof = meta_fixture.MetaBrowserTests.proof

    def prepared_pair(self):
        task, item = self.ready()
        begun = self.browser.begin(item["id"], self.proof(item))
        self.browser.result(
            item["id"], {"status": "failed_known", "confirmed_not_submitted": True}, begun["owner"]
        )
        policy = {k: v for k, v in task["policy"].items() if k not in {"quota_snapshot"}}
        policy["ends_at"] = iso(self.browser.clock() + 7200)
        replacement = self.engine.create_task(
            TaskInput(name="Explicit recovery", account_ids=[self.aid], policy=policy)
        )
        p = {
            k: v
            for k, v in item["payload"].items()
            if k not in {"position", "platform", "account_label"}
        }
        p["scheduled_at"] = iso(self.browser.clock() + 3600)
        new = self.engine.set_items(replacement["id"], [ItemInput(**p)])["items"][0]
        return item, new

    def test_explicit_requeue_preserves_attempt_quota_and_normal_guard(self):
        old, new = self.prepared_pair()
        before = {
            table: self.store.rows("SELECT * FROM " + table)
            for table in ("attempts", "activity", "accounts")
        }
        result = requeue_unsubmitted(
            self.browser, old["id"], new["id"], "Requeue this confirmed unsent item"
        )
        self.assertFalse(result["already_registered"])
        self.assertTrue(
            requeue_unsubmitted(self.browser, old["id"], new["id"], "same request")[
                "already_registered"
            ]
        )
        for table, rows in before.items():
            self.assertEqual(rows, self.store.rows("SELECT * FROM " + table))
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (old["id"],))["status"],
            "failed_known",
        )
        self.assertEqual(
            self.store.one("SELECT status,attempt_id FROM items WHERE id=?", (new["id"],)),
            {"status": "not_attempted", "attempt_id": None},
        )
        self.assertIsNone(self.store.one("SELECT 1 FROM meta_browser_contacts"))
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='meta_browser.requeued_unsubmitted'"
            )["n"],
            1,
        )
        with self.assertRaises((Conflict, BackendError)):
            self.browser.guard(
                old["id"], self.store.one("SELECT owner FROM meta_browser_claims")["owner"]
            )

    def test_guard_used_unknown_receipt_and_changed_content_are_refused(self):
        old, new = self.prepared_pair()
        mutations = [
            ("UPDATE meta_browser_claims SET submission_started=1 WHERE item_id=?", old["id"]),
            ("UPDATE items SET status='unknown' WHERE id=?", old["id"]),
            (
                "UPDATE items SET external_reference='https://example.com/receipt' WHERE id=?",
                old["id"],
            ),
            ("UPDATE items SET payload=json_set(payload,'$.text','changed') WHERE id=?", new["id"]),
            ("UPDATE execution_jobs SET state='leased' WHERE reference_id=?", old["id"]),
        ]
        for sql, arg in mutations:
            with self.subTest(sql=sql):
                with self.store.connect(write=True) as db:
                    db.execute(sql, (arg,))
                with self.assertRaises(Conflict):
                    requeue_unsubmitted(self.browser, old["id"], new["id"], "Requeue this item")
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE meta_browser_claims SET submission_started=0 WHERE item_id=?",
                        (old["id"],),
                    )
                    db.execute(
                        "UPDATE items SET status='failed_known',external_reference=NULL WHERE id=?",
                        (old["id"],),
                    )
                    db.execute(
                        "UPDATE items SET payload=? WHERE id=?",
                        (json.dumps(new["payload"]), new["id"]),
                    )
                    db.execute(
                        "UPDATE execution_jobs SET state='failed_known' WHERE reference_id=?",
                        (old["id"],),
                    )
                self.assertEqual(
                    self.store.one("SELECT count(*) n FROM meta_browser_contacts")["n"], 1
                )
        with self.assertRaises(Conflict):
            requeue_unsubmitted(self.browser, old["id"], new["id"], "")


if __name__ == "__main__":
    unittest.main()
