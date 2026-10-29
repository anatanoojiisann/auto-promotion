import json
import unittest

import test_persistent_pools as fixtures

from auto_promotion.interaction_intents import transfer_reserved_task_intents


class IntentReschedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.old, self.account = self.fixture.ready(target_id=123)
        self.engine.change_state(self.old["id"], "paused")
        self.new, _ = self.fixture.ready(
            account=self.account, target_id=123, start=False, channel="local"
        )

    def transfer(self):
        with self.store.connect(write=True) as db:
            return transfer_reserved_task_intents(
                db, self.old["id"], self.new["id"], self.engine.clock()
            )

    def test_moves_unsent_reservation_and_is_idempotent(self):
        self.assertEqual(self.transfer(), 1)
        self.assertEqual(self.transfer(), 1)
        item = self.store.one("SELECT id FROM items WHERE task_id=?", (self.new["id"],))
        self.assertEqual(
            self.store.one("SELECT item_id FROM interaction_intents")["item_id"], item["id"]
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.engine.task(self.old["id"])["state"], "paused")

    def test_active_original_is_rejected(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='active' WHERE id=?", (self.old["id"],))
        with self.assertRaisesRegex(ValueError, "StoppedTasks"):
            self.transfer()

    def test_unknown_or_claimed_result_is_never_released(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE task_id=?", (self.old["id"],))
        with self.assertRaisesRegex(ValueError, "AttemptedItem"):
            self.transfer()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='not_attempted' WHERE task_id=?", (self.old["id"],))
            db.execute("UPDATE interaction_intents SET state='claimed'")
        with self.assertRaisesRegex(ValueError, "IntentNotReserved"):
            self.transfer()

    def test_changed_reply_is_rejected(self):
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT id,payload FROM items WHERE task_id=?", (self.new["id"],)
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["text"] = "different reply"
            db.execute("UPDATE items SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        with self.assertRaisesRegex(ValueError, "PayloadMismatch"):
            self.transfer()

    def test_completed_task_can_transfer_only_proven_never_attempted_missed_item(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
            db.execute("UPDATE items SET status='missed' WHERE task_id=?", (self.old["id"],))
        self.assertEqual(self.transfer(), 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.engine.task(self.old["id"])["state"], "completed")

    def test_completed_task_with_nonmissed_or_attempted_item_is_rejected(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
        with self.assertRaisesRegex(ValueError, "CompletedTaskNotMissed"):
            self.transfer()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',attempt_id='evidence' WHERE task_id=?",
                (self.old["id"],),
            )
        with self.assertRaisesRegex(ValueError, "AttemptedItem"):
            self.transfer()


if __name__ == "__main__":
    unittest.main()
