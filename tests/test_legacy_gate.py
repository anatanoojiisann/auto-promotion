import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from auto_promotion.provider_bridge import legacy_gate


class LegacyGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "legacy.db"

    def meta(self, stopped=False, limit=2, cooldown=0):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(
                "CREATE TABLE system_settings(key,value_json); CREATE TABLE account_action_limits(platform,account_id,action_type,daily_limit,blocked_until_epoch); CREATE TABLE outbound_attempts(item_id,submitted,created_at); CREATE TABLE outbound_items(item_id,platform,account_id,action_type);"
            )
            db.execute(
                "INSERT INTO system_settings VALUES(?,?)",
                ("global_stop", json.dumps({"stopped": stopped})),
            )
            if limit is not None:
                db.execute(
                    "INSERT INTO account_action_limits VALUES('instagram','key','publish_content',?,?)",
                    (limit, cooldown),
                )
        with patch.dict(os.environ, {"META_SOCIAL_DB_PATH": str(self.path)}):
            return legacy_gate("instagram", self.root, "key", "publish_content")

    def test_meta_global_stop_is_respected(self):
        self.assertFalse(self.meta(stopped=True)["allowed"])

    def test_meta_missing_limit_blocks(self):
        self.assertFalse(self.meta(limit=None)["allowed"])

    def test_meta_cooldown_blocks(self):
        self.assertFalse(self.meta(cooldown=9999999999)["allowed"])

    def test_meta_returns_remaining_daily_capacity(self):
        self.assertEqual(self.meta(), {"allowed": True, "daily_remaining": 2})

    def test_x_preserves_stop_cooldown_and_limits_without_writing(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(
                "CREATE TABLE system_control(control_id,stopped); INSERT INTO system_control VALUES(1,1); CREATE TABLE action_limits(account_id,action_type,daily_limit); INSERT INTO action_limits VALUES('user','reply',3); CREATE TABLE action_events(job_id,event_type,created_at); CREATE TABLE action_jobs(job_id,account_id,action_type); CREATE TABLE action_pacing(account_id,blocked_until_epoch,next_allowed_epoch); INSERT INTO action_pacing VALUES('user',0,0);"
            )
        with patch.dict(os.environ, {"X_ENGAGEMENT_DB": str(self.path)}):
            self.assertFalse(legacy_gate("x", self.root, "user", "reply")["allowed"])
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE system_control SET stopped=0")
            self.assertEqual(
                legacy_gate("x", self.root, "user", "reply"),
                {"allowed": True, "daily_remaining": 3},
            )
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE action_pacing SET blocked_until_epoch=9999999999")
            self.assertFalse(legacy_gate("x", self.root, "user", "reply")["allowed"])
            self.assertFalse(legacy_gate("x", self.root, "unknown", "reply")["allowed"])
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM action_events").fetchone()[0], 0)

    def test_missing_database_is_not_created(self):
        with patch.dict(os.environ, {"X_ENGAGEMENT_DB": str(self.path)}):
            with self.assertRaises(sqlite3.OperationalError):
                legacy_gate("x", self.root, "user", "reply")
        self.assertFalse(self.path.exists())
