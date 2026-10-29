"""Owned publication and dependency contracts; all records and adapters are synthetic."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_promotion import native_bridge
from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.backends.common import (
    automatic_action_allowed,
    owned_x_target,
    save_profile,
    x_post_weight,
)
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput, canonical
from auto_promotion.queue_store import JobQueue
from auto_promotion.store import Store


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


class OriginalPostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.env = patch.dict(
            os.environ, {"AP_ACCOUNTS_DIR": str(Path(self.tmp.name) / "profiles")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.engine = Engine(
            self.store,
            AdapterRegistry(Path(self.tmp.name), overrides={"x": SimulationAdapter()}),
            clock=lambda: self.now,
        )
        self.account = self.engine.add_account(
            AccountInput(
                platform="x",
                label="synthetic",
                backend_label="synthetic",
                mode="simulation",
                min_interval=0,
                daily_limit=50,
                hourly_limit=20,
            )
        )
        self.engine.check_account(self.account["id"])
        self.engine.set_stop(False)

    def task(self, *, account=None, sources=None, **policy):
        account = account or self.account
        return self.engine.create_task(
            TaskInput(
                name="synthetic publication",
                account_ids=[account["id"]],
                policy={
                    "starts_at": iso(self.now - 1000),
                    "ends_at": iso(self.now + 172800),
                    "min_interval": 0 if account["mode"] == "simulation" else 120,
                    "daily_limit": 50,
                    "allow_owned_x_posts": True,
                    **(
                        {"outbound_after_task_ids": sources, "outbound_after_tasks_seconds": 3600}
                        if sources
                        else {}
                    ),
                    **policy,
                },
            )
        )

    def item(self, task, **changes):
        payload = {
            "account_id": task["account_ids"][0],
            "action": "post",
            "target": "https://x.com/simulation",
            "target_owned": True,
            "text": "Original post",
            "scheduled_at": iso(self.now + 30),
            **changes,
        }
        return self.engine.set_items(task["id"], [ItemInput(**payload)])

    def source(self):
        task = self.task(allow_owned_x_posts=False)
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="reply",
                    target=f"https://x.com/target/status/{100 + i}",
                    text=f"synthetic {i}",
                    scheduled_at=iso(self.now + 10 + i * 30),
                )
                for i in range(2)
            ],
        )
        return task

    def finish(self, task, *, status="confirmed", verified=True, published=True):
        with self.store.connect(write=True) as db:
            rows = db.execute(
                "SELECT id FROM items WHERE task_id=? ORDER BY position", (task["id"],)
            ).fetchall()
            for i, row in enumerate(rows):
                ref = "simulation://x/" + row["id"]
                result = {"verified": verified, "external_reference": ref}
                if published:
                    result["published_at"] = self.now - 20 + i * 10
                db.execute(
                    "UPDATE items SET status=?,result=?,external_reference=? WHERE id=?",
                    (status, canonical(result), ref, row["id"]),
                )
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (task["id"],))

    def test_owned_target_rejects_foreign_urls_and_credentials(self):
        self.assertTrue(owned_x_target("https://x.com/Brand", "brand"))
        for target in (
            "https://x.com/other",
            "https://x.com/brand?x=1",
            "https://user@x.com/brand",
            "https://x.com:443/brand",
            "https://x.com/brand/status/1",
        ):
            self.assertFalse(owned_x_target(target, "brand"), target)

    def test_weighted_text_budget_handles_long_links_and_korean(self):
        self.assertEqual(x_post_weight("한" * 140), 280)
        self.assertEqual(x_post_weight("a" * 256 + " https://example.com/" + "x" * 300), 280)
        self.item(self.task(), text="a" * 256 + " https://example.com/" + "x" * 300)
        with self.assertRaises(Conflict):
            self.item(self.task(), text="한" * 141)

    def test_accepted_post_receipt_survives_failure_without_resubmission(self):
        calls = []

        def accepted(platform, label, operation, payload, checkpoint, authorize):
            calls.append(operation)
            checkpoint(
                {
                    "status": "unknown",
                    "verified": False,
                    "external_reference": "https://x.com/brand/status/1234",
                    "target_verified": True,
                }
            )
            raise RuntimeError("verification transport lost")

        request = {
            "platform": "x",
            "account": "synthetic",
            "operation": "execute",
            "payload": {"item_id": "original-once", "payload": {"action": "post", "text": "exact"}},
        }
        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.browser.call", accepted),
        ):
            first = native_bridge.dispatch(request)
            again = native_bridge.dispatch(request)
        self.assertEqual(calls, ["execute"])
        self.assertEqual(first, again)
        self.assertEqual(first["external_reference"], "https://x.com/brand/status/1234")

    def test_post_requires_task_opt_in_and_owned_target(self):
        with self.assertRaises(Conflict):
            self.item(self.task(allow_owned_x_posts=False))
        with self.assertRaises(Conflict):
            self.item(self.task(), target="https://x.com/another")
        with self.assertRaises(Conflict):
            self.item(self.task(), target_owned=False)

    def test_unverified_completed_task_does_not_start_countdown(self):
        for status, verified, published in (
            ("missed", False, True),
            ("unknown", True, True),
            ("confirmed", False, True),
            ("confirmed", True, False),
        ):
            with self.subTest(status=status, verified=verified, published=published):
                source = self.source()
                self.finish(source, status=status, verified=verified, published=published)
                child = self.item(self.task(sources=[source["id"]]))
                child = self.engine.start_task(child["id"])
                if status == "missed":
                    self.assertEqual(child["state"], "completed")
                    self.assertEqual(
                        child["items"][0]["result"]["reason"], "interaction_dependencies_failed"
                    )
                    self.assertIsNone(child["manifest_hash"])
                    continue
                self.assertEqual(child["phase"], "starting")
                self.assertIsNone(child["manifest_hash"])
                self.assertEqual(child["readiness"]["workflow"]["status"], "waiting_interactions")

    def test_pending_placeholder_does_not_expire_while_waiting(self):
        source = self.source()
        child = self.item(self.task(sources=[source["id"]]))
        self.engine.start_task(child["id"])
        self.now += 7200
        JobQueue(self.engine).sync(child["id"])
        waiting = self.engine.task(child["id"])
        self.assertEqual(waiting["items"][0]["status"], "not_attempted")
        self.assertEqual(waiting["readiness"]["workflow"]["status"], "waiting_interactions")
        self.finish(source)
        JobQueue(self.engine).sync(child["id"])
        ready = self.engine.task(child["id"])
        self.assertEqual(ready["phase"], "ready")
        self.assertEqual(ready["items"][0]["due_at"], self.now - 10 + 3600)
        self.assertIsNotNone(ready["manifest_hash"])

    def test_actual_last_publication_drives_freeze_and_survives_restart(self):
        source = self.source()
        self.finish(source)
        child = self.item(self.task(sources=[source["id"]]))
        ready = self.engine.start_task(child["id"])
        self.assertEqual(ready["items"][0]["due_at"], self.now - 10 + 3600)
        self.assertEqual(ready["manifest"]["items"][0]["scheduled_at"], iso(self.now - 10 + 3600))
        previous_hash = ready["manifest_hash"]
        self.now += 60
        self.assertEqual(self.engine.start_task(child["id"])["manifest_hash"], previous_hash)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 0)

    def test_past_target_is_missed_without_attempt_or_catchup(self):
        source = self.source()
        self.finish(source)
        child = self.item(self.task(sources=[source["id"]]))
        self.now += 4000
        late = self.engine.start_task(child["id"])
        self.assertEqual(late["state"], "completed")
        self.assertEqual(late["items"][0]["status"], "missed")
        self.assertEqual(late["items"][0]["result"]["reason"], "interaction_schedule_elapsed")
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 0)

    def test_dependency_must_use_same_account_and_one_original_post(self):
        source = self.source()
        other = self.engine.add_account(
            AccountInput(
                platform="x",
                label="other",
                backend_label="other",
                mode="simulation",
                min_interval=0,
            )
        )
        with self.assertRaises(Conflict):
            self.task(account=other, sources=[source["id"]])
        with self.assertRaises(Conflict):
            self.item(
                self.task(sources=[source["id"]]), action="reply", target="https://x.com/a/status/1"
            )

    def test_relative_publication_never_shortens_existing_account_pacing(self):
        source = self.source()
        self.finish(source)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET next_at=? WHERE id=?", (self.now + 7200, self.account["id"])
            )
        child = self.item(self.task(sources=[source["id"]]))
        child = self.engine.start_task(child["id"])
        self.assertEqual(child["phase"], "starting")
        self.assertIsNone(child["manifest_hash"])
        self.assertEqual(
            child["readiness"]["items"][0]["reason"], "interaction_schedule_pacing_blocked"
        )
        self.assertEqual(self.engine.account(self.account["id"])["next_at"], self.now + 7200)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 0)

    def test_task_scoped_permission_does_not_expand_account_actions(self):
        account = {"platform": "x", "automatic_actions": ["reply"]}
        self.assertFalse(automatic_action_allowed(account, "post", {}))
        self.assertFalse(
            automatic_action_allowed(
                account, "post", {"allow_owned_x_posts": True, "execution_channel": "local"}
            )
        )
        self.assertTrue(
            automatic_action_allowed(
                account, "post", {"allow_owned_x_posts": True, "execution_channel": "cloud"}
            )
        )
        self.assertEqual(account["automatic_actions"], ["reply"])
        self.assertFalse(
            automatic_action_allowed(
                {"platform": "x", "automatic_actions": []},
                "post",
                {"allow_owned_x_posts": True, "execution_channel": "cloud"},
            )
        )

    def test_live_owned_post_uses_verified_profile_without_changing_opt_in(self):
        save_profile(
            "x",
            "brand",
            {"identity": "brand", "browser_channel": "msedge", "automatic_actions": ["reply"]},
        )
        account = self.engine.add_account(
            AccountInput(
                platform="x", label="brand", backend_label="brand", mode="live", min_interval=120
            )
        )
        task = self.task(account=account, execution_channel="cloud")
        self.item(task, target="https://x.com/brand")
        self.assertEqual(self.engine.account(account["id"])["automatic_actions"], ["reply"])
        with self.assertRaises(Conflict):
            self.item(
                self.task(account=account, execution_channel="cloud"), target="https://x.com/other"
            )
        with self.assertRaises(Conflict):
            self.task(account=account, execution_channel="local")


if __name__ == "__main__":
    unittest.main()
