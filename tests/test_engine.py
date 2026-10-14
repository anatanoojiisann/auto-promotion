import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import (
    AccountInput,
    AccountUpdate,
    CandidateInput,
    ItemInput,
    ManualOutcome,
    TaskInput,
)
from auto_promotion.store import Store


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class CountingAdapter(SimulationAdapter):
    def __init__(self, result=None):
        self.sends = 0
        self.result = result
        self.lock = threading.Lock()

    def call(self, account, operation, payload):
        if operation == "execute":
            with self.lock:
                self.sends += 1
            if isinstance(self.result, Exception):
                raise self.result
            if self.result is not None:
                return self.result
        return super().call(account, operation, payload)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.provider = CountingAdapter()
        self.registry = AdapterRegistry(
            Path(self.tmp.name),
            overrides={
                "telegram": self.provider,
                "reddit": self.provider,
                "instagram": self.provider,
                "x": self.provider,
            },
        )
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.engine = Engine(self.store, self.registry, clock=lambda: self.now)
        self.accounts = {}
        for p in ["telegram", "reddit", "instagram", "x"]:
            a = self.engine.add_account(
                AccountInput(
                    platform=p,
                    label=p,
                    backend_label=p,
                    mode="simulation",
                    min_interval=0,
                    daily_limit=20,
                    hourly_limit=20,
                )
            )
            self.engine.check_account(a["id"])
            self.accounts[p] = a
        self.engine.set_stop(False)

    def task(self, platform="telegram", **policy):
        if isinstance(platform, str):
            platform = [platform]
        return self.engine.create_task(
            TaskInput(
                name="Test task",
                account_ids=[self.accounts[p]["id"] for p in platform],
                policy={
                    "starts_at": iso(self.now - 100),
                    "ends_at": iso(self.now + 86400),
                    "min_interval": 0,
                    "daily_limit": 20,
                    **policy,
                },
            )
        )

    def item(self, platform="telegram", **changes):
        fields = {
            "account_id": self.accounts[platform]["id"],
            "action": "send_message",
            "target": "@ai_group",
            "text": "AI video example",
            "scheduled_at": iso(self.now),
        }
        if platform == "reddit":
            fields.update(
                action="reply",
                target="https://www.reddit.com/r/artificial/comments/abc123/example/",
            )
        if platform == "x":
            fields.update(action="reply", target="https://x.com/example/status/12345")
        if platform == "instagram":
            fields.update(action="manual_comment", target="https://www.instagram.com/p/example/")
        return ItemInput(**{**fields, **changes})

    def approve(self, t, items=None):
        t = self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        self.engine.set_items(t["id"], items or [self.item()])
        t = self.engine.prepare(t["id"])
        return self.engine.approve(t["id"], "outbound", t["outbound_phrase"])

    def test_four_platform_flow(self):
        t = self.approve(
            self.task(["telegram", "reddit", "x", "instagram"]),
            [self.item(), self.item("reddit"), self.item("x"), self.item("instagram")],
        )
        result = self.engine.run_once(t["id"])
        self.assertEqual(len(result["processed"]), 4)
        self.assertEqual(
            [i["status"] for i in self.engine.task(t["id"])["items"]],
            ["confirmed"] * 3 + ["manual_attention"],
        )
        self.assertEqual(self.provider.sends, 3)

    def test_exact_phrase_not_generic(self):
        t = self.task()
        for phrase in ["好的", "继续", "可以", t["discovery_phrase"] + " "]:
            with self.assertRaises(Conflict):
                self.engine.approve(t["id"], "discovery_join", phrase)
        self.assertEqual(len(self.engine.task(t["id"])["approvals"]), 0)

    def test_platform_publication_time_persists_without_changing_approved_manifest(self):
        task = self.approve(self.task())
        self.provider.result = {
            "status": "confirmed",
            "verified": True,
            "external_reference": "https://example.com/message",
            "published_at": self.now - 1,
        }
        self.engine.run_once(task["id"])
        item = self.engine.task(task["id"])["items"][0]
        evidence = self.store.one(
            "SELECT * FROM publication_evidence WHERE item_id=?", (item["id"],)
        )
        self.assertEqual(evidence["published_at"], self.now - 1)
        self.assertEqual(evidence["verified_at"], self.now)
        self.assertEqual(self.engine.task(task["id"])["manifest_hash"], task["manifest_hash"])

    def test_two_approvals_persist_and_are_idempotent(self):
        t = self.approve(self.task())
        new = Engine(Store(self.store.path), self.registry, clock=lambda: self.now)
        for stage, key in [("discovery_join", "discovery_phrase"), ("outbound", "outbound_phrase")]:
            new.approve(t["id"], stage, t[key])
        self.assertEqual(len(new.task(t["id"])["approvals"]), 2)
        new.run_once(t["id"])
        new.run_once(t["id"])
        self.assertEqual(self.provider.sends, 1)

    def test_draft_does_not_send(self):
        t = self.task()
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)

    def test_prepare_requires_discovery(self):
        t = self.task()
        with self.assertRaises(Conflict):
            self.engine.set_items(t["id"], [self.item()])

    def test_cannot_edit_frozen_batch(self):
        t = self.approve(self.task())
        with self.assertRaises(Conflict):
            self.engine.set_items(t["id"], [self.item(text="changed")])

    def test_manifest_tamper_is_blocked(self):
        t = self.approve(self.task())
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET manifest_hash='bad' WHERE id=?", (t["id"],))
        result = self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)
        self.assertEqual(result["processed"][0]["status"], "blocked")

    def test_item_tamper_is_blocked(self):
        t = self.approve(self.task())
        p = t["items"][0]["payload"]
        p["text"] = "changed"
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=? WHERE id=?", (json.dumps(p), t["items"][0]["id"])
            )
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)

    def test_account_change_invalidates_grant(self):
        t = self.approve(self.task())
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET backend_label=? WHERE id=?",
                ("changed", self.accounts["telegram"]["id"]),
            )
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)

    def test_verified_identity_cannot_be_registered_twice(self):
        class SharedIdentity(SimulationAdapter):
            def call(self, account, operation, payload):
                result = super().call(account, operation, payload)
                if operation == "health":
                    result["account_identity"] = "same-platform-user"
                return result

        self.registry.overrides["telegram"] = SharedIdentity()
        first = self.engine.add_account(
            AccountInput(platform="telegram", label="First", backend_label="first-live")
        )
        second = self.engine.add_account(
            AccountInput(platform="telegram", label="Second", backend_label="second-live")
        )
        self.assertEqual(self.engine.check_account(first["id"])["status"], "healthy")
        checked = self.engine.check_account(second["id"])
        self.assertEqual(checked["status"], "unavailable")
        self.assertEqual(checked["health"]["reason"], "DuplicatePlatformIdentity")

    def test_task_interval_applies_across_accounts(self):
        second = self.engine.add_account(
            AccountInput(
                platform="telegram", label="Second", backend_label="second", mode="simulation"
            )
        )
        task = self.engine.create_task(
            TaskInput(
                name="Alternating accounts",
                account_ids=[self.accounts["telegram"]["id"], second["id"]],
                policy={
                    "starts_at": iso(self.now - 100),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 120,
                    "ordering_mode": "independent_accounts",
                },
            )
        )
        task = self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        first_item = self.item(target="@one")
        second_item = self.item(account_id=second["id"], target="@two")
        with self.assertRaisesRegex(Conflict, "任务最小间隔"):
            self.engine.set_items(task["id"], [first_item, second_item])
        second_item.scheduled_at = iso(self.now + 120)
        self.engine.set_items(task["id"], [first_item, second_item])

    def test_independent_account_continues_when_another_is_disabled(self):
        second = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Second",
                backend_label="second",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(second["id"])
        task = self.engine.create_task(
            TaskInput(
                name="Independent accounts",
                account_ids=[self.accounts["telegram"]["id"], second["id"]],
                policy={
                    "starts_at": iso(self.now - 100),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 0,
                    "ordering_mode": "independent_accounts",
                },
            )
        )
        task = self.approve(
            task,
            [self.item(target="@one"), self.item(account_id=second["id"], target="@two")],
        )
        self.engine.disable_account(self.accounts["telegram"]["id"], True)
        self.engine.run_once(task["id"])
        statuses = [item["status"] for item in self.engine.task(task["id"])["items"]]
        self.assertEqual(statuses, ["not_attempted", "confirmed"])

    def test_account_settings_edit_requires_ending_active_task(self):
        account_id = self.accounts["telegram"]["id"]
        task = self.task()
        update = AccountUpdate(label="New label", daily_limit=7, hourly_limit=3, min_interval=0)
        with self.assertRaisesRegex(Conflict, "未结束任务"):
            self.engine.update_account(account_id, update)
        self.engine.change_state(task["id"], "cancelled")
        self.assertEqual(self.engine.update_account(account_id, update)["label"], "New label")

    def test_existing_database_adds_account_identity_and_task_pacing_columns(self):
        old_path = Path(self.tmp.name) / "old.db"
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY,platform TEXT,mode TEXT)")
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        migrated = Store(old_path)
        with migrated.connect() as db:
            accounts = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            tasks = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        self.assertIn("identity_hash", accounts)
        self.assertIn("next_at", tasks)

    def test_concurrent_workers_one_submission(self):
        t = self.approve(self.task())
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.engine.run_once(t["id"]), range(8)))
        self.assertEqual(self.provider.sends, 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)

    def test_transport_unknown_never_retried(self):
        self.provider.result = ConnectionError("token=secret-not-for-output")
        t = self.approve(self.task())
        self.engine.run_once(t["id"])
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 1)
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "unknown")
        self.assertNotIn(
            "secret-not-for-output", json.dumps(self.store.rows("SELECT * FROM calls"))
        )

    def test_claimed_crash_recovered_unknown(self):
        t = self.approve(self.task())
        self.engine._claim(t["items"][0]["id"])
        self.now += 301
        self.assertEqual(self.engine.recover(), 1)
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "unknown")

    def test_unverified_success_unknown(self):
        self.provider.result = {"status": "confirmed", "verified": False, "external_reference": "x"}
        t = self.approve(self.task())
        self.engine.run_once(t["id"])
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "unknown")

    def test_verified_without_reference_unknown(self):
        self.provider.result = {"status": "confirmed", "verified": True}
        t = self.approve(self.task())
        self.engine.run_once(t["id"])
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "unknown")

    def test_native_schedule_lookup_required(self):
        self.provider.result = {
            "status": "scheduled",
            "scheduled": True,
            "active": True,
            "verified": True,
            "message_ids": [1, 2],
            "lookup_message_ids": [1],
            "external_reference": "telegram:1",
        }
        t = self.approve(
            self.task(), [self.item(action="schedule_message", scheduled_at=iso(self.now + 3600))]
        )
        self.engine.run_once(t["id"])
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "unknown")

    def test_native_schedule_verified_before_due(self):
        t = self.approve(
            self.task(), [self.item(action="schedule_message", scheduled_at=iso(self.now + 3600))]
        )
        self.engine.run_once(t["id"])
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "scheduled")
        self.engine.change_state(t["id"], "cancelled")
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "scheduled")

    def test_future_local_schedule_not_early(self):
        t = self.approve(self.task(), [self.item(scheduled_at=iso(self.now + 3600))])
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)
        self.now += 3600
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 1)

    def test_missed_schedule_not_sent_late(self):
        t = self.approve(self.task())
        self.now += 901
        self.engine.run_once(t["id"])
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "missed")
        self.assertEqual(self.provider.sends, 0)

    def test_global_stop_pause_and_resume(self):
        t = self.approve(self.task())
        self.engine.set_stop(True)
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)
        self.engine.set_stop(False)
        self.engine.change_state(t["id"], "paused")
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)
        self.engine.change_state(t["id"], "active")
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 1)

    def test_cancel_is_terminal(self):
        t = self.approve(self.task())
        self.engine.change_state(t["id"], "cancelled")
        with self.assertRaises(Conflict):
            self.engine.change_state(t["id"], "active")
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)

    def test_daily_and_cross_task_account_cooldown(self):
        t = self.approve(
            self.task(daily_limit=1), [self.item(target="@one"), self.item(target="@two")]
        )
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 1)
        self.provider.result = {
            "status": "unknown",
            "rate_limited": True,
            "retry_after_seconds": 3600,
        }
        t2 = self.approve(self.task(), [self.item(target="@three")])
        self.engine.run_once(t2["id"])
        t3 = self.approve(self.task(), [self.item(target="@four")])
        self.engine.run_once(t3["id"])
        self.assertEqual(self.provider.sends, 2)
        self.assertGreater(
            self.engine.account(self.accounts["telegram"]["id"])["cooldown_until"], self.now
        )

    def test_risk_warning_disables_account(self):
        self.provider.result = {"status": "unknown", "risk_warning": True}
        t = self.approve(self.task())
        self.engine.run_once(t["id"])
        self.assertTrue(self.engine.account(self.accounts["telegram"]["id"])["disabled"])

    def test_cross_task_per_user_limit(self):
        for i in range(3):
            t = self.approve(self.task(), [self.item(target="@same", text=str(i))])
            self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 2)

    def test_media_tamper_blocks_submission(self):
        path = self.engine.media_root / "video.mp4"
        path.write_bytes(b"fixture")
        media = {
            "filename": "video.mp4",
            "path": str(path),
            "sha256": hashlib.sha256(b"fixture").hexdigest(),
        }
        t = self.approve(self.task(), [self.item(action="send_file", media=[media])])
        path.write_bytes(b"changed")
        self.engine.run_once(t["id"])
        self.assertEqual(self.provider.sends, 0)

    def test_unsupported_and_outside_account_rejected(self):
        t = self.task()
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        with self.assertRaises(Conflict):
            self.engine.set_items(t["id"], [self.item("reddit")])
        with self.assertRaises(Conflict):
            self.engine.set_items(t["id"], [self.item(action="tweet")])

    def test_policy_filter_persists_rejections(self):
        t = self.task(queries=["AI"], required_keywords=["AI"])
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        values = [
            {"target": "@good", "text": "AI video", "confidence": 0.9},
            {"target": "@casino", "text": "AI gambling", "confidence": 0.99},
            {"target": "@low", "text": "AI", "confidence": 0.1},
        ]
        for value in values:
            self.engine.candidate(
                t["id"],
                CandidateInput(account_id=self.accounts["telegram"]["id"], language="en", **value),
            )
        self.assertEqual(
            [
                c["decision"]
                for c in sorted(self.engine.task(t["id"])["candidates"], key=lambda c: c["target"])
            ],
            ["rejected", "qualified", "rejected"],
        )

    def test_unattempted_candidate_can_be_corrected_at_capacity(self):
        task = self.task(queries=["AI"], max_candidates=1)
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        value = CandidateInput(
            account_id=self.accounts["telegram"]["id"],
            target="@coding_chat",
            text="Coding discussion",
            language="en",
            confidence=0.95,
        )
        self.assertEqual(self.engine.candidate(task["id"], value)["decision"], "rejected")
        original = self.engine.task(task["id"])
        correction = value.model_copy(update={"text": "Coding discussion; AI source translation"})
        self.assertEqual(self.engine.candidate(task["id"], correction)["decision"], "qualified")
        saved = self.engine.task(task["id"])
        self.assertEqual(len(saved["candidates"]), 1)
        self.assertEqual(saved["candidates"][0]["id"], original["candidates"][0]["id"])
        self.assertEqual(saved["policy_hash"], original["policy_hash"])
        self.assertEqual(saved["approvals"], original["approvals"])
        with self.assertRaises(Conflict):
            self.engine.candidate(task["id"], correction.model_copy(update={"target": "@other"}))
        self.assertEqual(self.engine.candidate(task["id"], correction)["decision"], "qualified")

    def test_candidate_correction_still_applies_risk_gate(self):
        task = self.task(queries=["AI"], max_candidates=1)
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        value = CandidateInput(
            account_id=self.accounts["telegram"]["id"],
            target="@ai_chat",
            text="AI tools",
            language="en",
            confidence=0.95,
        )
        self.engine.candidate(task["id"], value)
        result = self.engine.candidate(task["id"], value.model_copy(update={"text": "AI gambling"}))
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("blocked_category", result["reasons"])

    def test_attempted_candidate_cannot_be_corrected(self):
        task = self.task(max_joins=1, max_candidates=1)
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        value = CandidateInput(
            account_id=self.accounts["telegram"]["id"],
            target="@ai_chat",
            text="AI tools",
            language="en",
            confidence=0.95,
        )
        self.engine.candidate(task["id"], value)
        self.engine.run_joins(task["id"])
        with self.assertRaises(Conflict):
            self.engine.candidate(task["id"], value.model_copy(update={"text": "AI video"}))

    def test_candidate_correction_cannot_change_frozen_batch(self):
        task = self.task(max_candidates=1)
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        value = CandidateInput(
            account_id=self.accounts["telegram"]["id"],
            target="@ai_group",
            text="AI tools",
            language="en",
            confidence=0.95,
        )
        self.engine.candidate(task["id"], value)
        self.engine.set_items(task["id"], [self.item()])
        frozen = self.engine.prepare(task["id"])
        with self.assertRaises(Conflict):
            self.engine.candidate(task["id"], value.model_copy(update={"text": "AI video"}))
        self.assertEqual(self.engine.task(task["id"])["manifest_hash"], frozen["manifest_hash"])

    def test_reconcile_does_not_resubmit(self):
        self.provider.result = ConnectionError()
        t = self.approve(self.task())
        self.engine.run_once(t["id"])
        self.engine.reconcile(t["items"][0]["id"])
        self.assertEqual(self.provider.sends, 1)
        self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "confirmed")

    def test_live_accounts_cannot_mix_simulation(self):
        live = self.engine.add_account(
            AccountInput(platform="telegram", label="live", backend_label="live")
        )
        with self.assertRaises(Conflict):
            self.engine.create_task(
                TaskInput(
                    name="mixed",
                    account_ids=[live["id"], self.accounts["telegram"]["id"]],
                    policy={"starts_at": iso(self.now), "ends_at": iso(self.now + 1000)},
                )
            )

    def test_join_gate_attempt_once_and_maximum(self):
        t = self.task(max_joins=1)
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        for target in ["@one", "@two"]:
            self.engine.candidate(
                t["id"],
                CandidateInput(
                    account_id=self.accounts["telegram"]["id"],
                    target=target,
                    language="en",
                    confidence=0.9,
                ),
            )
        self.assertEqual(len(self.engine.run_joins(t["id"])["processed"]), 1)
        self.assertEqual(self.engine.run_joins(t["id"])["processed"], [])
        self.assertEqual(len(self.store.rows("SELECT * FROM join_attempts")), 1)

    def test_join_pending_and_unknown_are_not_retried(self):
        for status in ["pending_admin_approval", "unknown"]:

            class Pending(SimulationAdapter):
                def call(self, account, operation, payload):
                    if operation == "join":
                        return {"status": status, "verified": True}
                    return super().call(account, operation, payload)

            self.registry.overrides["telegram"] = Pending()
            t = self.task(max_joins=2)
            self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
            self.engine.candidate(
                t["id"],
                CandidateInput(
                    account_id=self.accounts["telegram"]["id"],
                    target="@" + status,
                    language="en",
                    confidence=0.9,
                ),
            )
            r = self.engine.run_joins(t["id"])
            self.assertEqual(r["processed"][0]["status"], status)
            self.assertEqual(self.engine.run_joins(t["id"])["processed"], [])

    def test_live_owner_and_backend_gate_block_before_claim(self):
        a = self.engine.add_account(
            AccountInput(platform="telegram", label="Live test", backend_label="fake-live")
        )
        self.accounts["telegram"] = a
        self.engine.live_enabled = True
        t = self.approve(self.task(min_interval=120))
        self.engine.check_account(a["id"])
        with patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": ""}):
            self.assertIsNone(self.engine._claim(t["items"][0]["id"], {"allowed": True}))
            self.assertFalse(self.engine.account(a["id"])["writable"])
        with patch.dict(
            os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": "auto-promotion"}
        ):
            self.assertIsNone(self.engine._claim(t["items"][0]["id"], {"allowed": False}))
            self.assertIsNone(self.engine._claim(t["items"][0]["id"]))
            self.assertEqual(self.engine.task(t["id"])["items"][0]["status"], "not_attempted")
            self.assertIsNotNone(
                self.engine._claim(t["items"][0]["id"], {"allowed": True, "daily_remaining": 1})
            )
        self.assertEqual(self.provider.sends, 0)

    def test_join_reconcile_never_rejoins_after_unknown(self):
        ops = []

        class Uncertain(SimulationAdapter):
            def call(self, account, operation, payload):
                ops.append(operation)
                if operation == "join":
                    return {"status": "unknown"}
                return super().call(account, operation, payload)

        self.registry.overrides["telegram"] = Uncertain()
        t = self.task(max_joins=2)
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        self.engine.candidate(
            t["id"],
            CandidateInput(
                account_id=self.accounts["telegram"]["id"],
                target="@uncertain",
                language="en",
                confidence=0.9,
            ),
        )
        self.engine.run_joins(t["id"])
        c = self.engine.task(t["id"])["candidates"][0]
        self.assertEqual(self.engine.reconcile_join(c["id"])["status"], "joined")
        self.engine.run_joins(t["id"])
        self.assertEqual(ops.count("join"), 1)
        self.assertEqual(ops.count("verify_join"), 1)

    def test_worker_runs_only_approved_qualified_joins(self):
        t = self.task(max_joins=2)
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        self.engine.candidate(
            t["id"],
            CandidateInput(
                account_id=self.accounts["telegram"]["id"],
                target="@allowed",
                language="en",
                confidence=0.9,
            ),
        )
        self.engine.candidate(
            t["id"],
            CandidateInput(
                account_id=self.accounts["telegram"]["id"],
                target="@low",
                language="en",
                confidence=0.1,
            ),
        )
        self.engine.run_cycle()
        self.engine.run_cycle()
        self.assertEqual(len(self.store.rows("SELECT * FROM join_attempts")), 1)

    def test_schedule_reserves_delivery_day_across_submission_days(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET daily_limit=1 WHERE id=?", (self.accounts["telegram"]["id"],)
            )
        due = self.now + 3 * 86400
        one = self.approve(
            self.task(ends_at=iso(due + 3600)),
            [self.item(action="schedule_message", target="@one", scheduled_at=iso(due))],
        )
        self.engine.run_once(one["id"])
        self.now += 86400
        two = self.approve(
            self.task(ends_at=iso(due + 3600)),
            [self.item(action="schedule_message", target="@two", scheduled_at=iso(due + 120))],
        )
        self.engine.run_once(two["id"])
        self.assertEqual(self.provider.sends, 1)
        self.assertEqual(self.engine.task(two["id"])["items"][0]["status"], "not_attempted")

    def test_manual_result_finishes_four_platform_task(self):
        t = self.approve(
            self.task(["telegram", "reddit", "x", "instagram"]),
            [self.item(), self.item("reddit"), self.item("x"), self.item("instagram")],
        )
        self.engine.run_once(t["id"])
        i = self.engine.task(t["id"])["items"][-1]
        r = self.engine.manual_outcome(
            i["id"],
            ManualOutcome(
                observed_text=i["payload"]["text"],
                external_reference="https://www.instagram.com/p/example/",
                verified=True,
            ),
        )
        self.assertEqual(r["status"], "confirmed")
        self.assertEqual(self.engine.task(t["id"])["state"], "completed")

    def test_late_receipt_does_not_reopen_cancelled_task(self):
        t = self.approve(self.task())
        claim = self.engine._claim(t["items"][0]["id"])
        self.engine.change_state(t["id"], "cancelled")
        self.engine.finish(
            claim,
            {"status": "confirmed", "verified": True, "external_reference": "simulation://late"},
        )
        self.assertEqual(self.engine.task(t["id"])["state"], "cancelled")

    def test_instagram_live_media_requires_source_before_approval(self):
        a = self.engine.add_account(
            AccountInput(platform="instagram", label="Live test", backend_label="test_creator")
        )
        self.accounts["instagram"] = a
        t = self.task("instagram", min_interval=120)
        self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
        media = {
            "filename": "test.png",
            "path": "missing",
            "sha256": "0" * 64,
            "mime_type": "image/png",
            "media_type": "IMAGE",
        }
        with self.assertRaisesRegex(Conflict, "HTTPS"):
            self.engine.set_items(
                t["id"],
                [
                    self.item(
                        "instagram",
                        action="publish",
                        target="owned",
                        target_owned=True,
                        media=[media],
                    )
                ],
            )

    def test_two_instagram_accounts_can_prepare_publish_without_global_account_setting(self):
        accounts = [
            self.engine.add_account(
                AccountInput(platform="instagram", label=f"IG {n}", backend_label=f"ig-{n}")
            )
            for n in (1, 2)
        ]
        task = self.engine.create_task(
            TaskInput(
                name="Two Instagram accounts",
                account_ids=[account["id"] for account in accounts],
                policy={
                    "starts_at": iso(self.now - 100),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 120,
                },
            )
        )
        task = self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        media_path = self.engine.media_root / "post.png"
        media_path.write_bytes(b"test image")
        media = {
            "filename": "post.png",
            "path": str(media_path),
            "sha256": hashlib.sha256(media_path.read_bytes()).hexdigest(),
            "mime_type": "image/png",
            "media_type": "IMAGE",
            "source_url": "https://example.com/post.png",
        }
        items = [
            self.item(
                "instagram",
                account_id=account["id"],
                action="publish",
                target=str(index),
                target_owned=True,
                media=[media],
                scheduled_at=iso(self.now + index * 120),
            )
            for index, account in enumerate(accounts, 1)
        ]
        with patch.dict(os.environ, {}, clear=True):
            saved = self.engine.set_items(task["id"], items)
        self.assertEqual(len(saved["items"]), 2)

    def test_all_mvp_outbound_actions_in_simulation(self):
        from auto_promotion.models import CAPABILITIES

        path = self.engine.media_root / "synthetic.png"
        path.write_bytes(b"synthetic-media-fixture")
        media = {
            "filename": path.name,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mime_type": "image/png",
            "media_type": "IMAGE",
        }
        number = 0
        for platform in ["telegram", "reddit", "instagram", "x"]:
            for action in sorted(CAPABILITIES[platform] - {"discover", "join"}):
                number += 1
                changes = {"action": action}
                if platform == "telegram":
                    changes["target"] = "@sim_" + action
                    if action == "reply":
                        changes["reply_to"] = 123
                    if action in {"send_file", "schedule_file"}:
                        changes["media"] = [media]
                    if action.startswith("schedule_"):
                        changes["scheduled_at"] = iso(self.now + 3600)
                if platform == "reddit":
                    changes["target"] = (
                        "testcommunity"
                        if action == "post"
                        else "https://www.reddit.com/r/test/comments/123/test/"
                    )
                    if action == "post":
                        changes["title"] = "Synthetic title"
                if platform == "instagram" and action == "publish":
                    changes.update(target="owned", target_owned=True, media=[media])
                if platform == "x":
                    changes["target"] = "https://x.com/example/status/" + str(number)
                    if action in {"like", "repost"}:
                        changes["text"] = ""
                t = self.approve(self.task(platform), [self.item(platform, **changes)])
                self.engine.run_once(t["id"])
                item = self.engine.task(t["id"])["items"][0]
                expected = (
                    "manual_attention"
                    if action == "manual_comment"
                    else "scheduled"
                    if action.startswith("schedule_")
                    else "confirmed"
                )
                self.assertEqual(item["status"], expected, (platform, action))
                if expected != "manual_attention":
                    self.assertTrue(item["external_reference"].startswith("simulation://"))
        self.assertEqual(number, 13)
        self.assertEqual(self.provider.sends, 12)

    def test_simulated_discovery_flows_through_qualification(self):
        for platform in ["telegram", "reddit", "x"]:
            t = self.task(platform, queries=["AI video"])
            self.engine.approve(t["id"], "discovery_join", t["discovery_phrase"])
            result = self.engine.discover(t["id"], self.accounts[platform]["id"])
            self.assertEqual(result["decisions"][0]["decision"], "qualified")
            self.assertEqual(len(self.engine.task(t["id"])["candidates"]), 1)
        self.assertEqual(self.provider.sends, 0)

    def test_missing_timezone_rejected(self):
        with self.assertRaises(ValueError):
            self.item(scheduled_at="2026-09-06T10:00:00")


if __name__ == "__main__":
    unittest.main()
