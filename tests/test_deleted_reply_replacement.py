import hashlib
import unittest
from unittest.mock import patch

import test_engine as fixtures

from auto_promotion.core import Conflict
from auto_promotion.deleted_reply_replacement import (
    authorize_no_submit_retry,
    authorize_replacement,
    replacement_proof,
)
from auto_promotion.interaction_intents import intent_blocker
from auto_promotion.models import canonical, digest


class DeletedReplyReplacementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine = self.fixture.engine
        self.clock = patch(
            "auto_promotion.deleted_reply_replacement.time.time",
            side_effect=lambda: self.fixture.now,
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        task = self.fixture.task("x")
        self.engine.set_items(task["id"], [self.fixture.item("x")])
        self.engine.start_task(task["id"])
        self.engine.run_once(task["id"])
        task = self.engine.task(task["id"])
        old = task["items"][0]
        self.old_id = old["id"]
        payload = {**old["payload"], "text": "\ufffd\ufffd damaged reply"}
        manifest = {**task["manifest"], "items": [payload]}
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown',payload=?,payload_hash=?,external_reference=? WHERE id=?",
                (
                    canonical(payload),
                    digest(payload),
                    "https://x.com/example/status/99999",
                    self.old_id,
                ),
            )
            db.execute(
                "UPDATE tasks SET manifest=?,manifest_hash=? WHERE id=?",
                (canonical(manifest), digest(manifest), task["id"]),
            )
        self.task, self.item = self.draft()
        self.evidence = {
            "observed_at": self.fixture.now,
            "verified": True,
            "delete_response_accepted": True,
            "reload_deleted": True,
            "deleted_post_id": "99999",
            "account_handle": "example",
            "parent_target": self.item["payload"]["target"],
            "new_text_sha256": hashlib.sha256(self.item["payload"]["text"].encode()).hexdigest(),
        }

    def draft(self, **policy):
        task = self.fixture.task("x", **policy)
        self.engine.set_items(task["id"], [self.fixture.item("x", text="正确的中文文案")])
        task = self.engine.task(task["id"])
        return task, task["items"][0]

    def authorize(self, **changes):
        return authorize_replacement(
            self.engine,
            self.old_id,
            self.item["id"],
            source="test: explicit user delete and correct instruction",
            deletion_evidence={**self.evidence, **changes},
        )

    def test_only_exact_deleted_garbled_reply_is_replaced_once(self):
        old_before = self.engine.store.one("SELECT * FROM items WHERE id=?", (self.old_id,))
        account = self.fixture.accounts["x"]
        with self.engine.store.connect() as db:
            self.assertIsNotNone(
                intent_blocker(db, account, self.task, self.item["payload"], self.item["id"])
            )
        self.authorize()
        with self.engine.store.connect() as db:
            self.assertIsNone(
                intent_blocker(db, account, self.task, self.item["payload"], self.item["id"])
            )
        self.engine.start_task(self.task["id"])
        self.engine.run_once(self.task["id"])
        self.engine.run_once(self.task["id"])
        self.assertEqual(self.fixture.provider.sends, 2)  # one original, one new correction
        self.assertEqual(self.engine.task(self.task["id"])["items"][0]["status"], "confirmed")
        self.assertEqual(
            self.engine.store.one("SELECT * FROM items WHERE id=?", (self.old_id,)), old_before
        )

    def test_missing_failed_stale_or_mismatched_deletion_never_unlocks(self):
        for changes in (
            {"verified": False},
            {"delete_response_accepted": False},
            {"reload_deleted": False},
            {"observed_at": self.fixture.now - 901},
            {"observed_at": float("nan")},
            {"deleted_post_id": "another"},
            {"account_handle": "another"},
            {"new_text_sha256": "0" * 64},
            {"parent_target": "https://x.com/other/status/1"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.authorize(**changes)
        with self.engine.store.connect() as db:
            self.assertIsNone(replacement_proof(db, self.item["id"]))

    def test_deletion_receipt_cannot_unlock_a_second_task(self):
        self.authorize()
        self.assertTrue(self.authorize()["unchanged"])
        other_task, other_item = self.draft()
        with self.assertRaises(ValueError):
            authorize_replacement(
                self.engine,
                self.old_id,
                other_item["id"],
                source="another instruction",
                deletion_evidence=self.evidence,
            )
        with self.engine.store.connect() as db:
            self.assertIsNotNone(
                intent_blocker(
                    db,
                    self.fixture.accounts["x"],
                    other_task,
                    other_item["payload"],
                    other_item["id"],
                )
            )

    def test_expiry_is_rechecked_at_the_final_claim_boundary(self):
        self.authorize()
        self.engine.start_task(self.task["id"])
        self.fixture.now += 1801
        with self.engine.store.connect() as db, self.assertRaises(Conflict):
            row = db.execute("SELECT * FROM tasks WHERE id=?", (self.task["id"],)).fetchone()
            self.engine._integrity(db, row, "outbound")
        self.assertIsNone(self.engine.task(self.task["id"])["items"][0]["attempt_id"])

    def test_unknown_or_missing_negative_receipt_cannot_continue_correction(self):
        self.authorize()
        other_task, other_item = self.draft()
        with self.assertRaises(ValueError):
            authorize_no_submit_retry(
                self.engine, self.item["id"], other_item["id"], source="same correction"
            )
        with self.engine.store.connect() as db:
            self.assertIsNotNone(replacement_proof(db, self.item["id"]))
            self.assertIsNone(replacement_proof(db, other_item["id"]))

    def test_validated_no_submit_receipt_moves_one_grant_without_resetting_attempt(self):
        self.authorize()
        self.engine.start_task(self.task["id"])
        self.fixture.provider.result = {
            "status": "failed_known",
            "verified": False,
            "submission_started": False,
            "reason": "TabbitPreparationFailedBeforeSubmission",
            "execution_step": "reply_composer",
        }
        self.engine.run_once(self.task["id"])
        previous = self.engine.store.one("SELECT * FROM items WHERE id=?", (self.item["id"],))
        other_task, other_item = self.draft()
        with patch(
            "auto_promotion.deleted_reply_replacement._negative_receipt_matches", return_value=True
        ):
            authorize_no_submit_retry(
                self.engine, self.item["id"], other_item["id"], source="same approved correction"
            )
            with self.engine.store.connect() as db:
                self.assertIsNone(replacement_proof(db, self.item["id"]))
                self.assertEqual(
                    replacement_proof(db, other_item["id"])["prior_no_submit_items"][0]["item_id"],
                    self.item["id"],
                )
            self.assertEqual(
                self.engine.store.one("SELECT * FROM items WHERE id=?", (self.item["id"],)),
                previous,
            )

    def test_two_proven_unsubmitted_preparations_keep_user_and_target_limit_one(self):
        self.fixture.now += 90000
        task, item = self.draft(target_daily_limit=1, user_daily_limit=1)
        self.task, self.item = task, item
        self.evidence["observed_at"] = self.fixture.now
        self.authorize()
        self.fixture.provider.result = {
            "status": "failed_known",
            "verified": False,
            "submission_started": False,
            "reason": "TabbitPreparationFailedBeforeSubmission",
            "execution_step": "reply_composer",
        }
        self.engine.start_task(task["id"])
        self.engine.run_once(task["id"])
        with patch(
            "auto_promotion.deleted_reply_replacement._negative_receipt_matches", return_value=True
        ):
            for index in range(2):
                next_task, next_item = self.draft(target_daily_limit=1, user_daily_limit=1)
                authorize_no_submit_retry(
                    self.engine, item["id"], next_item["id"], source="same correction"
                )
                self.engine.start_task(next_task["id"])
                if index == 1:
                    self.fixture.provider.result = None
                self.engine.run_once(next_task["id"])
                task, item = next_task, next_item
            self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")
        self.assertEqual(self.fixture.provider.sends, 4)
        self.assertEqual(self.engine.store.one("SELECT count(*) n FROM attempts")["n"], 4)

    def test_expired_preparation_needs_fresh_draft_and_verified_no_submit_receipt(self):
        self.authorize()
        self.engine.start_task(self.task["id"])
        self.fixture.provider.result = {
            "status": "failed_known",
            "verified": False,
            "submission_started": False,
            "reason": "TabbitPreparationFailedBeforeSubmission",
            "execution_step": "reply_composer",
        }
        self.engine.run_once(self.task["id"])
        self.fixture.now += 1801
        task, item = self.draft()
        with patch(
            "auto_promotion.deleted_reply_replacement._negative_receipt_matches", return_value=False
        ):
            with self.assertRaises(ValueError):
                authorize_no_submit_retry(
                    self.engine, self.item["id"], item["id"], source="same approved correction"
                )
        with patch(
            "auto_promotion.deleted_reply_replacement._negative_receipt_matches", return_value=True
        ):
            authorize_no_submit_retry(
                self.engine, self.item["id"], item["id"], source="same approved correction"
            )
            with self.engine.store.connect() as db:
                self.assertIsNone(replacement_proof(db, self.item["id"]))
                self.assertIsNotNone(replacement_proof(db, item["id"]))


if __name__ == "__main__":
    unittest.main()
