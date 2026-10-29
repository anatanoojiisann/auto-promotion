import json
import unittest

import test_meta_browser as fixtures

from auto_promotion.core import Conflict
from auto_promotion.meta_browser import claim_blocker
from auto_promotion.meta_skip import skip_unknown


class MetaSkipUnknownTests(unittest.TestCase):
    setUp = fixtures.MetaBrowserTests.setUp
    write_source = fixtures.MetaBrowserTests.write_source
    identity = fixtures.MetaBrowserTests.identity
    ready = fixtures.MetaBrowserTests.ready
    proof = fixtures.MetaBrowserTests.proof

    def uncertain(self):
        task, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner)
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        return task, item, owner

    def test_explicit_skip_retains_uncertainty_attempt_and_target_deduplication(self):
        task, item, owner = self.uncertain()
        before = self.store.one("SELECT attempt_id,claimed_at FROM items WHERE id=?", (item["id"],))
        account_before = self.engine.account(self.aid)
        result = skip_unknown(self.browser, item["id"], owner, "Skip this item")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "UserSkippedUnknownSubmission")
        self.assertFalse(result["verified"])
        self.assertTrue(result["submission_started"])
        self.assertNotIn("confirmed_not_submitted", result)
        self.assertEqual(
            before,
            self.store.one("SELECT attempt_id,claimed_at FROM items WHERE id=?", (item["id"],)),
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(
            self.store.one("SELECT state FROM meta_browser_contacts")["state"], "cancelled"
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)
        audit = self.store.one(
            "SELECT details FROM audit WHERE kind='meta_browser.user_skipped_unknown'"
        )
        self.assertEqual(json.loads(audit["details"])["prior_status"], "unknown")
        self.assertEqual(json.loads(audit["details"])["submission_outcome"], "unknown")
        for key in (
            "next_at",
            "cooldown_until",
            "daily_limit",
            "hourly_limit",
            "min_interval",
            "disabled",
        ):
            self.assertEqual(account_before[key], self.engine.account(self.aid)[key])
        with self.store.connect() as db:
            self.assertEqual(
                claim_blocker(
                    db, account_before, item["payload"], "new-item", self.browser.clock()
                ),
                "meta_duplicate_target",
            )
            other = {
                **item["payload"],
                "target": "https://www.facebook.com/groups/example/posts/88888/",
            }
            self.assertIsNone(
                claim_blocker(db, account_before, other, "new-item", self.browser.clock())
            )
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        with self.assertRaises(Conflict):
            self.browser.guard(item["id"], owner)
        self.assertEqual(
            skip_unknown(self.browser, item["id"], owner, "Skip this item")["reason"],
            "UserSkippedUnknownSubmission",
        )
        audit_count = self.store.one(
            "SELECT count(*) n FROM audit WHERE kind='meta_browser.user_skipped_unknown'"
        )["n"]
        self.assertEqual(
            self.browser.result(item["id"], {"status": "unknown"}, owner)["status"], "cancelled"
        )
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='meta_browser.user_skipped_unknown'"
            )["n"],
            audit_count,
        )

    def test_no_implicit_skip_or_wrong_owner_or_known_failure(self):
        task, item, owner = self.uncertain()
        for instruction, bad_owner in [("", owner), ("Skip this item", "other-owner")]:
            with self.subTest(instruction=instruction, bad_owner=bad_owner):
                with self.assertRaises(Conflict):
                    skip_unknown(self.browser, item["id"], bad_owner, instruction)
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "unknown",
        )
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta_browser_claims SET submission_started=0 WHERE item_id=?", (item["id"],)
            )
        with self.assertRaises(Conflict):
            skip_unknown(self.browser, item["id"], owner, "Skip this item")
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
