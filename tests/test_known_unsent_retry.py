import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.interaction_intents import authorize_known_unsent_retry, intent_blocker
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue
from auto_promotion.quotas import check_claim, event
from auto_promotion.store import Store


class KnownUnsentRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.old, self.account = self.fixture.ready(target_id=987)
        self.new, _ = self.fixture.ready(
            account=self.account, target_id=987, start=False, channel="local"
        )
        JobQueue(self.engine).sync(self.old["id"])
        self.old_id = self.engine.task(self.old["id"])["items"][0]["id"]
        self.new_id = self.engine.task(self.new["id"])["items"][0]["id"]
        self.attempt_id = "attempt-prior"
        self.result = {
            "status": "failed_known",
            "verified": False,
            "confirmed_not_submitted": True,
            "submission_started": False,
        }
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
            db.execute(
                "UPDATE items SET status='failed_known',attempt_id=?,result=? WHERE id=?",
                (self.attempt_id, json.dumps(self.result), self.old_id),
            )
            db.execute(
                "UPDATE execution_jobs SET state='failed_known',owner=NULL,lease_until=0 WHERE reference_id=?",
                (self.old_id,),
            )
            db.execute(
                "UPDATE interaction_intents SET state='claimed' WHERE item_id=?", (self.old_id,)
            )
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    self.attempt_id,
                    self.old_id,
                    self.account["id"],
                    self.old["id"],
                    "target",
                    "user",
                    "simulation",
                    self.engine.clock() - 100,
                    self.engine.clock() - 90,
                    "failed_known",
                ),
            )
            receipt = {
                "state": "failed_known",
                "attempt_id": self.attempt_id,
                "result": self.result,
            }
            db.execute(
                "INSERT INTO cloud_receipt_history(job_id,lease,outcome) VALUES(?,?,?)",
                ("send:" + self.old_id, "{}", json.dumps(receipt)),
            )

    def authorize(self, source="explicit_user_known_unsent_local_retry"):
        with self.store.connect(write=True) as db:
            return authorize_known_unsent_retry(
                db, self.old_id, self.new_id, self.engine.clock(), source=source
            )

    def blocker(self):
        with self.store.connect() as db:
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (self.account["id"],)
            ).fetchone()
            task = db.execute("SELECT * FROM tasks WHERE id=?", (self.new["id"],)).fetchone()
            item = db.execute("SELECT * FROM items WHERE id=?", (self.new_id,)).fetchone()
            return intent_blocker(db, account, task, json.loads(item["payload"]), self.new_id)

    def seed_native_proof(self):
        from auto_promotion.backends.common import Receipts
        from auto_promotion.final_receipts import binding
        from auto_promotion.models import digest

        env = patch.dict(
            os.environ, {"AP_ACCOUNTS_DIR": str(Path(self.fixture.temp.name) / "accounts")}
        )
        env.start()
        self.addCleanup(env.stop)
        at = self.engine.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash='synthetic-identity' WHERE id=?",
                (self.account["id"],),
            )
            db.execute("UPDATE items SET claimed_at=? WHERE id=?", (at - 100, self.old_id))
            db.execute("DELETE FROM cloud_receipt_history WHERE job_id=?", ("send:" + self.old_id,))
            db.execute(
                "INSERT OR REPLACE INTO task_channel_claims VALUES(?,?,?,?)",
                ("send:" + self.old_id, "synthetic-owner", "generation", "local"),
            )
            state = {
                "active": "local",
                "authorization": "local_queue",
                "offline_only": True,
                "until": at + 1000,
                "account_ids": [self.account["id"]],
            }
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES('execution_channel',?)", (json.dumps(state),)
            )
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES('local_channel_authority',?)",
                (json.dumps({"account_ids": state["account_ids"]}),),
            )
            Store.audit(
                db,
                "execution.job_channel",
                {"job_id": "send:" + self.old_id, "channel": "local"},
                self.old["id"],
                at - 100,
            )
            item = db.execute("SELECT * FROM items WHERE id=?", (self.old_id,)).fetchone()
            task = db.execute("SELECT * FROM tasks WHERE id=?", (self.old["id"],)).fetchone()
            claim = {
                "item_id": self.old_id,
                "task_id": self.old["id"],
                "attempt_id": self.attempt_id,
                "payload": json.loads(item["payload"]),
                "manifest_hash": task["manifest_hash"],
            }
            bound = binding(
                db,
                claim,
                self.account["id"],
                "x",
                self.account["backend_label"],
                "send:" + self.old_id,
            )
        self.assertIsNotNone(bound)
        receipt = Receipts("x", self.account["backend_label"])
        key = "execute:" + self.old_id
        receipt.begin(key, claim, binding=bound)
        final_at = at - 80
        envelope = {
            "receipt_binding": bound,
            "receipt_finalized_at": final_at,
            "receipt_final_result": self.result,
            "receipt_final_hash": digest(
                {"binding": bound, "result": self.result, "finalized_at": final_at}
            ),
        }
        receipt.save(key, {**self.result, **envelope})
        return receipt, key, envelope

    def test_only_proven_unsent_original_transfers_and_preserves_attempt(self):
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")
        self.assertEqual(self.authorize()["new_item_id"], self.new_id)
        self.assertIsNone(self.blocker())
        self.assertEqual(
            self.store.one("SELECT state FROM interaction_intents WHERE item_id=?", (self.new_id,))[
                "state"
            ],
            "reserved",
        )
        self.assertEqual(
            self.store.one("SELECT outcome FROM attempts WHERE id=?", (self.attempt_id,))[
                "outcome"
            ],
            "failed_known",
        )
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_id,))["status"],
            "failed_known",
        )

    def test_requires_explicit_instruction_and_exact_body(self):
        with self.assertRaisesRegex(ValueError, "ExplicitUser"):
            self.authorize(source="automatic")
        with self.store.connect(write=True) as db:
            item = db.execute("SELECT payload FROM items WHERE id=?", (self.new_id,)).fetchone()
            payload = json.loads(item[0])
            payload["text"] = "different"
            db.execute("UPDATE items SET payload=? WHERE id=?", (json.dumps(payload), self.new_id))
        with self.assertRaisesRegex(ValueError, "PayloadMismatch"):
            self.authorize()

    def test_unknown_submitted_or_conflicting_receipt_never_transfers(self):
        for bad in (
            {"confirmed_not_submitted": True, "submission_started": True},
            {"confirmed_not_submitted": False, "submission_started": False},
            {"confirmed_not_submitted": True},
        ):
            with self.subTest(bad=bad):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET result=? WHERE id=?", (json.dumps(bad), self.old_id)
                    )
                with self.assertRaisesRegex(ValueError, "SubmissionNotDisproved"):
                    self.authorize()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET result=? WHERE id=?", (json.dumps(self.result), self.old_id)
            )
            db.execute(
                "UPDATE cloud_receipt_history SET outcome=? WHERE job_id=?",
                (json.dumps({"state": "unknown"}), "send:" + self.old_id),
            )
        with self.assertRaisesRegex(ValueError, "ReceiptEvidenceMismatch"):
            self.authorize()

    def test_missing_receipt_or_changed_original_keeps_lock(self):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM cloud_receipt_history WHERE job_id=?", ("send:" + self.old_id,))
        with self.assertRaisesRegex(ValueError, "ReceiptEvidenceMissing"):
            self.authorize()
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")

    def test_offline_native_final_receipt_allows_only_exact_unsent_transfer(self):
        self.seed_native_proof()
        self.authorize()
        self.assertIsNone(self.blocker())
        row = self.store.one(
            "SELECT details FROM audit WHERE kind='interaction.explicit_unsent_retry_authorized' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(json.loads(row["details"])["evidence_source"], "native_final_receipt")
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_id,))["status"],
            "failed_known",
        )

    def test_native_progress_or_tampered_receipt_never_authorizes_retry(self):
        receipt, key, envelope = self.seed_native_proof()
        for changed in (
            {**envelope, "receipt_final_hash": "tampered"},
            {k: v for k, v in envelope.items() if k != "receipt_finalized_at"},
        ):
            with self.subTest(changed=changed):
                receipt.save(key, {**self.result, **changed})
                with self.assertRaisesRegex(ValueError, "ReceiptEvidenceMissing"):
                    self.authorize()
        receipt.save(key, {**self.result, **envelope})
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE task_channel_claims SET channel='cloud' WHERE job_id=?",
                ("send:" + self.old_id,),
            )
        with self.assertRaisesRegex(ValueError, "ReceiptEvidenceMissing"):
            self.authorize()
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")

    def test_native_envelope_conflicting_with_central_result_never_transfers(self):
        from auto_promotion.models import digest

        receipt, key, envelope = self.seed_native_proof()
        conflicting = {**self.result, "submission_started": True, "confirmed_not_submitted": False}
        envelope["receipt_final_result"] = conflicting
        envelope["receipt_final_hash"] = digest(
            {
                "binding": envelope["receipt_binding"],
                "result": conflicting,
                "finalized_at": envelope["receipt_finalized_at"],
            }
        )
        receipt.save(key, {**conflicting, **envelope})
        with self.assertRaisesRegex(ValueError, "ReceiptEvidenceMissing"):
            self.authorize()
        self.assertEqual(self.blocker()["reason"], "interaction_already_claimed")

    def test_mutated_historical_evidence_is_not_accepted_at_claim(self):
        self.authorize()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET result=? WHERE id=?",
                (json.dumps({**self.result, "submission_started": True}), self.old_id),
            )
        self.assertEqual(self.blocker()["reason"], "interaction_history_unresolved_or_published")

    def test_authorized_retry_executes_once_without_erasing_original_attempt(self):
        self.authorize()
        self.engine.start_task(self.new["id"], source="test_explicit_start")
        JobQueue(self.engine).sync(self.new["id"])
        run_pool(self.engine, "simulation.x", "node", max_jobs=1)
        self.assertEqual(self.engine.task(self.new["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 2)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_id,))["status"],
            "failed_known",
        )
        self.assertIsNone(JobQueue(self.engine).claim("simulation.x", "second"))

    def test_old_no_submit_does_not_count_as_a_target_or_user_delivery(self):
        with self.store.connect(write=True) as db:
            old = db.execute("SELECT * FROM items WHERE id=?", (self.old_id,)).fetchone()
            payload = json.loads(old["payload"])
            row = event(
                {"id": self.account["id"], "platform": "x", "mode": "simulation"},
                {"id": self.old["id"]},
                payload,
                self.engine.clock() - 100,
            )
            db.execute(
                "UPDATE attempts SET target_key=?,user_key=? WHERE id=?",
                (row["target"], row["user"], self.attempt_id),
            )
        self.authorize()
        with self.store.connect() as db:
            account = dict(
                db.execute("SELECT * FROM accounts WHERE id=?", (self.account["id"],)).fetchone()
            )
            account["hourly_limit"] = 20
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (self.new["id"],)).fetchone())
            policy = json.loads(task["policy"])
            policy.update(target_daily_limit=1, user_daily_limit=1)
            task["policy"] = json.dumps(policy)
            item = db.execute("SELECT * FROM items WHERE id=?", (self.new_id,)).fetchone()
            payload = json.loads(item["payload"])
            self.assertIsNone(
                check_claim(
                    db, account, task, payload, item["due_at"], self.engine.clock(), self.new_id
                )
            )
            account["hourly_limit"] = 1
            self.assertEqual(
                check_claim(
                    db, account, task, payload, item["due_at"], self.engine.clock(), self.new_id
                ),
                "activity_limit",
            )
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT payload,payload_hash,due_at FROM items WHERE id=?", (self.old_id,)
            ).fetchone()
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,attempt_id,external_reference) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "item-other",
                    self.old["id"],
                    self.account["id"],
                    1,
                    old["payload"],
                    old["payload_hash"],
                    old["due_at"] + 5,
                    "confirmed",
                    "attempt-other",
                    "https://x.com/u/status/42",
                ),
            )
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt-other",
                    "item-other",
                    self.account["id"],
                    self.old["id"],
                    row["target"],
                    row["user"],
                    "simulation",
                    self.engine.clock() - 50,
                    self.engine.clock() - 40,
                    "confirmed",
                ),
            )
        with self.store.connect() as db:
            account["hourly_limit"] = 20
            self.assertEqual(
                check_claim(
                    db, account, task, payload, item["due_at"], self.engine.clock(), self.new_id
                ),
                "activity_limit",
            )


if __name__ == "__main__":
    unittest.main()
