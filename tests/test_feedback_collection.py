import json
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_statistics

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.backends.common import BackendError
from auto_promotion.core import Conflict, Engine


class FeedbackCollectionTests(unittest.TestCase):
    def setUp(self):
        fixture = test_statistics.StatisticsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store, self.now, self.stats = fixture.store, fixture.now, fixture.stats
        self.item_id = fixture.item(published=self.now - 100)
        self.provider = Mock()
        self.engine = Engine(
            self.store,
            AdapterRegistry(Path(self.store.path).parent, overrides={"telegram": self.provider}),
            clock=lambda: self.now,
            live_enabled=False,
        )

    def test_failure_code_is_retained_without_changing_publication_or_cooldown(self):
        before = self.store.one("SELECT * FROM accounts WHERE id=?", (self.item_id,))
        self.provider.call.side_effect = BackendError("ChannelPrivateError")
        with self.assertRaises(Conflict):
            self.engine.collect_feedback(self.item_id)
        check = self.store.one("SELECT * FROM feedback_checks WHERE item_id=?", (self.item_id,))
        self.assertEqual((check["status"], check["error_code"]), ("failed", "ChannelPrivateError"))
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.item_id,))["status"],
            "confirmed",
        )
        self.assertEqual(
            before, self.store.one("SELECT * FROM accounts WHERE id=?", (self.item_id,))
        )
        self.assertEqual(self.stats.feedback_issues()[0]["error_code"], "ChannelPrivateError")
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE feedback_checks SET error_code=NULL WHERE item_id=?", (self.item_id,)
            )
            db.execute("UPDATE calls SET at=? WHERE operation='feedback'", (self.now,))
        self.assertEqual(self.stats.feedback_issues()[0]["error_code"], "ChannelPrivateError")
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_unavailable_parent_stays_partial_and_success_later_clears_error(self):
        self.provider.call.return_value = {
            "verified": True,
            "feedback_events": [],
            "feedback_status": "partial",
            "feedback_checked_through": self.now,
            "feedback_cursor": 574,
            "reason": "feedback_parent_unavailable",
        }
        self.engine.collect_feedback(self.item_id)
        row = self.stats.feedback_issues()[0]
        self.assertEqual(
            (row["status"], row["error_code"]), ("partial", "feedback_parent_unavailable")
        )
        self.assertIsNone(self.stats.snapshot()["totals"]["received_replies"])
        self.provider.call.return_value.update(feedback_status="complete", feedback_cursor=500)
        self.engine.collect_feedback(self.item_id)
        row = self.store.one("SELECT * FROM feedback_checks WHERE item_id=?", (self.item_id,))
        self.assertIsNone(row["error_code"])
        self.assertEqual(row["last_message_id"], 574)

    def test_cooldown_and_per_slot_collection_are_preserved(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET cooldown_until=? WHERE id=?", (self.now + 100, self.item_id)
            )
        with self.assertRaises(Conflict):
            self.engine.collect_feedback(self.item_id)
        self.assertEqual(self.engine.collect_statistics()["processed"], [])
        self.provider.call.assert_not_called()
        self.now += 200
        self.assertEqual(self.engine.collect_statistics()["processed"], [])
        self.provider.call.assert_not_called()

    def test_error_messages_are_not_saved_as_diagnostics(self):
        self.provider.call.return_value = {"verified": False, "reason": "token=private detail"}
        result = self.engine.collect_feedback(self.item_id)
        row = self.stats.feedback_issues()[0]
        self.assertNotIn("private detail", json.dumps([row, result]))
        self.assertEqual(row["error_code"], "feedback_unavailable")


if __name__ == "__main__":
    unittest.main()
