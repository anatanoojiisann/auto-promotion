import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

schedule = None
if Path("var/private/tiktok-dm-latest10-20261027/schedule.py").is_file():
    spec = importlib.util.spec_from_file_location(
        "dm_batch_schedule", Path("var/private/tiktok-dm-latest10-20261027/schedule.py")
    )
    schedule = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schedule)


@unittest.skipUnless(
    schedule is not None, "Optional private operations fixture is not in this checkout"
)
class TikTokSkippedScheduleTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "tmp" / "tiktok-schedule-tests"
        root.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = datetime(2026, 10, 28, 16, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.db = self.root / "test.db"
        self.state_path = self.root / "plan.json"
        self.patches = [
            patch.object(schedule, "DB", self.db),
            patch.object(schedule, "STATE", self.state_path),
            patch.object(schedule.time, "time", return_value=self.now),
            patch.object(schedule.secrets, "randbelow", return_value=0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                CREATE TABLE tasks(id TEXT, name TEXT, account_ids TEXT, state TEXT);
                CREATE TABLE items(id TEXT, task_id TEXT, account_id TEXT, payload TEXT,
                                   status TEXT, attempt_id TEXT, result TEXT);
                CREATE TABLE attempts(id TEXT, account_id TEXT, started_at REAL, finished_at REAL);
                CREATE TABLE meta_browser_claims(item_id TEXT, submission_started INTEGER);
                CREATE TABLE meta_browser_contacts(item_id TEXT, state TEXT);
                CREATE TABLE audit(task_id TEXT, kind TEXT, details TEXT);
                CREATE TABLE accounts(id TEXT, daily_limit INTEGER, hourly_limit INTEGER);
            """)
            db.execute("INSERT INTO accounts VALUES('acct',300,2)")
        self.entry = {
            "rank": 7,
            "recipient_id": "peer_one",
            "conversation_id": "0:1:9:10",
            "parent_message_id": "100",
            "text": "Test reply",
            "task_name": "task-seven",
            "task_id": "task-7",
            "status": "unknown",
        }
        self.next_entry = {
            "rank": 8,
            "recipient_id": "peer_two",
            "conversation_id": "0:1:11:12",
            "parent_message_id": "101",
            "text": "Other reply",
            "task_name": "task-eight",
            "status": "pending",
        }
        self.payload = {
            "account_id": "acct",
            "action": "reply_direct_message",
            "recipient_id": "peer_one",
            "target": "https://www.tiktok.com/@peer_one",
            "text": "Test reply",
            "tiktok_conversation_id": "0:1:9:10",
            "tiktok_message_id": "100",
        }
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?)",
                ("task-7", "task-seven", json.dumps(["acct"]), "completed"),
            )
            db.execute(
                "INSERT INTO items VALUES(?,?,?,?,?,?,?)",
                (
                    "item-7",
                    "task-7",
                    "acct",
                    json.dumps(self.payload),
                    "cancelled",
                    "attempt-7",
                    json.dumps({"reason": "UserSkippedUnknownSubmission"}),
                ),
            )
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?)",
                ("attempt-7", "acct", self.now - 7200, self.now - 1),
            )
            db.execute("INSERT INTO meta_browser_claims VALUES('item-7',1)")
            db.execute("INSERT INTO meta_browser_contacts VALUES('item-7','cancelled')")
            details = {
                "item_id": "item-7",
                "prior_status": "unknown",
                "submission_outcome": "unknown",
                "target_reservation_retained": True,
            }
            db.execute(
                "INSERT INTO audit VALUES(?,?,?)",
                ("task-7", "meta_browser.user_skipped_unknown", json.dumps(details)),
            )

    def state(self, entries=None):
        return {
            "account_id": "acct",
            "entries": entries or [self.entry.copy(), self.next_entry.copy()],
            "execution_authorized": True,
            "blocked": [],
            "target_count": 2,
        }

    def test_audited_unknown_is_counted_separately_and_next_item_is_ready(self):
        state = self.state()
        result = schedule.plan(state)
        self.assertEqual(result["mode"], "ready")
        self.assertEqual(
            (result["confirmed"], result["skipped_unknown"], result["remaining"]), (0, 1, 1)
        )
        self.assertEqual(result["entry"]["rank"], 8)
        self.assertEqual(state["anchor_key"], "item-7")
        self.assertEqual(state["pacing_at"], self.now - 5400)
        self.assertEqual(state["entries"][0]["status"], "skipped_unknown")

    def test_unknown_without_the_official_skip_still_stops_the_queue(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE items SET status='unknown',result='{}'")
        self.assertEqual(schedule.plan(self.state())["mode"], "verify_only")

    def test_skip_without_audit_does_not_unblock(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM audit")
        result = schedule.plan(self.state())
        self.assertEqual(
            (result["mode"], result["reason"]), ("blocked", "skipped_unknown_evidence_missing")
        )

    def test_processing_completion_does_not_mark_unknown_as_confirmed(self):
        state = self.state([self.entry.copy()])
        result = schedule.plan(state)
        self.assertEqual(result["mode"], "complete_with_unknown")
        self.assertEqual((result["confirmed"], result["skipped_unknown"]), (0, 1))
        self.assertTrue(state["processing_complete"])
        self.assertFalse(state["completed"])

    def test_platform_denial_blocks_creation_until_a_fresh_read_only_recheck(self):
        state = self.state()
        state["platform_block"] = {
            "reason": "BrowserAccessDenied",
            "status": "active",
            "retry_not_before": self.now + 600,
            "rank": 8,
        }
        self.assertEqual(schedule.plan(state)["mode"], "wait_platform")
        state["platform_block"]["retry_not_before"] = self.now - 1
        self.assertEqual(schedule.plan(state)["mode"], "recheck_platform")
        state["platform_block"]["status"] = "resolved"
        self.assertEqual(schedule.plan(state)["mode"], "ready")


if __name__ == "__main__":
    unittest.main()
