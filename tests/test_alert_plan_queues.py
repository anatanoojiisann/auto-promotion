import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from auto_promotion.alert_plan_queues import MANIFEST_FIELDS
from auto_promotion.alert_relay import AlertSettings
from auto_promotion.alert_service import AlertService
from auto_promotion.alert_summary import summary_notice
from auto_promotion.models import canonical
from auto_promotion.store import Store


class PlannedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.fromisoformat("2026-10-27T17:55:00+08:00").timestamp()
        self.business = Store(self.root / "business.db")
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES('acc-tiktok','tiktok','test','test','live',300,2,1800)"
            )
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.automation = self.root / "codex/automations/test-dm/automation.toml"
        self.automation.parent.mkdir(parents=True)
        self.automation.write_text(
            'id="test-dm"\nkind="heartbeat"\nstatus="ACTIVE"\n', encoding="utf-8"
        )
        self.path = self.root / "private/batch/reply-plan.json"
        self.path.parent.mkdir(parents=True)
        self.data = {
            "version": 1,
            "batch_id": "batch-dm10",
            "account_id": "acc-tiktok",
            "automation_id": "test-dm",
            "execution_authorized": True,
            "request_acceptance_authorized": True,
            "execution_state": "ready",
            "automation_status": "ACTIVE",
            "entries": [
                {
                    "rank": n,
                    "recipient_id": f"private-user-{n}",
                    "conversation_id": f"conversation-{n}",
                    "parent_message_id": f"message-{n}",
                    "text": f"PRIVATE_REPLY_BODY_{n}",
                    "task_name": f"private task {n}",
                    "status": "confirmed"
                    if n <= 2
                    else "pending_acceptance"
                    if n >= 7
                    else "pending",
                    "requires_acceptance": n >= 7,
                }
                for n in range(1, 11)
            ],
        }
        self.save()
        self.relay = Mock()
        self.relay.send.return_value = {
            "state": "accepted",
            "reason": "delivery_unverified",
            "http": 200,
        }
        self.service = AlertService(
            self.business.path,
            settings=AlertSettings(key="fake-key", enabled=True),
            relay=self.relay,
            clock=lambda: self.now,
        )
        self.service.register_plan_queue(self.path)

    def save(self, rehash=True):
        if rehash:
            manifest = [{k: e[k] for k in MANIFEST_FIELDS} for e in self.data["entries"]]
            self.data["manifest_sha256"] = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def summary(self):
        return self.service.sources.collect()["summary"]

    def tiktok(self):
        return next(p for p in self.summary()["platforms"] if p["platform"] == "tiktok")

    def native(
        self, rank=3, status="not_attempted", *, queued=False, task_state="active", attempted=False
    ):
        task_id, item_id = f"task-{rank}", f"item-{rank}"
        entry = self.data["entries"][rank - 1]
        payload = {
            "action": "reply_direct_message",
            "tiktok_conversation_id": entry["conversation_id"],
            "tiktok_message_id": entry["parent_message_id"],
        }
        with self.business.connect(write=True) as db:
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,state,phase,manifest_hash,created_at,updated_at) VALUES(?,?,'live',?,'[]','{}','hash',?,'ready','hash',?,?)",
                (task_id, task_id, canonical(["acc-tiktok"]), task_state, self.now, self.now),
            )
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,'acc-tiktok',0,?,'hash',?,?,'https://example.com/reference',?)",
                (
                    item_id,
                    task_id,
                    canonical(payload),
                    self.now,
                    status,
                    canonical({"verified": status == "confirmed"}),
                ),
            )
            if attempted:
                db.execute(
                    "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) VALUES(?,?,'acc-tiktok',?,'target','user','live',?,?,?)",
                    ("attempt-" + item_id, item_id, task_id, self.now, self.now, status),
                )
            if queued:
                db.execute(
                    "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,created_at,updated_at) VALUES(?,'send',?,?,'acc-tiktok','tiktok.test','{}','hash',?,?,?,?)",
                    (
                        "send:" + item_id,
                        item_id,
                        task_id,
                        self.now,
                        self.now + 3600,
                        self.now,
                        self.now,
                    ),
                )
        return task_id, item_id

    def test_registered_active_plan_adds_eight_pending_items_without_writing_private_plan(self):
        before = self.path.read_bytes()
        p = self.tiktok()
        self.assertEqual((p["queued"], p["queued_native"], p["queued_planned"]), (8, 0, 8))
        self.assertEqual(before, self.path.read_bytes())
        text = summary_notice(self.summary())
        self.assertIn("队列中 8 条", text)
        self.assertNotIn("PRIVATE_REPLY", text)
        self.assertNotIn("private-user", canonical(self.summary()))

    def test_native_queued_item_is_not_counted_again_in_plan(self):
        self.native(queued=True)
        p = self.tiktok()
        self.assertEqual((p["queued"], p["queued_native"], p["queued_planned"]), (8, 1, 7))

    def test_canonical_receipt_overrides_stale_pending_plan(self):
        self.native(status="confirmed", attempted=True)
        self.assertEqual(self.tiktok()["queued"], 7)

    def test_unknown_or_attempted_items_do_not_reenter_planned_queue(self):
        for rank, status in (
            (3, "unknown"),
            (4, "executing"),
            (5, "failed_known"),
            (6, "not_attempted"),
        ):
            self.native(rank=rank, status=status, attempted=True)
        self.assertEqual(self.tiktok()["queued"], 4)

    def test_paused_bound_task_is_not_counted(self):
        task_id, item_id = self.native(task_state="paused")
        self.data["entries"][2].update(task_id=task_id, item_id=item_id)
        self.save()
        self.assertEqual(self.tiktok()["queued"], 7)

    def test_pending_acceptance_requires_existing_acceptance_authorization(self):
        self.data["request_acceptance_authorized"] = False
        self.save()
        self.assertEqual(self.tiktok()["queued"], 4)

    def test_uncertain_acceptance_is_not_a_fresh_send_queue_item(self):
        self.data["entries"][6]["acceptance_intent"] = {"state": "attempting"}
        self.save()
        self.assertEqual(self.tiktok()["queued"], 7)

    def test_paused_automation_and_unapproved_plan_are_inactive(self):
        self.automation.write_text(
            'id="test-dm"\nkind="heartbeat"\nstatus="PAUSED"\n', encoding="utf-8"
        )
        self.assertEqual(self.tiktok()["queued"], 0)
        self.automation.write_text(
            'id="test-dm"\nkind="heartbeat"\nstatus="ACTIVE"\n', encoding="utf-8"
        )
        self.data["execution_authorized"] = False
        self.save()
        self.assertEqual(self.tiktok()["queued"], 0)

    def test_missing_registered_plan_is_unavailable_instead_of_false_zero(self):
        self.path.unlink()
        p = self.tiktok()
        self.assertIsNone(p["queued"])
        self.assertEqual(self.summary()["queue_sources"][0]["reason"], "planned_queue_unavailable")
        self.assertIn("队列中 未统计", summary_notice(self.summary()))

    def test_completed_plan_stays_zero_after_its_automation_is_removed(self):
        self.data["completed"] = True
        self.save()
        self.automation.unlink()
        self.assertEqual(self.tiktok()["queued"], 0)

    def test_manifest_change_and_wrong_identity_are_not_counted(self):
        self.data["entries"][2]["text"] = "changed draft"
        self.save(rehash=False)
        self.assertIsNone(self.tiktok()["queued"])
        self.data["account_id"] = "another-account"
        self.save()
        self.assertIsNone(self.tiktok()["queued"])

    def test_duplicate_registry_entries_do_not_double_count_events(self):
        with self.service.store.connect(write=True) as db:
            sources = self.service.store.meta(db, "planned_queue_sources")
            sources.append({**sources[0], "id": "another-source"})
            self.service.store.set_meta(db, "planned_queue_sources", sources)
        self.assertEqual(self.tiktok()["queued"], 8)

    def test_register_is_idempotent_and_rejects_files_outside_private_root(self):
        self.service.register_plan_queue(self.path)
        self.assertEqual(len(self.service.store.planned_queue_sources()), 1)
        with self.assertRaises(ValueError):
            self.service.register_plan_queue(self.root / "other.json")

    def test_queue_plan_change_does_not_trigger_extra_notification(self):
        self.assertTrue(self.service.collect())
        self.now = datetime.fromisoformat("2026-10-27T18:00:01+08:00").timestamp()
        self.assertTrue(self.service.collect())
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)
        self.data["entries"][2]["status"] = "confirmed"
        self.save()
        self.now += 30
        self.assertTrue(self.service.collect())
        self.service.drain()
        self.assertEqual(self.relay.send.call_count, 1)
        self.assertEqual(self.service.status()["queue_sources"][0]["queued"], 7)
