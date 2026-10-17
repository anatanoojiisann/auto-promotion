import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.campaigns import Campaigns
from auto_promotion.core import Conflict, Engine
from auto_promotion.dispatcher import AccountBusy, AccountLanes
from auto_promotion.drafts import DraftResult
from auto_promotion.models import (
    AccountInput,
    AccountProfile,
    AssignmentUpdate,
    CampaignInput,
    ExecutionSettings,
    ItemInput,
    OpportunityInput,
    PoolInput,
    TaskInput,
)
from auto_promotion.planner import content_key, render_template
from auto_promotion.pools import Pools
from auto_promotion.quotas import Calendar
from auto_promotion.quotas import events as quota_events
from auto_promotion.store import Store


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class RecordingAdapter(SimulationAdapter):
    def __init__(self):
        self.calls = []
        self.result = None
        self.barrier = None
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def call(self, account, operation, payload):
        if operation == "preflight":
            return {"allowed": True}
        if operation == "execute":
            with self.lock:
                self.calls.append((account["id"], payload["item_id"]))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if self.barrier:
                    self.barrier.wait(timeout=10)
                else:
                    time.sleep(0.01)
                return self.result or super().call(account, operation, payload)
            finally:
                with self.lock:
                    self.active -= 1
        return super().call(account, operation, payload)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.adapter = RecordingAdapter()
        self.store = Store(Path(self.tmp.name) / "campaigns.db")
        self.engine = Engine(
            self.store,
            AdapterRegistry(
                Path(self.tmp.name),
                overrides={
                    p: self.adapter for p in ("telegram", "x", "reddit", "instagram", "discord")
                },
            ),
            clock=lambda: self.now,
        )
        self.pools, self.campaigns = Pools(self.engine), Campaigns(self.engine)
        self.engine.set_stop(False)
        self.accounts = [self.account("first"), self.account("second")]
        self.pool = self.pools.save(
            PoolInput(name="Test pool", account_ids=[a["id"] for a in self.accounts])
        )

    def account(self, label, platform="telegram", mode="simulation"):
        account = self.engine.add_account(
            AccountInput(
                platform=platform,
                label=label,
                backend_label=label,
                mode=mode,
                min_interval=0 if mode == "simulation" else 120,
                daily_limit=100,
                hourly_limit=20,
            )
        )
        self.engine.check_account(account["id"])
        self.pools.set_profile(
            account["id"],
            AccountProfile(
                topics=["video"],
                languages=["en"],
                reply_template="Regarding {title}: {facts}",
                facts="Approved test product fact.",
            ),
        )
        return self.engine.account(account["id"])

    def opportunity(self, n, **changes):
        return OpportunityInput(
            **{
                "platform": "telegram",
                "external_id": str(n),
                "target": f"@test_group_{n}",
                "reply_to": n + 1,
                "author_id": f"author-{n}",
                "title": "Video export",
                "context": "video export question",
                "topics": ["video"],
                "language": "en",
                "confidence": 0.9,
                **changes,
            }
        )

    def campaign(self, count=3, *, pool=None, values=None, **policy):
        imported = self.campaigns.import_content(
            values or [self.opportunity(n) for n in range(count)]
        )
        return self.campaigns.create(
            CampaignInput(
                name="Test campaign",
                pool_id=(pool or self.pool)["id"],
                opportunity_ids=imported["ids"],
                policy={
                    "starts_at": iso(self.now - 100),
                    "ends_at": iso(self.now + 14 * 86400),
                    "daily_limit": 100,
                    "min_interval": 0
                    if (pool or self.pool)["accounts"][0]["mode"] == "simulation"
                    else 120,
                    "languages": ["en"],
                    **policy,
                },
            )
        )

    def freeze(self, campaign):
        campaign = self.campaigns.materialize(campaign["id"])
        for row in campaign["tasks"]:
            task = self.engine.task(row["id"])
            self.engine.start_task(task["id"], sending=False)
            self.campaigns.populate(task["id"])
            task = self.engine.prepare(task["id"])
            self.engine.start_task(task["id"])
        return self.campaigns.get(campaign["id"])

    def test_thirty_accounts_and_thousand_contents_are_fair_and_explainable(self):
        for n in range(28):
            self.accounts.append(self.account(f"account-{n}"))
        pool = self.pools.save(
            PoolInput(name="Thirty", account_ids=[a["id"] for a in self.accounts])
        )
        campaign = self.campaign(pool=pool, count=1000)
        planned = self.campaigns.plan(campaign["id"])
        self.assertEqual(planned["summary"], {"planned": 1000})
        counts = [a["planned"] for a in planned["accounts"]]
        self.assertEqual(len(counts), 30)
        self.assertLessEqual(max(counts) - min(counts), 1)
        self.assertTrue(all(a["reasons"] for a in planned["assignments"]))
        materialized = self.campaigns.materialize(campaign["id"])
        self.assertEqual(len(materialized["tasks"]), 30)
        self.assertEqual(self.store.one("SELECT count(*) n FROM approvals")["n"], 0)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.campaigns.materialize(campaign["id"])["tasks"]), 30)

    def test_canonical_identifiers_deduplicate_url_variants(self):
        a = self.opportunity(
            1, platform="x", target="https://x.com/name/status/123?ref=one", reply_to=None
        )
        b = self.opportunity(
            1, platform="x", target="https://twitter.com/other/status/123", reply_to=None
        )
        self.assertEqual(content_key(a.model_dump()), content_key(b.model_dump()))
        result = self.campaigns.import_content([a, b])
        self.assertEqual(result["ids"][0], result["ids"][1])
        self.assertEqual(self.campaigns.content()["total"], 1)

    def test_rejection_and_expiry_have_reasons(self):
        values = [
            self.opportunity(1, permission="denied"),
            self.opportunity(2, expires_at=iso(self.now - 1)),
            self.opportunity(3, language="fr"),
        ]
        campaign = self.campaign(values=values)
        result = self.campaigns.plan(campaign["id"])
        self.assertEqual(result["summary"]["unassigned"], 3)
        self.assertTrue(all(a["reasons"] and a["payload"] is None for a in result["assignments"]))

    def test_persona_change_invalidates_unmaterialized_plan(self):
        campaign = self.campaigns.plan(self.campaign()["id"])
        self.pools.set_profile(self.accounts[0]["id"], AccountProfile(role="new role"))
        with self.assertRaisesRegex(Conflict, "重新规划"):
            self.campaigns.materialize(campaign["id"])

    def test_frozen_profile_versions_survive_profile_updates(self):
        campaign = self.freeze(self.campaigns.plan(self.campaign(count=1)["id"]))
        task = self.engine.task(campaign["tasks"][0]["id"])
        old_hash = task["manifest_hash"]
        account_id = task["account_ids"][0]
        self.pools.set_profile(account_id, AccountProfile(reply_template="new text"))
        self.assertEqual(task["manifest"]["profiles"][account_id]["version"], 1)
        self.now = task["items"][0]["due_at"]
        self.engine.run_once(task["id"])
        self.assertEqual(self.engine.task(task["id"])["manifest_hash"], old_hash)
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")
        self.assertEqual(self.campaigns.get(campaign["id"])["state"], "completed")
        self.assertEqual(len(self.campaigns.materialize(campaign["id"])["tasks"]), 1)

    def test_drafts_can_be_edited_only_before_batches(self):
        campaign = self.campaigns.plan(self.campaign(count=1)["id"])
        assignment = campaign["assignments"][0]
        self.campaigns.edit_assignment(
            assignment["id"], AssignmentUpdate(text="Reviewed exact text")
        )
        campaign = self.campaigns.materialize(campaign["id"])
        with self.assertRaises(Conflict):
            self.campaigns.edit_assignment(assignment["id"], AssignmentUpdate(text="late edit"))
        self.assertEqual(campaign["assignments"][0]["payload"]["text"], "Reviewed exact text")

    def test_unknown_retains_contact_and_never_reassigns_or_resends(self):
        campaign = self.freeze(self.campaigns.plan(self.campaign(count=1)["id"]))
        task = self.engine.task(campaign["tasks"][0]["id"])
        self.adapter.result = {"status": "unknown", "reason": "transport_unknown"}
        self.now = task["items"][0]["due_at"]
        self.engine.run_once()
        self.engine.run_once()
        self.assertEqual(len(self.adapter.calls), 1)
        second = self.campaigns.plan(self.campaign(count=1)["id"])
        self.assertEqual(second["summary"], {"unassigned": 1})
        self.assertIn("其他活动", second["assignments"][0]["reasons"][0])

    def test_cancel_releases_only_unattempted_contacts_and_reserved_quotas(self):
        campaign = self.freeze(self.campaigns.plan(self.campaign(count=2)["id"]))
        self.assertEqual(
            self.store.one("SELECT count(*) n FROM quota_reservations WHERE state='reserved'")["n"],
            2,
        )
        self.campaigns.change_state(campaign["id"], "cancelled")
        self.assertEqual(self.store.one("SELECT count(*) n FROM content_contacts")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM quota_reservations")["n"], 0)

    def test_permission_is_account_specific(self):
        campaign = self.campaign(
            values=[self.opportunity(1, permission_account_ids=[self.accounts[1]["id"]])]
        )
        result = self.campaigns.plan(campaign["id"])
        self.assertEqual(result["assignments"][0]["account_id"], self.accounts[1]["id"])

    def test_live_x_is_manual_without_browser_writes(self):
        account = self.account("live-x", "x", "live")
        pool = self.pools.save(PoolInput(name="X", account_ids=[account["id"]]))
        campaign = self.campaign(
            pool=pool,
            values=[
                self.opportunity(
                    1, platform="x", target="https://x.com/test/status/123", reply_to=None
                )
            ],
        )
        campaign = self.freeze(self.campaigns.plan(campaign["id"]))
        task = self.engine.task(campaign["tasks"][0]["id"])
        self.now = task["items"][0]["due_at"]
        result = self.engine.run_once(task["id"])
        self.assertEqual(result["processed"][0]["status"], "manual_attention")
        self.assertEqual(self.adapter.calls, [])

    def test_opted_in_x_reply_runs_and_manual_handoff_does_not_consume_delivery(self):
        from unittest.mock import patch

        account = self.account("live-x", "x", "live")
        pool = self.pools.save(PoolInput(name="X", account_ids=[account["id"]]))
        campaign = self.freeze(
            self.campaigns.plan(
                self.campaign(
                    pool=pool,
                    values=[
                        self.opportunity(
                            1, platform="x", target="https://x.com/test/status/123", reply_to=None
                        )
                    ],
                )["id"]
            )
        )
        task = self.engine.task(campaign["tasks"][0]["id"])
        self.now = task["items"][0]["due_at"]
        self.engine.run_once(task["id"])
        with self.store.connect() as db:
            self.assertEqual(quota_events(db, "live"), [])
            self.assertEqual(quota_events(db, "live", delivery=True), [])
        self.campaigns.change_state(campaign["id"], "cancelled")
        self.assertEqual(self.store.one("SELECT count(*) n FROM content_contacts")["n"], 0)
        with (
            patch("auto_promotion.core.automatic_actions", return_value=["reply"]),
            patch.object(self.engine.adapters, "commissioned", return_value=True),
        ):
            self.engine.live_enabled = True
            campaign = self.freeze(
                self.campaigns.plan(
                    self.campaign(
                        pool=pool,
                        values=[
                            self.opportunity(
                                1,
                                platform="x",
                                target="https://x.com/test/status/123",
                                reply_to=None,
                            )
                        ],
                    )["id"]
                )
            )
            task = self.engine.task(campaign["tasks"][0]["id"])
            self.assertEqual(task["items"][0]["payload"]["execution_mode"], "automatic")
            self.now = task["items"][0]["due_at"]
            execution = self.engine.run_once(task["id"])
            self.assertEqual(
                self.engine.task(task["id"])["items"][0]["status"], "confirmed", execution
            )
            self.engine.run_once(task["id"])
            self.assertEqual(len(self.adapter.calls), 1)

    def test_x_automation_opt_in_still_rejects_denied_and_wrong_account(self):
        from auto_promotion.planner import execution_mode

        account = {"id": "one", "mode": "live", "platform": "x", "automatic_actions": ["reply"]}
        value = {
            "action": "reply",
            "permission": "unknown",
            "permission_account_ids": [],
            "is_public": True,
        }
        self.assertEqual(execution_mode(account, value)[0], "automatic")
        self.assertEqual(execution_mode(account, {**value, "permission": "denied"})[0], "disabled")
        self.assertEqual(
            execution_mode(account, {**value, "permission_account_ids": ["two"]})[0], "disabled"
        )
        self.assertEqual(execution_mode(account, {**value, "action": "like"})[0], "disabled")
        self.assertEqual(execution_mode(account, {**value, "action": "quote"})[0], "manual")

    def test_template_cannot_read_attributes_or_format_arbitrary_fields(self):
        with self.assertRaises(Conflict):
            render_template("{context.__class__}", {})
        with self.assertRaises(Conflict):
            render_template("{facts!r}", {})
        self.assertEqual(
            render_template("{{literal}} {role}", {"role": "support"}), "{literal} support"
        )

    def test_generated_drafts_remain_local_until_exact_approvals(self):
        campaign = self.campaigns.plan(self.campaign(count=2)["id"])

        class Provider:
            def generate(self, profile, opportunity):
                return DraftResult(text="AI reviewed draft for " + opportunity["title"])

        result = self.campaigns.generate_drafts(
            campaign["id"], [a["id"] for a in campaign["assignments"]], Provider()
        )
        self.assertTrue(
            all(a["payload"]["text"].startswith("AI reviewed") for a in result["assignments"])
        )
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.one("SELECT count(*) n FROM approvals")["n"], 0)
        self.campaigns.materialize(campaign["id"])
        with self.assertRaises(Conflict):
            self.campaigns.generate_drafts(
                campaign["id"], [result["assignments"][0]["id"]], Provider()
            )

    def test_campaign_pause_blocks_child_start(self):
        campaign = self.campaigns.materialize(
            self.campaigns.plan(self.campaign(count=2)["id"])["id"]
        )
        self.campaigns.change_state(campaign["id"], "paused")
        task = self.engine.task(campaign["tasks"][0]["id"])
        with self.assertRaisesRegex(Conflict, "活动已暂停"):
            self.engine.start_task(task["id"])
        self.engine.run_once()
        self.assertEqual(self.adapter.calls, [])
        self.campaigns.change_state(campaign["id"], "active")
        task = self.engine.start_task(task["id"])
        self.now = task["items"][0]["due_at"]
        self.engine.run_once()
        self.assertEqual(len(self.adapter.calls), 1)

    def test_reserved_capacity_cannot_be_sold_to_another_frozen_batch(self):
        self.engine.configure_execution(ExecutionSettings(global_limit={"daily": 1, "hourly": 1}))
        first = self.campaigns.plan(self.campaign(count=1)["id"])
        second = self.campaigns.plan(self.campaign(values=[self.opportunity(99)])["id"])
        first = self.freeze(first)
        second = self.campaigns.materialize(second["id"])
        task = self.engine.task(second["tasks"][0]["id"])
        self.engine.start_task(task["id"], sending=False)
        self.campaigns.populate(task["id"])
        with self.assertRaisesRegex(Conflict, "额度"):
            self.engine.prepare(task["id"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM quota_reservations")["n"], 1)

    def test_same_account_is_serial_across_multiple_tasks(self):
        account = self.accounts[0]
        for n in range(2):
            task = self.engine.create_task(
                TaskInput(
                    name=f"Serial {n}",
                    account_ids=[account["id"]],
                    policy={
                        "starts_at": iso(self.now - 10),
                        "ends_at": iso(self.now + 3600),
                        "daily_limit": 20,
                        "min_interval": 0,
                        "ordering_mode": "independent_accounts",
                    },
                )
            )
            self.engine.start_task(task["id"], sending=False)
            self.engine.set_items(
                task["id"],
                [
                    ItemInput(
                        account_id=account["id"],
                        action="send_message",
                        target=f"@serial_{n}",
                        text="Exact",
                        scheduled_at=iso(self.now),
                    )
                ],
            )
            task = self.engine.prepare(task["id"])
            self.engine.start_task(task["id"])
        self.engine.run_once()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.peak, 1)

    def test_account_lane_is_shared_across_engines_and_released(self):
        other = AccountLanes(self.store)
        account_id = self.accounts[0]["id"]
        with self.engine.lanes.acquire(account_id):
            with self.assertRaises(AccountBusy):
                with other.acquire(account_id):
                    self.fail("Lane overlapped")
        with other.acquire(account_id):
            pass

    def test_different_accounts_really_execute_concurrently(self):
        self.adapter.barrier = threading.Barrier(2)
        task = self.engine.create_task(
            TaskInput(
                name="Concurrent",
                account_ids=[a["id"] for a in self.accounts],
                policy={
                    "starts_at": iso(self.now - 10),
                    "ends_at": iso(self.now + 3600),
                    "daily_limit": 20,
                    "min_interval": 0,
                    "pacing_scope": "account",
                    "ordering_mode": "independent_accounts",
                },
            )
        )
        self.engine.start_task(task["id"], sending=False)
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=a["id"],
                    action="send_message",
                    target=f"@parallel_{n}",
                    text="Exact",
                    scheduled_at=iso(self.now),
                )
                for n, a in enumerate(self.accounts)
            ],
        )
        task = self.engine.prepare(task["id"])
        self.engine.start_task(task["id"])
        result = self.engine.run_once(task["id"])
        self.assertEqual(self.adapter.peak, 2)
        self.assertEqual([r["status"] for r in result["processed"]], ["confirmed", "confirmed"])

    def test_calendar_protects_future_rolling_hour_reservations(self):
        calendar = Calendar([{"mode": "simulation", "global": "simulation", "at": 2000}])
        limit = [("global", "simulation", {"daily": 10, "hourly": 1})]
        self.assertIsNotNone(calendar.blocker(limit, 1000, "simulation")[0])
        self.assertEqual(calendar.slot(limit, 1000, 10000, "simulation"), 5600)

    def test_settings_increases_cannot_raise_existing_task_approval(self):
        self.engine.configure_execution(ExecutionSettings(global_limit={"daily": 1, "hourly": 1}))
        task = self.engine.create_task(
            TaskInput(
                name="Frozen limits",
                account_ids=[a["id"] for a in self.accounts],
                policy={
                    "starts_at": iso(self.now - 10),
                    "ends_at": iso(self.now + 3600),
                    "daily_limit": 20,
                    "min_interval": 0,
                    "pacing_scope": "account",
                    "ordering_mode": "independent_accounts",
                },
            )
        )
        self.engine.start_task(task["id"], sending=False)
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=a["id"],
                    action="send_message",
                    target=f"@limited_{n}",
                    text="Exact",
                    scheduled_at=iso(self.now),
                )
                for n, a in enumerate(self.accounts)
            ],
        )
        task = self.engine.prepare(task["id"])
        self.engine.start_task(task["id"])
        self.engine.configure_execution(
            ExecutionSettings(global_limit={"daily": 100, "hourly": 20})
        )
        self.engine.run_once(task["id"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            json.loads(
                self.store.one("SELECT policy FROM tasks WHERE id=?", (task["id"],))["policy"]
            )["quota_snapshot"]["execution"]["global_limit"]["daily"],
            1,
        )

    def test_client_cannot_forge_a_future_global_quota_snapshot(self):
        self.engine.configure_execution(ExecutionSettings(global_limit={"daily": 1, "hourly": 1}))
        task = self.engine.create_task(
            TaskInput(
                name="No forged quota",
                account_ids=[self.accounts[0]["id"]],
                policy={
                    "starts_at": iso(self.now - 10),
                    "ends_at": iso(self.now + 3600),
                    "quota_snapshot": {
                        "execution": ExecutionSettings(
                            global_limit={"daily": 10000, "hourly": 1000}
                        ).model_dump()
                    },
                },
            )
        )
        self.assertEqual(
            task["policy"]["quota_snapshot"]["execution"]["global_limit"], {"daily": 1, "hourly": 1}
        )
