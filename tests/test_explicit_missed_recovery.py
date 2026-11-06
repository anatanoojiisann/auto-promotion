import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.explicit_missed_recovery import SOURCE, recover
from auto_promotion.interaction_intents import intent_blocker
from auto_promotion.models import ItemInput, digest
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue


class MissedSubsetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.old, self.account = self.fixture.ready(target_id=123, start=False)
        due = datetime.fromtimestamp(self.engine.clock(), timezone.utc).isoformat()
        self.engine.set_items(
            self.old["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target="https://x.com/example/status/123",
                    text="specific reply",
                    scheduled_at=due,
                ),
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target="https://x.com/example/status/456",
                    text="other reply",
                    scheduled_at=due,
                ),
            ],
        )
        self.engine.start_task(self.old["id"], source="test_explicit_start")
        JobQueue(self.engine).sync(self.old["id"])
        self.new, _ = self.fixture.ready(
            account=self.account, target_id=123, start=False, channel="inherit"
        )
        self.old_id = self.engine.task(self.old["id"])["items"][0]["id"]
        self.other_id = self.engine.task(self.old["id"])["items"][1]["id"]
        self.new_id = self.engine.task(self.new["id"])["items"][0]["id"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (json.dumps({"reason": "delivery_window_elapsed"}), self.old_id),
            )
            db.execute(
                "UPDATE items SET status='confirmed',external_reference='https://x.com/example/status/999' WHERE id=?",
                (self.other_id,),
            )
            db.execute(
                "UPDATE execution_jobs SET state='missed',owner=NULL,lease_until=0,expires_at=? WHERE reference_id=?",
                (self.engine.clock() - 1, self.old_id),
            )
        self.cloud_state = "missed"

    def run_recovery(self, source=SOURCE):
        test = self

        class Cloud:
            def request(self, path, body):
                return {
                    "job": {
                        "id": body["id"],
                        "state": test.cloud_state,
                        "account_id": test.account["id"],
                        "pool": "simulation.x",
                    }
                }

        with patch(
            "auto_promotion.explicit_missed_recovery.CloudControl.configured", return_value=Cloud()
        ):
            return recover(
                self.engine,
                self.old["id"],
                self.new["id"],
                [(self.old_id, self.new_id)],
                source=source,
            )

    def blocker(self):
        with self.store.connect() as db:
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (self.account["id"],)
            ).fetchone()
            task = db.execute("SELECT * FROM tasks WHERE id=?", (self.new["id"],)).fetchone()
            item = db.execute("SELECT * FROM items WHERE id=?", (self.new_id,)).fetchone()
            return intent_blocker(db, account, task, json.loads(item["payload"]), self.new_id)

    def test_transfers_only_missed_item_in_mixed_completed_batch(self):
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")
        self.assertEqual(self.run_recovery()["transferred"], 1)
        self.assertIsNone(self.blocker())
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.other_id,))["status"],
            "confirmed",
        )
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.run_recovery()["transferred"], 0)

    def test_cloud_pending_is_never_transferred(self):
        self.cloud_state = "pending"
        with self.assertRaisesRegex(ValueError, "CloudNotTerminal"):
            self.run_recovery()
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")

    def test_transferred_item_runs_once_and_original_stays_missed(self):
        self.run_recovery()
        self.engine.start_task(self.new["id"], source="test_explicit_start")
        JobQueue(self.engine).sync(self.new["id"])
        run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        self.assertEqual(self.engine.task(self.new["id"])["summary"], {"confirmed": 1})
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_id,))["status"],
            "missed",
        )
        self.assertEqual(
            self.store.one("SELECT COUNT(*) n FROM attempts WHERE item_id=?", (self.old_id,))["n"],
            0,
        )
        self.assertEqual(
            self.store.one("SELECT COUNT(*) n FROM attempts WHERE item_id=?", (self.new_id,))["n"],
            1,
        )
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "second"))

    def test_attempt_or_changed_content_prevents_transfer(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET attempt_id='unknown-attempt' WHERE id=?", (self.old_id,))
        with self.assertRaisesRegex(ValueError, "OriginalNotProvenUnsent"):
            self.run_recovery()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET attempt_id=NULL WHERE id=?", (self.old_id,))
            row = db.execute("SELECT payload FROM items WHERE id=?", (self.new_id,)).fetchone()
            value = json.loads(row[0])
            value["text"] = "changed"
            db.execute(
                "UPDATE items SET payload=?,payload_hash=? WHERE id=?",
                (json.dumps(value), digest(value), self.new_id),
            )
        with self.assertRaisesRegex(ValueError, "PayloadMismatch"):
            self.run_recovery()

    def test_explicit_scope_is_required(self):
        with self.assertRaisesRegex(ValueError, "RequiresExplicitScope"):
            self.run_recovery(source="automatic")

    def test_batch_is_atomic_if_one_replacement_changes_text(self):
        due = datetime.fromtimestamp(self.engine.clock(), timezone.utc).isoformat()
        self.engine.set_items(
            self.new["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target="https://x.com/example/status/123",
                    text="specific reply",
                    scheduled_at=due,
                ),
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target="https://x.com/example/status/456",
                    text="changed reply",
                    scheduled_at=due,
                ),
            ],
        )
        new_ids = [row["id"] for row in self.engine.task(self.new["id"])["items"]]
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',external_reference=NULL,result=? WHERE id=?",
                (json.dumps({"reason": "delivery_window_elapsed"}), self.other_id),
            )
            db.execute(
                "UPDATE execution_jobs SET state='missed',owner=NULL,lease_until=0,expires_at=? WHERE reference_id=?",
                (self.engine.clock() - 1, self.other_id),
            )
        test = self

        class Cloud:
            def request(self, path, body):
                return {
                    "job": {
                        "id": body["id"],
                        "state": "missed",
                        "account_id": test.account["id"],
                        "pool": "simulation.x",
                    }
                }

        with patch(
            "auto_promotion.explicit_missed_recovery.CloudControl.configured", return_value=Cloud()
        ):
            with self.assertRaisesRegex(ValueError, "PayloadMismatch"):
                recover(
                    self.engine,
                    self.old["id"],
                    self.new["id"],
                    [(self.old_id, new_ids[0]), (self.other_id, new_ids[1])],
                    source=SOURCE,
                )
        self.assertEqual(
            self.store.one("SELECT item_id FROM interaction_intents WHERE source_event_id='123'")[
                "item_id"
            ],
            self.old_id,
        )
        self.assertEqual(
            self.store.one("SELECT item_id FROM interaction_intents WHERE source_event_id='456'")[
                "item_id"
            ],
            self.other_id,
        )


if __name__ == "__main__":
    unittest.main()
