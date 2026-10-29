import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from auto_promotion.alert_relay import AlertSettings
from auto_promotion.alert_service import AlertService
from auto_promotion.alert_summary import (
    SUMMARY_POLICY,
    collect_summary,
    summary_cutoff,
    summary_notice,
)
from auto_promotion.models import canonical
from auto_promotion.store import Store


def stamp(value):
    return datetime.fromisoformat(value + "+08:00").timestamp()


class ThreeHourSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.business = Store(self.root / "business.db")
        self.now = stamp("2026-10-25T13:55:00")
        self.relay = Mock()
        self.relay.send.return_value = {
            "state": "accepted",
            "reason": "delivery_unverified",
            "http": 200,
        }
        self.service = self.new_service()
        self.sequence = 0

    def new_service(self):
        return AlertService(
            self.business.path,
            self.root / "alerts.db",
            settings=AlertSettings(key="fake-key", enabled=True),
            relay=self.relay,
            clock=lambda: self.now,
        )

    def tick(self, value=None):
        if value:
            self.now = stamp(value)
        self.assertTrue(self.service.collect())
        self.service.drain()

    def rows(self):
        with self.service.store.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM alert_outbox ORDER BY created_at,id")
            ]

    def item(
        self,
        *,
        platform="telegram",
        status="confirmed",
        when="2026-10-25T14:10:00",
        mode="live",
        verified=True,
        action="reply",
        reference="https://example.com/receipt",
        recorded=True,
        interaction_channel=None,
    ):
        self.sequence += 1
        key = str(self.sequence)
        at = stamp(when)
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,5,3,480)",
                (key, platform, key, key, mode),
            )
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,state,phase,manifest_hash,created_at,updated_at) VALUES(?,?,?,?,?,'{}','hash','active','ready','hash',?,?)",
                (key, key, mode, canonical([key]), "[]", at, at),
            )
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,0,?,'hash',?,?,?,?)",
                (
                    key,
                    key,
                    key,
                    canonical(
                        {
                            "action": action,
                            "text": "private outbound text",
                            "interaction_channel": interaction_channel,
                        }
                    ),
                    at - 1000,
                    status,
                    reference,
                    canonical({"verified": verified}),
                ),
            )
            if recorded:
                db.execute(
                    "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (key, key, key, key, "target", "user", mode, at - 5, at, status),
                )
            if status == "confirmed" and recorded:
                db.execute(
                    "INSERT INTO publication_evidence VALUES(?,?,?,'platform')", (key, at - 50, at)
                )
        return key

    def test_startup_and_progress_never_push_between_boundaries(self):
        self.tick()
        self.item()
        self.item(status="failed_known", platform="x")
        for value in ("2026-10-25T14:00:00", "2026-10-25T14:12:00", "2026-10-25T14:59:59"):
            self.tick(value)
        self.assertEqual(self.relay.send.call_count, 0)
        self.assertEqual(self.rows(), [])

    def test_exactly_one_compact_summary_and_no_notification_id(self):
        self.tick()
        self.item()
        self.item(status="failed_known")
        self.item(platform="x")
        self.tick("2026-10-25T15:00:01")
        text = self.relay.send.call_args.args[0]
        self.assertEqual(
            text.split("\n\n"),
            [
                "Telegram：发送 2 条｜成功 1 条｜失败 1 条｜队列中 0 条",
                "X：发送 1 条｜成功 1 条｜失败 0 条｜队列中 0 条",
                "Reddit：发送 0 条｜成功 0 条｜失败 0 条｜队列中 0 条",
                "Instagram：发送 0 条｜成功 0 条｜失败 0 条｜队列中 0 条",
                "Facebook：发送 0 条｜成功 0 条｜失败 0 条｜队列中 0 条",
                "Discord：发送 0 条｜成功 0 条｜失败 0 条｜队列中 0 条",
                "TikTok：发送 0 条｜成功 0 条｜失败 0 条｜私信互动 未接入｜队列中 0 条",
            ],
        )
        self.assertNotIn("通知编号", text)
        for i in range(12):
            self.now += 30
            self.tick()
        self.assertEqual(self.relay.send.call_count, 1)

    def test_historical_report_refresh_cannot_fan_out_notifications(self):
        self.tick()
        self.tick("2026-10-25T15:00:01")
        with self.business.connect(write=True) as db:
            for i in range(17):
                db.execute(
                    "INSERT INTO statistical_reports VALUES('interval',?,?,?,?,1,'hash',?)",
                    (
                        self.now - (i + 1) * 10800,
                        self.now - (i + 2) * 10800,
                        self.now,
                        self.now,
                        "not even a valid report JSON",
                    ),
                )
        self.tick("2026-10-25T17:34:51")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_zero_activity_still_produces_one_summary(self):
        self.tick()
        self.tick("2026-10-25T15:00:00")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(
            self.relay.send.call_args.args[0].count("发送 0 条｜成功 0 条｜失败 0 条"), 7
        )

    def test_restart_and_same_window_changes_do_not_resend(self):
        self.tick()
        self.tick("2026-10-25T15:00:01")
        self.service = self.new_service()
        self.item()
        self.tick("2026-10-25T15:03:00")
        self.assertEqual(self.relay.send.call_count, 1)

    def test_reconnect_recovers_only_latest_window_instead_of_batch_catch_up(self):
        self.tick()
        self.tick("2026-10-25T20:10:00")
        self.assertEqual(self.relay.send.call_count, 1)
        recovered = self.service.status()["last_recovered_summary"]
        self.assertEqual(recovered["cutoff"], stamp("2026-10-25T18:00:00"))
        self.assertEqual(recovered["reason"], "latest_window_recovered")
        self.assertEqual(recovered["late_seconds"], 7800)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(
            self.rows()[0]["event_key"], f"platform_summary:{int(stamp('2026-10-25T18:00:00'))}"
        )
        self.tick("2026-10-25T21:00:00")
        self.assertEqual(self.relay.send.call_count, 2)
        self.assertEqual(len(self.rows()), 2)

    def test_boundary_wake_cycle_and_running_service_share_one_submission(self):
        self.tick()
        self.now = stamp("2026-10-25T15:00:01")
        wake_cycle = self.new_service()
        with ThreadPoolExecutor(max_workers=2) as pool:
            collected = list(pool.map(lambda s: s.collect(), (self.service, wake_cycle)))
            sent = list(pool.map(lambda s: s.drain(), (self.service, wake_cycle)))
        self.assertEqual(collected, [True, True])
        self.assertEqual(sum(sent), 1)
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_20_hour_daily_and_midnight_final_do_not_add_extra_pushes(self):
        self.tick("2026-10-25T18:10:00")
        self.tick("2026-10-25T20:00:00")
        self.assertEqual(self.relay.send.call_count, 0)
        self.tick("2026-10-25T21:00:00")
        self.tick("2026-10-26T00:00:00")
        self.tick("2026-10-26T00:05:00")
        self.assertEqual(self.relay.send.call_count, 2)

    def test_pending_legacy_messages_expire_on_upgrade_and_are_blocked_at_claim(self):
        with self.service.store.connect(write=True) as db:
            self.service.store.enqueue(db, "old-task", "task", "old task")
        self.tick()
        self.assertEqual(self.rows()[0]["state"], "expired")
        with self.service.store.connect(write=True) as db:
            self.service.store.enqueue(db, "old-report", "report", "old report")
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 0)
        self.assertTrue(all(r["state"] == "expired" for r in self.rows()))

    def test_unknown_request_is_not_recreated_on_restart(self):
        self.tick()
        self.relay.send.return_value = {"state": "unknown", "reason": "relay_outcome_unknown"}
        self.tick("2026-10-25T15:00:00")
        self.service = self.new_service()
        self.tick("2026-10-25T15:02:00")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "unknown")

    def test_pending_summary_can_send_after_five_minutes(self):
        self.tick()
        self.now = stamp("2026-10-25T15:00:00")
        self.assertTrue(self.service.collect())
        self.now = stamp("2026-10-25T15:05:00")
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(self.rows()[0]["state"], "accepted")

    def test_old_pending_summary_expires_when_next_boundary_arrives(self):
        self.tick()
        self.now = stamp("2026-10-25T15:00:00")
        self.assertTrue(self.service.collect())
        self.now = stamp("2026-10-25T18:00:00")
        self.assertTrue(self.service.collect())
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual([r["state"] for r in self.rows()], ["expired", "accepted"])

    def test_standby_over_nine_recovers_one_on_resume_without_duplicate(self):
        self.tick("2026-10-27T08:58:59")
        self.tick("2026-10-27T09:36:54")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(
            self.rows()[0]["event_key"], f"platform_summary:{int(stamp('2026-10-27T09:00:00'))}"
        )
        self.service = self.new_service()
        self.tick("2026-10-27T09:37:00")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(self.service.status()["next_summary_at"], stamp("2026-10-27T12:00:00"))

    def test_unknown_scheduled_simulated_unverified_and_non_delivery_items_are_excluded(self):
        self.tick()
        self.item(status="unknown")
        self.item(status="scheduled")
        self.item(mode="simulation")
        self.item(verified=False)
        self.item(action="like")
        self.item(reference="simulation://receipt")
        self.item(status="failed_known", recorded=False)
        self.tick("2026-10-25T15:00:00")
        self.assertEqual(
            self.relay.send.call_args.args[0].count("发送 0 条｜成功 0 条｜失败 0 条"), 7
        )

    def test_late_verified_result_enters_next_window_without_revising_previous(self):
        self.tick()
        key = self.item(status="unknown")
        self.tick("2026-10-25T15:00:00")
        self.now = stamp("2026-10-25T16:00:00")
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',result=? WHERE id=?",
                (canonical({"verified": True}), key),
            )
            db.execute(
                "UPDATE attempts SET finished_at=?,outcome='confirmed' WHERE item_id=?",
                (self.now, key),
            )
            db.execute(
                "INSERT INTO publication_evidence VALUES(?,?,?,'platform')",
                (key, self.now - 7000, self.now),
            )
        self.tick()
        self.assertEqual(self.relay.send.call_count, 1)
        self.tick("2026-10-25T18:00:00")
        self.assertIn(
            "Telegram：发送 1 条｜成功 1 条｜失败 0 条", self.relay.send.call_args.args[0]
        )
        self.assertEqual(self.relay.send.call_count, 2)

    def test_confirmation_window_is_half_open_and_next_time_survives_clock_reversal(self):
        self.tick()
        self.item(when="2026-10-25T12:00:00")
        self.item(when="2026-10-25T15:00:00")
        self.tick("2026-10-25T15:00:01")
        self.assertIn(
            "Telegram：发送 1 条｜成功 1 条｜失败 0 条", self.relay.send.call_args.args[0]
        )
        self.tick("2026-10-25T14:00:00")
        self.assertEqual(self.service.status()["next_summary_at"], stamp("2026-10-25T18:00:00"))
        self.assertEqual(self.relay.send.call_count, 1)

    def test_real_service_defaults_to_fixed_three_hour_policy(self):
        self.tick()
        self.assertEqual(self.service.status()["delivery_policy"], SUMMARY_POLICY)
        self.assertEqual(summary_cutoff(stamp("2026-10-25T20:00:00")), stamp("2026-10-25T18:00:00"))

    def test_tiktok_public_comments_do_not_imply_private_message_coverage(self):
        self.tick()
        self.item(platform="tiktok", action="comment")
        self.item(platform="tiktok", action="reply")
        self.tick("2026-10-25T15:00:00")
        self.assertIn(
            "TikTok：发送 2 条｜成功 2 条｜失败 0 条｜私信互动 未接入",
            self.relay.send.call_args.args[0],
        )

    def test_tiktok_dm_count_is_confirmed_outbound_messages_and_excludes_other_results(self):
        self.tick()
        self.item(platform="tiktok", action="comment")
        self.item(platform="tiktok", action="reply")
        self.item(platform="tiktok", action="direct_message")
        self.item(platform="tiktok", action="reply", interaction_channel="direct_message")
        self.item(platform="tiktok", action="send_direct_message", status="failed_known")
        self.item(platform="tiktok", action="direct_message", status="unknown")
        self.item(platform="tiktok", action="direct_message", mode="simulation")
        self.item(platform="tiktok", action="direct_message", verified=False)
        self.item(platform="tiktok", action="direct_message", reference="simulation://receipt")
        self.item(platform="x", action="direct_message")
        self.tick("2026-10-25T15:00:00")
        text = self.relay.send.call_args.args[0]
        self.assertIn("TikTok：发送 5 条｜成功 4 条｜失败 1 条｜私信互动 2 条", text)
        self.assertEqual(text.count("私信互动"), 1)
        self.assertIn("X：发送 0 条｜成功 0 条｜失败 0 条", text)
        self.assertNotIn("private outbound text", text)

    def test_historical_dm_evidence_allows_a_real_zero_for_this_window(self):
        self.tick()
        self.item(platform="tiktok", action="direct_message", when="2026-10-25T11:10:00")
        self.tick("2026-10-25T15:00:00")
        self.assertIn(
            "TikTok：发送 0 条｜成功 0 条｜失败 0 条｜私信互动 0 条",
            self.relay.send.call_args.args[0],
        )

    def test_late_tiktok_private_message_receipt_does_not_rebroadcast_old_window(self):
        self.tick()
        key = self.item(platform="tiktok", action="reply_direct_message", status="unknown")
        self.tick("2026-10-25T15:00:00")
        self.assertIn("私信互动 未接入", self.relay.send.call_args.args[0])
        self.now = stamp("2026-10-25T16:00:00")
        with self.business.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',result=? WHERE id=?",
                (canonical({"verified": True}), key),
            )
            db.execute(
                "UPDATE attempts SET finished_at=?,outcome='confirmed' WHERE item_id=?",
                (self.now, key),
            )
            db.execute(
                "INSERT INTO publication_evidence VALUES(?,?,?,'platform')",
                (key, self.now - 7000, self.now),
            )
        self.service = self.new_service()
        self.tick()
        self.assertEqual(self.relay.send.call_count, 1)
        self.tick("2026-10-25T18:00:00")
        self.assertIn("私信互动 1 条", self.relay.send.call_args.args[0])
        self.assertEqual(self.relay.send.call_count, 2)

    def test_private_message_formatter_never_turns_missing_or_invalid_data_into_zero(self):
        with self.business.connect() as db:
            summary = collect_summary(db, stamp("2026-10-25T15:00:00"))
        tiktok = next(p for p in summary["platforms"] if p["platform"] == "tiktok")
        self.assertIsNone(tiktok["direct_message_interactions"])
        tiktok.pop("direct_message_interactions")
        self.assertIn("私信互动 未接入", summary_notice(summary))
        for invalid in (-1, True, "3"):
            tiktok["direct_message_interactions"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                summary_notice(summary)

    def queue_item(
        self,
        *,
        platform="x",
        task_state="active",
        job_state="pending",
        item_status="not_attempted",
        mode="live",
        expires=None,
        available=None,
        kind="send",
        action="reply",
        recorded=False,
    ):
        key = self.item(
            platform=platform, status=item_status, mode=mode, action=action, recorded=recorded
        )
        with self.business.connect(write=True) as db:
            db.execute("UPDATE tasks SET state=? WHERE id=?", (task_state, key))
            db.execute(
                "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'{}','hash',?,?,?,?,?)",
                (
                    kind + ":" + key,
                    kind,
                    key,
                    key,
                    key,
                    platform + ".test",
                    self.now if available is None else available,
                    self.now + 21600 if expires is None else expires,
                    job_state,
                    self.now,
                    self.now,
                ),
            )
        return key

    def test_queue_count_uses_real_unattempted_active_delivery_jobs(self):
        self.queue_item(platform="x", available=self.now + 3600)
        self.queue_item(platform="x", job_state="leased")
        self.queue_item(platform="instagram")
        for kwargs in (
            {"task_state": "draft"},
            {"task_state": "paused"},
            {"task_state": "cancelled"},
            {"task_state": "completed"},
            {"job_state": "unknown"},
            {"job_state": "confirmed"},
            {"item_status": "executing"},
            {"item_status": "unknown"},
            {"item_status": "scheduled"},
            {"item_status": "manual_attention"},
            {"item_status": "confirmed"},
            {"item_status": "failed_known"},
            {"mode": "simulation"},
            {"expires": self.now},
            {"kind": "join"},
            {"action": "like"},
            {"recorded": True},
        ):
            self.queue_item(**kwargs)
        with self.business.connect() as db:
            summary = collect_summary(db, summary_cutoff(self.now), queue_at=self.now)
        counts = {p["platform"]: p["queued"] for p in summary["platforms"]}
        self.assertEqual(counts["x"], 2)
        self.assertEqual(counts["instagram"], 1)
        self.assertEqual(sum(counts.values()), 3)
        self.assertNotIn("private outbound text", str(summary))

    def test_queue_is_current_snapshot_instead_of_period_cutoff(self):
        self.now = stamp("2026-10-25T15:20:00")
        cutoff = summary_cutoff(self.now)
        self.queue_item(expires=cutoff + 600)
        sample = self.service.sources.collect()
        self.assertEqual(sample["summary"]["queue_observed_at"], self.now)
        self.assertEqual(
            next(p["queued"] for p in sample["summary"]["platforms"] if p["platform"] == "x"), 0
        )
        with self.business.connect() as db:
            historical = collect_summary(db, cutoff)
        self.assertEqual(
            next(p["queued"] for p in historical["platforms"] if p["platform"] == "x"), 1
        )

    def test_queue_changes_do_not_trigger_additional_broadcasts(self):
        self.tick()
        self.queue_item()
        self.tick("2026-10-25T15:00:01")
        self.assertIn(
            "X：发送 0 条｜成功 0 条｜失败 0 条｜队列中 1 条", self.relay.send.call_args.args[0]
        )
        self.queue_item()
        self.tick("2026-10-25T15:10:00")
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(
            next(
                p["queued"]
                for p in self.service.sources.collect()["summary"]["platforms"]
                if p["platform"] == "x"
            ),
            2,
        )

    def test_tiktok_dm_queue_counts_outbound_messages_without_double_counting(self):
        key = self.queue_item(platform="tiktok", action="send_direct_message")
        self.queue_item(platform="x", action="send_direct_message")
        self.queue_item(platform="tiktok", action="like")
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,created_at,updated_at) VALUES(?,'join',?,?,?,'tiktok.test','{}','hash',?,?,?,?)",
                ("join:" + key, key, key, key, self.now, self.now + 21600, self.now, self.now),
            )
        sample = self.service.sources.collect()["summary"]
        self.assertEqual(
            next(p["queued"] for p in sample["platforms"] if p["platform"] == "tiktok"), 1
        )
        self.assertEqual(next(p["queued"] for p in sample["platforms"] if p["platform"] == "x"), 0)

    def test_missing_queue_evidence_is_not_formatted_as_zero(self):
        summary = self.service.sources.collect()["summary"]
        x = next(p for p in summary["platforms"] if p["platform"] == "x")
        x.pop("queued")
        self.assertIn("X：发送 0 条｜成功 0 条｜失败 0 条｜队列中 未统计", summary_notice(summary))
        for invalid in (-1, True, "3"):
            x["queued"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                summary_notice(summary)


if __name__ == "__main__":
    unittest.main()
