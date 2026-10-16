import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from auto_promotion.config import database_path
from auto_promotion.models import canonical
from auto_promotion.statistics import Statistics, day_bounds, record_publication
from auto_promotion.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "data.db")
        self.now = stamp("2026-10-13T21:00:00+08:00")
        self.stats = Statistics(self.store, clock=lambda: self.now)
        self.sequence = 0

    def item(
        self,
        *,
        platform="telegram",
        mode="live",
        status="confirmed",
        due=None,
        published=None,
        action="send_message",
        verified=True,
        reference="https://example.com/message",
    ):
        self.sequence += 1
        key = str(self.sequence)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,5,3,0)",
                (key, platform, key, key, mode),
            )
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, key, mode, canonical([key]), "[]", "{}", "hash", self.now, self.now),
            )
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,0,?,?,?,?,?,?)",
                (
                    key,
                    key,
                    key,
                    canonical({"action": action}),
                    "hash",
                    due or self.now - 3600,
                    status,
                    reference,
                    canonical({"verified": verified, "status": status}),
                ),
            )
            if status == "confirmed" and published is not None:
                record_publication(db, key, {"published_at": published}, self.now)
        return key

    def test_all_live_tasks_and_business_states_without_simulation_or_transport_success(self):
        self.item(published=self.now - 4000)
        self.item(platform="x", published=self.now - 3000, action="reply")
        self.item(status="scheduled", published=self.now - 3000)
        self.item(status="unknown")
        self.item(status="cancelled")
        self.item(mode="simulation", published=self.now - 4000)
        self.item(verified=False, published=self.now - 4000)
        self.item(reference="simulation://test", published=self.now - 4000)
        data = self.stats.snapshot(day="2026-10-13")
        self.assertEqual(data["totals"]["published_success"], 2)
        self.assertEqual(data["totals"]["pending_verification"], 2)
        self.assertIsNone(data["totals"]["received_replies"])
        self.assertEqual(data["scope"], "all_live_tasks")
        self.assertEqual(len(data["platforms"]), 6)

    def test_midnight_and_actual_publication_date_not_planned_or_verification_time(self):
        midnight, _ = day_bounds("2026-10-13")
        self.item(due=midnight - 300, published=midnight + 1)
        self.item(due=midnight + 300, published=midnight - 1)
        self.item(published=midnight)
        self.item()  # historical confirmation has no actual platform timestamp
        today = self.stats.snapshot(day="2026-10-13")["totals"]
        yesterday = self.stats.snapshot(day="2026-10-12")["totals"]
        self.assertEqual(today["published_success"], 2)
        self.assertEqual(yesterday["published_success"], 1)
        self.assertEqual(today["confirmed_without_publication_time"], 1)

    def test_unknown_reply_coverage_dedup_self_exclusion_and_passive_action_denominator(self):
        key = self.item(published=self.now - 4000)
        self.item(platform="x", action="like", published=self.now - 3000)
        self.assertTrue(self.stats.record_feedback(key, "reply:42", self.now - 100))
        self.assertFalse(self.stats.record_feedback(key, "reply:42", self.now - 100))
        self.assertFalse(self.stats.record_feedback(key, "reply:self", self.now - 50, is_self=True))
        tg = self.stats.snapshot(day="2026-10-13")["platforms"][0]
        self.assertIsNone(tg["received_replies"])
        self.assertEqual(tg["observed_replies"], 1)
        self.stats.mark_feedback(key, self.now, "complete")
        tg = self.stats.snapshot(day="2026-10-13")["platforms"][0]
        self.assertEqual(tg["received_replies"], 1)
        self.assertEqual(tg["responded_interactions"], 1)
        self.assertEqual(tg["reply_rate"], 1)
        self.stats.mark_feedback(key, self.now, "failed")
        tg = self.stats.snapshot(day="2026-10-13")["platforms"][0]
        self.assertEqual(tg["feedback_status"], "failed")
        self.assertIsNone(tg["received_replies"])

    def test_reply_to_yesterday_counts_today_and_late_import_revises_saved_report(self):
        old = self.item(published=stamp("2026-10-12T23:00:00+08:00"))
        self.stats.run_due()
        self.stats.record_feedback(old, "late", stamp("2026-10-13T19:00:00+08:00"))
        self.stats.mark_feedback(old, self.now, "complete")
        data = self.stats.snapshot(day="2026-10-13")["totals"]
        self.assertEqual(data["received_replies"], 1)
        self.assertEqual(data["responded_interactions"], 0)
        self.stats.run_due()
        report = next(r for r in self.stats.reports() if r["kind"] == "daily_20")
        self.assertEqual(report["revision"], 2)
        self.assertEqual(report["content"]["daily"]["totals"]["received_replies"], 1)

    def test_idempotent_20_hour_cutoff_midnight_final_and_restart_catchup(self):
        self.item(published=stamp("2026-10-13T19:00:00+08:00"))
        self.item(published=stamp("2026-10-13T20:30:00+08:00"))
        self.stats.run_due()
        report = next(r for r in self.stats.reports() if r["kind"] == "daily_20")
        self.assertEqual(report["content"]["daily"]["totals"]["published_success"], 1)
        count = len(self.stats.reports())
        self.assertEqual(self.stats.run_due()["updated_reports"], 0)
        self.assertEqual(len(self.stats.reports()), count)
        self.now = stamp("2026-10-14T06:00:00+08:00")
        restarted = Statistics(self.store, clock=lambda: self.now)
        restarted.run_due()
        final = next(
            r
            for r in restarted.reports()
            if r["kind"] == "daily_final" and r["content"]["day"] == "2026-10-13"
        )
        self.assertEqual(final["content"]["daily"]["totals"]["published_success"], 2)
        intervals = [r for r in restarted.reports() if r["kind"] == "interval"]
        self.assertTrue(any(r["cutoff"] == self.now for r in intervals))

    def test_late_publication_evidence_updates_original_day_once(self):
        key = self.item(status="scheduled", due=stamp("2026-10-13T19:00:00+08:00"))
        self.stats.run_due()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',result=? WHERE id=?",
                (canonical({"verified": True}), key),
            )
            record_publication(
                db, key, {"published_at": stamp("2026-10-13T19:00:00+08:00")}, self.now
            )
        self.stats.run_due()
        report = next(r for r in self.stats.reports() if r["kind"] == "daily_20")
        self.assertEqual(report["revision"], 2)
        self.assertEqual(report["content"]["daily"]["totals"]["published_success"], 1)
        self.assertEqual(self.stats.run_due()["updated_reports"], 0)

    def test_concurrent_report_workers_and_long_offline_period_do_not_duplicate_or_skip(self):
        self.stats.run_due()
        self.now += 33 * 86400
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.stats.run_due(), range(2)))
        self.assertTrue(any(r["catch_up_pending"] for r in results))
        state = self.store.one("SELECT * FROM reporting_state")
        self.assertEqual(state["last_tick"], self.now)
        self.assertEqual(
            self.store.one("SELECT count(*) count FROM statistical_reports WHERE kind='daily_20'")[
                "count"
            ],
            34,
        )

    def test_invalid_future_timestamp_is_not_invented_and_reply_cannot_change_parent(self):
        first = self.item(published=self.now + 100)
        second = self.item(published=self.now - 100)
        self.assertIsNone(
            self.store.one(
                "SELECT published_at FROM publication_evidence WHERE item_id=?", (first,)
            )["published_at"]
        )
        self.stats.record_feedback(first, "one", self.now - 20)
        # Same event within an account cannot be attributed to two outbound items.
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET account_id=? WHERE id=?", (first, second))
        with self.assertRaises(ValueError):
            self.stats.record_feedback(second, "one", self.now - 10)
        with self.assertRaises(ValueError):
            self.stats.snapshot(day="2026-10-15")

    def test_default_database_selects_business_store_from_parent_workspace(self):
        root = Path(self.tmp.name) / "workspace"
        (root / "auto-promotion" / "src" / "auto_promotion").mkdir(parents=True)
        (root / "auto-promotion" / "pyproject.toml").write_text("")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("auto_promotion.config.Path.cwd", return_value=root),
        ):
            self.assertEqual(
                database_path(), (root / "auto-promotion" / "var" / "auto-promotion.db").resolve()
            )
        with patch.dict(os.environ, {"AP_DB": str(root / "explicit.db")}):
            self.assertEqual(database_path(), (root / "explicit.db").resolve())


if __name__ == "__main__":
    unittest.main()
