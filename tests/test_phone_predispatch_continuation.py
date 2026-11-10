import json
import time
import unittest

import test_meta_browser as fixtures

from auto_promotion.meta_browser import claim_blocker
from auto_promotion.meta_migration import register
from auto_promotion.models import canonical, digest
from auto_promotion.phone_predispatch_continuation import record_scope
from auto_promotion.phone_social import SCHEMA


class PhonePredispatchContinuationTests(unittest.TestCase):
    setUp = fixtures.MetaBrowserTests.setUp
    write_source = fixtures.MetaBrowserTests.write_source
    identity = fixtures.MetaBrowserTests.identity
    ready = fixtures.MetaBrowserTests.ready

    def setup_case(self):
        self.aid = register(self.browser, [{
            "platform": "instagram", "account_id": "instagram:existing_ig", "label": "Existing IG",
            "details": {"connector": "edge_browser_use", "profile_url": "https://www.instagram.com/existing_ig/",
                        "workflow_paths": [str(self.source)], "scope": "Existing IG"},
        }])[0]
        self.old_task, self.old = self.ready("https://www.instagram.com/reel/Old123/")
        self.new_task, self.new = self.ready("https://www.instagram.com/reel/New456/")
        self.now = time.time()
        self.attempt = "attempt-original"
        self.auth = "phone-social-submit:original"
        with self.store.connect(write=True) as db:
            db.executescript(SCHEMA)
            # Seed a completed historical attempt in this throwaway test database.
            db.execute("DROP TRIGGER IF EXISTS pooled_send_attempt")
            db.execute("DROP TRIGGER IF EXISTS task_channel_send_attempt_v2")
            for task in (self.old_task, self.new_task):
                row = db.execute("SELECT policy FROM tasks WHERE id=?", (task["id"],)).fetchone()
                policy = json.loads(row["policy"])
                policy.update(execution_method="phone", execution_channel="local")
                db.execute("UPDATE tasks SET policy=? WHERE id=?", (canonical(policy), task["id"]))
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old_task["id"],))
            db.execute("INSERT OR IGNORE INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,state,created_at,updated_at) "
                       "VALUES(?,'send',?,?,?,'instagram.phone','{}','v1',?,?,'unknown',?,?)",
                       ("send:" + self.old["id"], self.old["id"], self.old_task["id"], self.aid,
                        self.now - 10, self.now + 100, self.now - 10, self.now - 8))
            db.execute("UPDATE execution_jobs SET state='unknown',owner=NULL,lease_until=0 WHERE task_id=?", (self.old_task["id"],))
            db.execute("UPDATE items SET status='unknown',attempt_id=?,claimed_at=?,result=? WHERE id=?",
                       (self.attempt, self.now - 10, canonical({"status": "unknown", "reason": "transport_outcome_unknown"}), self.old["id"]))
            db.execute("INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) "
                       "VALUES(?,?,?,?,?,?,'live',?,?,'unknown')",
                       (self.attempt, self.old["id"], self.aid, self.old_task["id"], "target", "user", self.now - 10, self.now - 8))
            db.execute("INSERT INTO calls(at,platform,operation,status,duration_ms,task_id,error_code) "
                       "VALUES(?,'instagram','execute','error',1,?,'PhoneFleetDeviceNotReady')",
                       (self.now - 9, self.old_task["id"]))
            claim = {"account_id": self.aid, "platform": "instagram", "payload": {
                "item_id": self.old["id"], "task_id": self.old_task["id"], "attempt_id": self.attempt}}
            context = {"platform": "instagram", "action": "comment", "target_url": self.old["payload"]["target"],
                       "text": self.old["payload"]["text"]}
            db.execute("INSERT INTO phone_social_authorizations(id,item_id,request_hash,claim,context,device_id,expires_at) "
                       "VALUES(?,?,?,?,?,?,?)", (self.auth, self.old["id"], "hash", canonical(claim), canonical(context), "device", self.now - 2))

    def blocker(self, db=None, now=None):
        if db is None:
            with self.store.connect() as db:
                return self.blocker(db, now)
        return claim_blocker(db, self.engine.account(self.aid), self.new["payload"], self.new["id"], self.now if now is None else now)

    def scope(self):
        return record_scope(self.store, self.old["id"], self.new["id"], "User requested a different Instagram target", now=self.now)

    def test_explicit_scope_allows_only_new_target_and_preserves_unknown(self):
        self.setup_case()
        self.assertEqual(self.blocker(), "meta_outcome_unknown")
        self.scope()
        self.assertIsNone(self.blocker())
        self.assertEqual(self.store.one("SELECT status FROM items WHERE id=?", (self.old["id"],))["status"], "unknown")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts WHERE item_id=?", (self.old["id"],))["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts WHERE item_id=?", (self.new["id"],))["n"], 0)
        self.assertEqual(self.blocker(now=self.now + 1201), "meta_outcome_unknown")

    def test_changed_or_unsafe_evidence_reblocks(self):
        self.setup_case()
        self.scope()
        cases = [
            ("UPDATE phone_social_authorizations SET used_at=? WHERE id=?", (self.now, self.auth)),
            ("UPDATE phone_social_authorizations SET native_id='native' WHERE id=?", (self.auth,)),
            ("UPDATE phone_social_authorizations SET execution_id='execution' WHERE id=?", (self.auth,)),
            ("UPDATE phone_social_authorizations SET expires_at=? WHERE id=?", (self.now + 20, self.auth)),
            ("UPDATE calls SET error_code='NetworkError' WHERE task_id=? AND operation='execute'", (self.old_task["id"],)),
            ("UPDATE execution_jobs SET state='leased',owner='worker',lease_until=? WHERE task_id=?", (self.now + 20, self.old_task["id"])),
            ("INSERT INTO phone_social_operations(id,account_id,item_id,kind,request,phase,created_at) VALUES(?,?,?,'social_submit','{}','dispatching',?)", (self.auth, self.aid, self.old["id"], self.now)),
        ]
        with self.store.connect(write=True) as db:
            for sql, args in cases:
                with self.subTest(sql=sql):
                    db.execute("SAVEPOINT mutation")
                    db.execute(sql, args)
                    self.assertEqual(self.blocker(db), "meta_outcome_unknown")
                    db.execute("ROLLBACK TO mutation")
                    db.execute("RELEASE mutation")

    def test_same_target_and_missing_instruction_rejected(self):
        self.setup_case()
        with self.assertRaises(ValueError):
            record_scope(self.store, self.old["id"], self.new["id"], "", now=self.now)
        with self.store.connect(write=True) as db:
            payload = {**self.new["payload"], "target": "https://www.instagram.com/p/Old123/"}
            db.execute("UPDATE items SET payload=?,payload_hash=? WHERE id=?", (canonical(payload), digest(payload), self.new["id"]))
        with self.assertRaises(ValueError):
            self.scope()

    def test_changed_candidate_still_blocks(self):
        self.setup_case()
        self.scope()
        with self.store.connect(write=True) as db:
            payload = {**self.new["payload"], "target": "https://www.instagram.com/reel/Other789/"}
            db.execute("UPDATE items SET payload=?,payload_hash=? WHERE id=?", (canonical(payload), digest(payload), self.new["id"]))
            self.assertEqual(self.blocker(db), "meta_outcome_unknown")

    def test_unrelated_unknown_still_blocks(self):
        self.setup_case()
        self.scope()
        _, other = self.ready("https://www.instagram.com/reel/Other789/")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (other["id"],))
            self.assertEqual(self.blocker(db), "meta_outcome_unknown")


if __name__ == "__main__":
    unittest.main()
