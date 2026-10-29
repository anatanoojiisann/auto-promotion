import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, CandidateInput, ItemInput, TaskInput
from auto_promotion.store import Store


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class ReadinessTests(unittest.TestCase):
    def test_explicit_ten_minute_wait_uses_actual_late_membership(self):
        task = self.task(
            max_joins=1,
            outbound_after_membership_seconds=600,
            ends_at=iso(self.now + 86400),
        )
        candidate = self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 600))
        self.assertEqual(self.engine.start_task(task["id"])["phase"], "starting")
        self.now += 7200
        joined_at = self.now
        self.membership(candidate, outbound_eligible_at_utc=iso(joined_at + 3600))
        task = self.engine.start_task(task["id"], source="scheduler")
        self.assertEqual(task["phase"], "ready")
        self.assertEqual(task["items"][0]["due_at"], joined_at + 600)
        self.now = joined_at + 599
        self.assertEqual(self.engine.run_once(task["id"])["processed"], [])
        self.now = joined_at + 600
        self.assertEqual(self.engine.run_once(task["id"])["processed"][0]["status"], "confirmed")

    def test_twenty_minute_wait_still_requires_verified_send_permission(self):
        task = self.task(max_joins=1, outbound_after_membership_seconds=1200)
        candidate = self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 1200))
        self.membership(candidate, can_send=False, outbound_eligible_at_utc=iso(self.now + 3600))
        self.assertEqual(self.engine.start_task(task["id"])["phase"], "starting")
        self.membership(candidate, outbound_eligible_at_utc=iso(self.now + 3600))
        task = self.engine.start_task(task["id"], source="scheduler")
        self.assertEqual(task["phase"], "ready")
        self.assertEqual(task["items"][0]["due_at"], self.now + 1200)

    def test_readonly_lookup_can_confirm_existing_member_without_join_attempt(self):
        task = self.task(max_joins=1, outbound_after_membership_seconds=600)
        candidate = self.candidate(task)
        result = dict(
            status="joined",
            verified=True,
            can_send=True,
            external_reference="simulation://membership/existing",
            outbound_eligible_at_utc=iso(self.now + 3600),
        )
        with patch.object(self.engine, "call", return_value=result) as call:
            observed = self.engine.reconcile_join(candidate["id"])
        self.assertEqual(call.call_args.args[1], "verify_join")
        self.assertEqual(observed["status"], "already_joined")
        self.assertEqual(self.engine.task(task["id"])["candidates"][0]["state"], "already_joined")
        self.assertFalse(self.store.rows("SELECT * FROM join_attempts"))

    def test_readonly_nonmember_lookup_keeps_the_first_join_available(self):
        task = self.task(max_joins=1, outbound_after_membership_seconds=600)
        candidate = self.candidate(task)
        with patch.object(self.engine, "call", return_value=dict(status="unknown", verified=False)):
            self.engine.reconcile_join(candidate["id"])
        self.assertEqual(self.engine.task(task["id"])["candidates"][0]["state"], "not_attempted")
        self.assertFalse(self.store.rows("SELECT * FROM join_attempts"))

    def test_recovery_schedule_waits_for_cloud_and_keeps_hour_and_spacing(self):
        task = self.task(
            max_joins=1,
            outbound_after_membership_seconds=3600,
            outbound_after_service_recovery_seconds=630,
            ends_at=iso(self.now + 86400),
        )
        candidate = self.candidate(task)
        self.membership(candidate, outbound_eligible_at_utc=iso(self.now + 3600))
        self.items(task, scheduled_at=iso(self.now + 3600))
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_control_required','1')")
            db.execute(
                "INSERT INTO meta VALUES('cloud_control_health',?)",
                (json.dumps({"state": "CloudWriteQuotaExceeded"}),),
            )
        task = self.engine.start_task(task["id"])
        self.assertEqual(task["phase"], "starting")
        self.assertIsNone(task["manifest_hash"])
        self.now += 7200
        self.assertEqual(
            self.engine.start_task(task["id"])["readiness"]["workflow"]["status"],
            "waiting_execution_service",
        )
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='cloud_control_health'",
                (json.dumps({"state": "connected"}),),
            )
        task = self.engine.start_task(task["id"], source="scheduler")
        self.assertEqual(task["phase"], "ready")
        self.assertEqual(task["items"][0]["due_at"], self.now + 630)
        self.assertGreater(task["items"][0]["due_at"], 1800003600)

    def test_join_and_one_interaction_have_budget_for_both_actions(self):
        task = self.task(
            max_joins=1,
            outbound_after_membership_seconds=3600,
            daily_limit=2,
            target_daily_limit=2,
            user_daily_limit=2,
        )
        self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 3600))
        self.engine.start_task(task["id"])
        self.assertEqual(self.engine.run_joins(task["id"])["processed"][0]["status"], "joined")
        self.engine.start_task(task["id"], source="scheduler")
        self.now += 3600
        sent = self.engine.run_once(task["id"])["processed"]
        self.assertEqual(sent[0]["status"], "confirmed")
        self.assertEqual(len(self.store.rows("SELECT * FROM join_attempts")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)

    def test_relative_hour_resolves_from_actual_late_join_before_freezing(self):
        task = self.task(
            max_joins=1, outbound_after_membership_seconds=3600, ends_at=iso(self.now + 86400)
        )
        candidate = self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 3600))
        self.assertEqual(self.engine.start_task(task["id"])["phase"], "starting")
        self.now += 7200
        eligible = self.now + 3600
        self.membership(candidate, outbound_eligible_at_utc=iso(eligible))
        task = self.engine.start_task(task["id"], source="scheduler")
        self.assertEqual(task["phase"], "ready")
        self.assertEqual(task["items"][0]["due_at"], eligible)
        self.assertEqual(task["items"][0]["payload"]["text"], "AI question")
        self.now = eligible - 1
        self.assertEqual(self.engine.run_once(task["id"])["processed"], [])
        self.now = eligible
        self.assertEqual(self.engine.run_once(task["id"])["processed"][0]["status"], "confirmed")

    def test_relative_hour_requires_confirmed_membership_not_admin_request(self):
        task = self.task(max_joins=1, outbound_after_membership_seconds=3600)
        candidate = self.candidate(task)
        self.membership(candidate, "pending_admin_approval", verified=False)
        self.items(task, scheduled_at=iso(self.now + 3600))
        task = self.engine.start_task(task["id"])
        self.assertEqual(task["phase"], "starting")
        self.assertIsNone(task["manifest_hash"])
        self.assertEqual(task["items"][0]["due_at"], self.now + 3600)

    def test_relative_hour_does_not_modify_a_frozen_manifest(self):
        task = self.task(max_joins=1, outbound_after_membership_seconds=3600)
        candidate = self.candidate(task)
        self.membership(candidate, outbound_eligible_at_utc=iso(self.now + 3600))
        self.items(task, scheduled_at=iso(self.now + 3600))
        original = self.engine.start_task(task["id"])
        self.now += 3600
        restarted = self.engine.start_task(task["id"])
        self.assertEqual(original["manifest_hash"], restarted["manifest_hash"])
        self.assertEqual(original["items"][0]["payload"], restarted["items"][0]["payload"])

    def test_relative_membership_dependents_continue_per_group(self):
        parent = self.task(max_joins=1)
        candidate = self.candidate(parent)
        child = self.task(outbound_after_membership_seconds=3600)
        self.items(child, scheduled_at=iso(self.now + 3600))
        self.assertEqual(self.engine.start_task(child["id"])["phase"], "starting")
        other = self.task(max_joins=1, outbound_after_membership_seconds=3600)
        self.items(other, target="@other_ai_group", scheduled_at=iso(self.now + 3600))
        self.assertEqual(self.engine.start_task(other["id"])["phase"], "starting")
        self.membership(candidate, outbound_eligible_at_utc=iso(self.now + 3600))
        from auto_promotion.queue_store import JobQueue

        JobQueue(self.engine).sync(child["id"])
        self.assertEqual(self.engine.task(child["id"])["phase"], "ready")
        self.assertEqual(self.engine.task(other["id"])["phase"], "starting")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.provider = SimulationAdapter()
        self.registry = AdapterRegistry(Path(self.tmp.name), overrides={"telegram": self.provider})
        self.engine = Engine(self.store, self.registry, clock=lambda: self.now)
        self.account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="test",
                backend_label="test",
                mode="simulation",
                min_interval=0,
                daily_limit=20,
                hourly_limit=20,
            )
        )
        self.engine.check_account(self.account["id"])
        self.engine.set_stop(False)

    def task(self, **policy):
        return self.engine.create_task(
            TaskInput(
                name="readiness",
                account_ids=[self.account["id"]],
                policy={
                    "starts_at": iso(self.now - 10),
                    "ends_at": iso(self.now + 7200),
                    "min_interval": 0,
                    "daily_limit": 20,
                    **policy,
                },
            )
        )

    def candidate(self, task, **fields):
        self.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=self.account["id"],
                target="@ai_group",
                language="en",
                confidence=0.95,
                text="AI video",
                **fields,
            ),
        )
        return self.engine.task(task["id"])["candidates"][0]

    def discovery(self, task):
        return self.engine.start_task(task["id"], sending=False)

    def items(self, task, **fields):
        return self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    **{
                        "account_id": self.account["id"],
                        "target": "@ai_group",
                        "text": "AI question",
                        "action": "reply",
                        "reply_to": 123,
                        "scheduled_at": iso(self.now),
                        **fields,
                    }
                )
            ],
        )

    def ready(self):
        task = self.discovery(self.task())
        self.items(task)
        task = self.engine.prepare(task["id"])
        return self.engine.start_task(task["id"])

    def membership(self, candidate, state="joined", **changes):
        result = {
            "verified": True,
            "external_reference": "simulation://membership/test",
            "can_send": True,
            "outbound_eligible_at_utc": iso(self.now),
            **changes,
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE candidates SET state=?,result=? WHERE id=?",
                (state, json.dumps(result), candidate["id"]),
            )

    def test_non_adult_annotation_does_not_hide_other_risk_signals(self):
        cases = [
            ({"text": "Current non-adult AI video question"}, "qualified"),
            ({"text": "NON-ADULT AI video question"}, "qualified"),
            ({"text": "non-adult AI video, adult content elsewhere"}, "rejected"),
            ({"text": "non-adult AI video, porn examples"}, "rejected"),
            ({"text": "non-adult AI video, nsfw examples"}, "rejected"),
            ({"text": "non-adult AI video", "title": "adult channel"}, "rejected"),
            ({"text": "non-adult AI video", "target": "@adult_group"}, "rejected"),
            ({"text": "non-adult AI video", "title": "non-adult"}, "rejected"),
            ({"text": "adult AI video"}, "rejected"),
            ({"text": "not adult AI video"}, "rejected"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                task = self.task(queries=["AI video"], blocked_keywords=["adult", "nsfw", "porn"])
                self.discovery(task)
                payload = {
                    "account_id": self.account["id"],
                    "target": "@ai_group",
                    "language": "en",
                    "confidence": 0.95,
                    **fields,
                }
                self.engine.candidate(task["id"], CandidateInput(**payload))
                candidate = self.engine.task(task["id"])["candidates"][0]
                self.assertEqual(candidate["decision"], expected)
                self.assertEqual(candidate["payload"]["text"], fields["text"])

    def test_rejected_candidate_cannot_enter_frozen_manifest(self):
        task = self.discovery(self.task(blocked_keywords=["adult"]))
        self.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=self.account["id"],
                target="@ai_group",
                text="adult AI",
                language="en",
                confidence=0.95,
            ),
        )
        self.items(task, target="@AI_GROUP")
        with self.assertRaisesRegex(Conflict, "筛选"):
            self.engine.prepare(task["id"])
        self.assertIsNone(self.engine.task(task["id"])["manifest_hash"])

    def test_new_group_requires_current_candidate_and_verified_join(self):
        task = self.discovery(self.task(max_joins=1))
        self.items(task, scheduled_at=iso(self.now + 3600))
        with self.assertRaisesRegex(Conflict, "候选资格"):
            self.engine.prepare(task["id"])
        self.candidate(task)
        with self.assertRaisesRegex(Conflict, "入群回执"):
            self.engine.prepare(task["id"])
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_case_variant_cannot_hide_rejected_candidate(self):
        task = self.discovery(self.task(blocked_keywords=["adult"]))
        self.candidate(task)
        self.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=self.account["id"],
                target="@AI_GROUP",
                text="adult AI",
                language="en",
                confidence=0.95,
            ),
        )
        self.items(task)
        with self.assertRaisesRegex(Conflict, "筛选"):
            self.engine.prepare(task["id"])

    def test_native_schedule_waits_for_join_eligibility_before_submission(self):
        task = self.discovery(self.task(max_joins=1))
        self.candidate(task)
        self.engine.run_joins(task["id"])
        self.items(
            task, action="schedule_message", reply_to=None, scheduled_at=iso(self.now + 4000)
        )
        task = self.engine.prepare(task["id"])
        task = self.engine.start_task(task["id"])
        with patch.object(self.provider, "call", wraps=self.provider.call) as calls:
            self.engine.run_once(task["id"])
        calls.assert_not_called()
        item = self.engine.task(task["id"])["items"][0]
        self.assertEqual(item["result"]["skip_reason"], "join_wait_pending")
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))
        self.now += 3600
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "scheduled")

    def test_join_wait_is_checked_at_planned_time_and_execution_time(self):
        task = self.discovery(self.task(max_joins=1))
        self.candidate(task)
        self.engine.run_joins(task["id"])
        self.items(task, scheduled_at=iso(self.now + 3599))
        with self.assertRaisesRegex(Conflict, "等待"):
            self.engine.prepare(task["id"])
        self.items(task, scheduled_at=iso(self.now + 3600))
        task = self.engine.prepare(task["id"])
        task = self.engine.start_task(task["id"])
        frozen_hash = task["manifest_hash"]
        self.engine.run_once(task["id"])
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))
        self.now += 3600
        self.engine.run_once(task["id"])
        task = self.engine.task(task["id"])
        self.assertEqual(task["items"][0]["status"], "confirmed")
        self.assertEqual(task["manifest_hash"], frozen_hash)
        self.assertNotIn("skip_reason", task["items"][0]["result"])
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)

    def test_unverified_or_unsendable_membership_cannot_freeze(self):
        cases = [
            ("pending_admin_approval", {}),
            ("unknown", {}),
            ("executing", {}),
            ("joined", {"verified": False}),
            ("joined", {"external_reference": ""}),
            ("joined", {"can_send": False}),
            ("joined", {"can_send": None}),
            ("joined", {"outbound_eligible_at_utc": None}),
        ]
        for state, changes in cases:
            with self.subTest(state=state, changes=changes):
                task = self.discovery(self.task(max_joins=1))
                candidate = self.candidate(task)
                self.membership(candidate, state, **changes)
                self.items(task, scheduled_at=iso(self.now + 3600))
                with self.assertRaises(Conflict):
                    self.engine.prepare(task["id"])
                self.assertIsNone(self.engine.task(task["id"])["manifest_hash"])

    def test_existing_member_permissions_can_be_rechecked_without_join_or_new_wait(self):
        task = self.discovery(self.task(max_joins=1))
        candidate = self.candidate(task)
        self.membership(candidate, can_send=False)
        self.items(task)
        fresh = {
            "status": "joined",
            "verified": True,
            "can_send": True,
            "external_reference": "simulation://membership/test",
            "outbound_eligible_at_utc": iso(self.now + 3600),
        }
        with patch.object(self.engine, "call", return_value=fresh) as call:
            result = self.engine.reconcile_join(candidate["id"])
        self.assertEqual(call.call_args.args[1], "verify_join")
        self.assertTrue(result["can_send"])
        self.assertEqual(result["outbound_eligible_at_utc"], iso(self.now))
        self.assertIsNotNone(self.engine.prepare(task["id"])["manifest_hash"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM join_attempts")["n"], 0)

    def test_lost_membership_invalidates_old_permission_evidence_without_rejoin(self):
        task = self.discovery(self.task(max_joins=1))
        candidate = self.candidate(task)
        self.membership(candidate)
        with patch.object(
            self.engine, "call", return_value={"verified": False, "reason": "ChannelPrivateError"}
        ):
            result = self.engine.reconcile_join(candidate["id"])
        self.assertEqual(result["status"], "unknown")
        row = self.store.one("SELECT state,result FROM candidates WHERE id=?", (candidate["id"],))
        self.assertEqual(row["state"], "unknown")
        self.assertFalse(json.loads(row["result"])["can_send"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM join_attempts")["n"], 0)

    def test_membership_is_rechecked_before_approval_and_submission(self):
        task = self.discovery(self.task(max_joins=1))
        candidate = self.candidate(task)
        self.membership(candidate)
        self.items(task)
        task = self.engine.prepare(task["id"])
        self.membership(candidate, "unknown")
        waiting = self.engine.start_task(task["id"])
        self.assertEqual(waiting["phase"], "starting")
        self.assertEqual(len(self.engine.task(task["id"])["approvals"]), 0)
        self.membership(candidate)
        task = self.engine.start_task(task["id"])
        self.membership(candidate, "unknown")
        with patch.object(self.provider, "call", wraps=self.provider.call) as calls:
            self.engine.run_once(task["id"])
        calls.assert_not_called()
        item = self.engine.task(task["id"])["items"][0]
        self.assertEqual(item["result"]["skip_reason"], "membership_unverified")
        self.assertEqual(item["status"], "not_attempted")

    def test_expired_task_cannot_receive_new_discovery_approval(self):
        task = self.task(ends_at=iso(self.now + 10))
        self.now += 10
        with self.assertRaisesRegex(Conflict, "窗口已过期"):
            self.discovery(task)
        self.assertFalse(self.engine.task(task["id"])["approvals"])

    def test_expired_manifest_cannot_receive_new_outbound_approval(self):
        task = self.discovery(self.task(ends_at=iso(self.now + 10)))
        self.items(task, scheduled_at=iso(self.now + 5))
        task = self.engine.prepare(task["id"])
        self.now += 10
        with self.assertRaisesRegex(Conflict, "窗口已过期"):
            self.engine.start_task(task["id"])
        self.assertEqual(len(self.engine.task(task["id"])["approvals"]), 0)

    def test_elapsed_delivery_time_is_rejected_at_freeze_and_approval(self):
        task = self.discovery(self.task())
        self.items(task)
        self.now += 901
        with self.assertRaisesRegex(Conflict, "计划时间"):
            self.engine.prepare(task["id"])
        self.items(task, scheduled_at=iso(self.now + 1))
        task = self.engine.prepare(task["id"])
        self.now += 902
        with self.assertRaisesRegex(Conflict, "计划时间"):
            self.engine.start_task(task["id"])

    def test_expired_paused_task_cannot_be_restarted(self):
        task = self.ready()
        self.engine.change_state(task["id"], "paused")
        self.now += 7200
        with self.assertRaisesRegex(Conflict, "窗口已过期"):
            self.engine.start_task(task["id"])
        self.assertEqual(self.engine.task(task["id"])["state"], "paused")

    def test_immediate_delivery_keeps_existing_grace_and_native_schedule_must_be_future(self):
        task = self.discovery(self.task())
        self.items(task, scheduled_at=iso(self.now - 5))
        self.engine.prepare(task["id"])
        self.engine.start_task(task["id"])
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")
        task = self.discovery(self.task())
        self.items(task, action="schedule_message", reply_to=None)
        with self.assertRaisesRegex(Conflict, "计划时间"):
            self.engine.prepare(task["id"])

    def test_skips_are_durable_deduplicated_and_do_not_change_payloads(self):
        task = self.discovery(self.task())
        self.items(task)
        task = self.engine.prepare(task["id"])
        with patch.object(self.provider, "call", wraps=self.provider.call) as calls:
            self.engine.run_cycle()
            self.now += 1
            self.engine.run_cycle()
        calls.assert_not_called()
        current = self.engine.task(task["id"])
        self.assertEqual(current["items"][0]["result"]["skip_reason"], "task_not_started")
        self.assertEqual(current["manifest_hash"], task["manifest_hash"])
        self.assertEqual(current["items"][0]["payload_hash"], task["items"][0]["payload_hash"])
        self.assertEqual(len(self.store.rows("SELECT * FROM audit WHERE kind='item.skipped'")), 1)
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_global_stop_records_reason_without_any_platform_call(self):
        task = self.ready()
        self.engine.set_stop(True)
        with patch.object(self.provider, "call", wraps=self.provider.call) as calls:
            self.engine.run_cycle()
        calls.assert_not_called()
        current = self.engine.task(task["id"])
        self.assertEqual(current["items"][0]["result"]["skip_reason"], "global_stop")
        self.assertIn("global_stop", [b["reason"] for b in current["readiness"]["blockers"]])
        self.engine.set_stop(False)
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")

    def test_unapproved_batch_does_not_contend_for_busy_account(self):
        task = self.discovery(self.task())
        self.items(task, scheduled_at=iso(self.now + 4000))
        with (
            patch.object(self.engine.lanes, "acquire") as acquire,
            patch.object(self.provider, "call", wraps=self.provider.call) as calls,
        ):
            for _ in range(3):
                self.engine.run_cycle()
        acquire.assert_not_called()
        calls.assert_not_called()
        audits = self.store.rows("SELECT details FROM audit WHERE kind='item.skipped'")
        self.assertEqual([json.loads(a["details"])["reason"] for a in audits], ["task_not_started"])
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_approved_future_delivery_never_acquires_account_lane(self):
        task = self.discovery(self.task())
        self.items(task, scheduled_at=iso(self.now + 100))
        task = self.engine.prepare(task["id"])
        task = self.engine.start_task(task["id"])
        with patch.object(self.engine.lanes, "acquire") as acquire:
            self.engine.run_once(task["id"])
        acquire.assert_not_called()
        self.assertEqual(
            self.engine.task(task["id"])["readiness"]["workflow"]["status"], "waiting_schedule"
        )
        self.now += 100
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)

    def test_future_join_window_does_not_occupy_account_lane(self):
        task = self.discovery(self.task(max_joins=1, starts_at=iso(self.now + 100)))
        self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 4000))
        self.engine.start_task(task["id"])
        with patch.object(self.engine.lanes, "acquire") as acquire:
            self.engine.run_cycle()
        acquire.assert_not_called()
        self.assertEqual(
            self.engine.task(task["id"])["readiness"]["workflow"]["status"], "waiting_join_window"
        )
        self.now += 100
        self.engine.run_cycle()
        self.assertEqual(self.engine.task(task["id"])["candidates"][0]["state"], "joined")
        self.assertEqual(len(self.store.rows("SELECT * FROM join_attempts")), 1)
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_explicit_start_waits_for_membership_then_continues_automatically(self):
        task = self.task(max_joins=1)
        self.candidate(task)
        self.items(task, scheduled_at=iso(self.now + 4000))
        task = self.engine.start_task(task["id"], source="codex")
        self.assertEqual(task["phase"], "starting")
        self.assertEqual(task["readiness"]["workflow"]["status"], "waiting_membership")
        self.engine.run_cycle()
        task = self.engine.task(task["id"])
        self.assertEqual(task["phase"], "ready")
        self.assertEqual(task["approvals"], [])
        self.assertEqual(task["readiness"]["workflow"]["status"], "waiting_schedule")
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))
        self.now += 4000
        self.engine.run_cycle()
        self.engine.run_cycle()
        task = self.engine.task(task["id"])
        self.assertEqual(task["readiness"]["workflow"]["counts"]["verified_sent"], 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM join_attempts")), 1)

    def test_unapproved_expired_item_is_marked_missed_without_a_lane(self):
        task = self.discovery(self.task())
        self.items(task)
        self.now += 901
        with patch.object(self.engine.lanes, "acquire") as acquire:
            self.engine.run_once(task["id"])
        acquire.assert_not_called()
        task = self.engine.task(task["id"])
        self.assertEqual(task["items"][0]["status"], "missed")
        self.assertEqual(task["readiness"]["workflow"]["status"], "expired")
        self.assertEqual(len(task["approvals"]), 0)

    def test_cooldown_is_filtered_without_acquiring_a_lane_or_sending(self):
        task = self.ready()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET cooldown_until=? WHERE id=?",
                (self.now + 30, self.account["id"]),
            )
        with patch.object(self.engine.lanes, "acquire") as acquire:
            self.engine.run_once(task["id"])
        acquire.assert_not_called()
        self.assertEqual(
            self.engine.task(task["id"])["items"][0]["result"]["skip_reason"], "account_cooldown"
        )
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))
        self.now += 30
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")

    def test_live_disabled_join_is_logged_without_identity_check_or_join(self):
        self.account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="live",
                backend_label="live",
                min_interval=120,
            )
        )
        task = self.discovery(self.task(max_joins=1, min_interval=120))
        candidate = self.candidate(task)
        with patch.object(self.provider, "call", wraps=self.provider.call) as calls:
            self.engine.run_joins(task["id"])
            self.engine.run_joins(task["id"])
        calls.assert_not_called()
        candidate = self.engine.task(task["id"])["candidates"][0]
        self.assertEqual(candidate["result"]["skip_reason"], "live_writes_disabled")
        self.assertEqual(
            len(self.store.rows("SELECT * FROM audit WHERE kind='candidate.skipped'")), 1
        )
        self.assertFalse(self.store.rows("SELECT * FROM join_attempts"))


if __name__ == "__main__":
    unittest.main()
