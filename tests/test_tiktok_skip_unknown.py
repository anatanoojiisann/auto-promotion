import json
import unittest

import test_tiktok_browser as browser_fixtures
import test_tiktok_messages as message_fixtures

from auto_promotion.core import Conflict
from auto_promotion.meta_browser import claim_blocker
from auto_promotion.meta_skip import skip_unknown


class TikTokSkipUnknownTests(unittest.TestCase):
    setUp = browser_fixtures.TikTokBrowserTests.setUp
    identity = browser_fixtures.TikTokBrowserTests.identity
    snapshot = message_fixtures.TikTokMessageTests.snapshot
    enable = message_fixtures.TikTokMessageTests.enable
    ready = message_fixtures.TikTokMessageTests.ready
    proof = message_fixtures.TikTokMessageTests.proof
    receipt = message_fixtures.TikTokMessageTests.receipt

    def uncertain(self):
        task, item = self.ready(reply=True)
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        return task, item, owner

    def blocker(self, payload, item_id="another-item"):
        with self.store.connect() as db:
            return claim_blocker(db, self.engine.account(self.aid), payload, item_id, self.now)

    def test_explicit_skip_unblocks_other_dm_without_replaying_and_allows_later_receipt(self):
        _, item, owner = self.uncertain()
        other = {
            **item["payload"],
            "target": "https://www.tiktok.com/@peer_two",
            "recipient_id": "peer_two",
            "tiktok_conversation_id": "0:1:11:12",
            "tiktok_message_id": "101",
        }
        self.assertEqual(self.blocker(other), "meta_outcome_unknown")
        before = self.store.one(
            "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
        )
        account_before = self.engine.account(self.aid)
        result = skip_unknown(
            self.browser, item["id"], owner, "Do not block later content on an unconfirmed result"
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "UserSkippedUnknownSubmission")
        self.assertFalse(result["verified"])
        self.assertTrue(result["submission_started"])
        self.assertEqual(self.blocker(item["payload"]), "meta_duplicate_target")
        self.assertIsNone(self.blocker(other))
        self.assertEqual(
            before,
            self.store.one(
                "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
            ),
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)
        for key in (
            "next_at",
            "cooldown_until",
            "daily_limit",
            "hourly_limit",
            "min_interval",
            "disabled",
        ):
            self.assertEqual(account_before[key], self.engine.account(self.aid)[key])
        audit = json.loads(
            self.store.one(
                "SELECT details FROM audit WHERE kind='meta_browser.user_skipped_unknown'"
            )["details"]
        )
        self.assertEqual(audit["submission_outcome"], "unknown")
        self.assertTrue(audit["target_reservation_retained"])
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_sent_receipts")["n"], 0)
        self.now += 61
        pending = self.browser.pending()["unresolved"]
        recovered = next(row for row in pending if row["item_id"] == item["id"])
        self.assertEqual(recovered["private_attempt"]["before_message_ids"], ["100", "200"])
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], {**self.receipt(item), "message_id": "200"}, owner)
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], self.receipt(item), "wrong-owner")
        confirmed = self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_sent_receipts")["n"], 1)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "confirmed",
        )

    def test_skip_requires_the_original_owner_and_explicit_authorization(self):
        _, item, owner = self.uncertain()
        for instruction, actual_owner in (("", owner), ("skip", "wrong-owner")):
            with (
                self.subTest(instruction=instruction, owner=actual_owner),
                self.assertRaises(Conflict),
            ):
                skip_unknown(self.browser, item["id"], actual_owner, instruction)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
