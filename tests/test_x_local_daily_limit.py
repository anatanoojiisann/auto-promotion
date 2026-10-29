import json
import sqlite3
import unittest

from auto_promotion.account_daily_local import KEY, enabled, item_uses_local_channel
from auto_promotion.models import ExecutionSettings
from auto_promotion.quotas import Calendar, scopes


class LocalDailyLimitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute(
            "INSERT INTO meta VALUES(?,?)",
            ("execution_settings", ExecutionSettings().model_dump_json()),
        )
        self.account = {
            "id": "a",
            "mode": "live",
            "platform": "x",
            "daily_limit": 5,
            "hourly_limit": 3,
        }
        self.task = {
            "id": "t",
            "policy": {
                "account_daily_limit": 5,
                "daily_limit": 100,
                "target_daily_limit": 1,
                "user_daily_limit": 1,
            },
        }
        self.payload = {"target": "https://x.com/u/status/1", "recipient_id": "u"}
        self.now = 1800000000

    def limits(self, **kwargs):
        return scopes(self.db, self.account, self.task, self.payload, **kwargs)

    def daily(self, **kwargs):
        return next(v["daily"] for scope, _, v in self.limits(**kwargs) if scope == "account")

    def test_unlimited_x_only_and_cloud_payload_preserves_original(self):
        self.assertEqual(self.daily(), 5)
        self.db.execute("INSERT INTO meta VALUES(?,?)", (KEY, "1"))
        self.assertIsNone(self.daily())
        self.assertEqual(self.daily(include_local_override=False), 5)
        for platform in ("telegram", "reddit", "facebook", "instagram", "discord", "tiktok"):
            self.account["platform"] = platform
            self.assertEqual(self.daily(), 5)

    def test_invalid_value_fails_closed(self):
        self.db.execute("INSERT INTO meta VALUES(?,?)", (KEY, "true"))
        self.assertFalse(enabled(self.db, "x"))
        self.assertEqual(self.daily(), 5)

    def test_hourly_task_and_target_limits_are_preserved(self):
        self.db.execute("INSERT INTO meta VALUES(?,?)", (KEY, "1"))
        limits = self.limits()
        day = [{"mode": "live", "account": "a", "at": self.now - 5000 - i * 10} for i in range(5)]
        self.assertIsNone(Calendar(day).blocker(limits, self.now, "live", future=False)[0])
        hour = [{"mode": "live", "account": "a", "at": self.now - i * 20} for i in range(3)]
        self.assertEqual(
            Calendar(hour).blocker(limits, self.now, "live", future=False)[0],
            "account_hourly_limit",
        )
        task_limits = [
            (k, key, {"daily": 1, "hourly": 100000}) if k == "task" else (k, key, v)
            for k, key, v in limits
        ]
        self.assertEqual(
            Calendar([{"mode": "live", "task": "t", "at": self.now - 5000}]).blocker(
                task_limits, self.now, "live", future=False
            )[0],
            "task_daily_limit",
        )
        target = next(key for k, key, _ in limits if k == "target")
        self.assertEqual(
            Calendar([{"mode": "live", "target": target, "at": self.now - 5000}]).blocker(
                limits, self.now, "live", future=False
            )[0],
            "target_daily_limit",
        )

    def test_channel_guard_requires_current_local_claim(self):
        self.db.executescript("""
          CREATE TABLE execution_jobs(id TEXT,kind TEXT,reference_id TEXT,account_id TEXT,state TEXT,lease_until REAL,owner TEXT);
          CREATE TABLE task_channel_claims(job_id TEXT,owner TEXT,generation TEXT,channel TEXT);
          CREATE TABLE account_task_channels(account_id TEXT,state TEXT,until_at REAL,generation TEXT,channel TEXT);
          CREATE TABLE items(id TEXT,task_id TEXT);
          INSERT INTO items VALUES('i','t');
          INSERT INTO execution_jobs VALUES('j','send','i','a','leased',1800000030,'w');
          INSERT INTO task_channel_claims VALUES('j','w','g','cloud');
          INSERT INTO account_task_channels VALUES('a','ready',1800000030,'g','cloud');
        """)
        self.assertFalse(item_uses_local_channel(self.db, "i", "a", self.now))
        self.db.execute("UPDATE task_channel_claims SET channel='local'")
        self.assertFalse(item_uses_local_channel(self.db, "i", "a", self.now))
        self.db.execute("UPDATE account_task_channels SET channel='local'")
        self.assertTrue(item_uses_local_channel(self.db, "i", "a", self.now))
        self.assertFalse(item_uses_local_channel(self.db, "i", "a", self.now + 31))

    def test_legacy_scope_must_match_account_and_validity(self):
        self.db.executescript("""
          CREATE TABLE execution_jobs(id TEXT,kind TEXT,reference_id TEXT,account_id TEXT,state TEXT,lease_until REAL,owner TEXT);
          CREATE TABLE task_channel_claims(job_id TEXT,owner TEXT,generation TEXT,channel TEXT);
          CREATE TABLE account_task_channels(account_id TEXT,state TEXT,until_at REAL,generation TEXT,channel TEXT);
          CREATE TABLE items(id TEXT,task_id TEXT);
          INSERT INTO items VALUES('i','t');
        """)
        state = {
            "active": "local",
            "authorization": "local_queue",
            "until": self.now + 30,
            "account_ids": ["a"],
            "through_task_ids": ["t"],
        }
        self.db.execute("INSERT INTO meta VALUES(?,?)", ("execution_channel", json.dumps(state)))
        self.assertTrue(item_uses_local_channel(self.db, "i", "a", self.now))
        self.assertFalse(item_uses_local_channel(self.db, "i", "b", self.now))
        self.assertFalse(item_uses_local_channel(self.db, "i", "a", self.now + 31))


if __name__ == "__main__":
    unittest.main()
