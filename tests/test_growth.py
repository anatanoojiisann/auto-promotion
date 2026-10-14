import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.campaigns import Campaigns
from auto_promotion.core import Conflict, Engine
from auto_promotion.growth import Growth
from auto_promotion.growth_models import (
    ConversionEvent,
    FeedInput,
    GrowthChannel,
    GrowthGoal,
    InboxUpdate,
    StoreMetric,
)
from auto_promotion.models import (
    AccountInput,
    CampaignInput,
    OpportunityInput,
    PoolInput,
    TaskInput,
    canonical,
)
from auto_promotion.pools import Pools
from auto_promotion.statistics import Statistics
from auto_promotion.store import Store


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.fromisoformat("2026-10-14T12:00:00+08:00").timestamp()
        self.store = Store(Path(self.tmp.name) / "growth.db")
        self.engine = Engine(
            self.store, AdapterRegistry(Path(self.tmp.name)), clock=lambda: self.now
        )
        self.growth = Growth(self.engine)
        self.goal = self.growth.save_goal(
            GrowthGoal(
                name="Business measurement",
                app_store_url="https://apps.apple.com/us/app/aurax/id6779545792",
                starts_at=iso(self.now - 7 * 86400),
                ends_at=iso(self.now + 86400),
                downloads_target=100,
                paid_target=10,
            )
        )
        self.channel = self.growth.add_channel(
            self.goal["id"],
            GrowthChannel(
                name="Telegram",
                campaign_token="tg01",
                provider_token="123456",
            ),
        )

    def event(self, kind, user="user1", at=None, **changes):
        return ConversionEvent(
            **{
                "source": "app-export",
                "event_id": user + "-" + kind,
                "kind": kind,
                "subject_key": user,
                "channel_id": self.channel["id"],
                "occurred_at": iso(self.now - 6 * 86400 if at is None else at),
                "evidence_reference": "export-row",
                "country": "US",
                **changes,
            }
        )

    def report(self):
        return self.growth.report(self.goal["id"])

    def test_missing_data_not_zero_and_no_changes_to_execution(self):
        r = self.report()
        self.assertIsNone(r["store_metrics"]["first_downloads"])
        self.assertIsNone(r["observed_cac_minor"])
        self.assertEqual(r["data_status"], "not_connected")
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.assertFalse(self.engine.live_enabled)

    def test_mature_trial_cohort_refund_currency_and_late_paid_followup(self):
        values = [
            self.event("first_download"),
            self.event("first_generation", at=self.now - 5 * 86400),
            self.event(
                "trial_start", at=self.now - 5 * 86400, trial_ends_at=iso(self.now - 2 * 86400)
            ),
            self.event(
                "first_charge",
                at=self.now - 86400,
                transaction_id="tx1",
                amount_minor=999,
                currency="USD",
            ),
            self.event(
                "refund", at=self.now - 3600, transaction_id="tx1", amount_minor=400, currency="USD"
            ),
            self.event("first_download", user="new", at=self.now - 10000),
            self.event(
                "trial_start", user="new", at=self.now - 9000, trial_ends_at=iso(self.now + 86400)
            ),
        ]
        self.growth.import_events(self.goal["id"], values)
        r = self.report()
        self.assertEqual(
            r["event_funnel"],
            {"first_download": 2, "first_generation": 1, "trial_start": 2, "first_charge": 1},
        )
        self.assertEqual(
            (r["mature_trials"], r["immature_trials"], r["observed_trial_to_paid_rate"]), (1, 1, 1)
        )
        self.assertEqual(r["revenue_by_currency"]["USD"]["net_minor"], 599)
        self.assertEqual(r["channel_funnel"][self.channel["id"]]["first_charge"], 1)
        saved_subject = json.loads(
            self.store.one("SELECT payload FROM conversion_events")["payload"]
        )["subject_key"]
        self.assertNotEqual(saved_subject, "user1")
        self.assertEqual(len(saved_subject), 64)
        # A trial after the acquisition window can still mature in the same download cohort.
        self.now += 3 * 86400
        self.assertEqual(self.report()["mature_trials"], 2)

    def test_events_idempotent_conflicting_import_rolls_back_and_cross_source_double_count_rejected(
        self,
    ):
        e = self.event("first_download")
        self.assertEqual(self.growth.import_events(self.goal["id"], [e])["imported"], 1)
        self.assertEqual(self.growth.import_events(self.goal["id"], [e])["duplicates"], 1)
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"],
                [
                    self.event("first_download", user="other"),
                    e.model_copy(update={"country": "GB"}),
                ],
            )
        self.assertEqual(self.report()["event_funnel"]["first_download"], 1)
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"],
                [e.model_copy(update={"source": "other-source", "event_id": "new"})],
            )

    def test_concurrent_duplicate_events_are_counted_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: self.growth.import_events(
                        self.goal["id"], [self.event("first_download")]
                    ),
                    range(4),
                )
            )
        self.assertEqual(sum(r["imported"] for r in results), 1)

    def test_future_foreign_channel_and_refund_overflow_rejected(self):
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"], [self.event("first_download", at=self.now + 1)]
            )
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"], [self.event("first_download", channel_id="other")]
            )
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"],
                [self.event("refund", transaction_id="missing", amount_minor=5, currency="USD")],
            )
        self.growth.import_events(
            self.goal["id"],
            [self.event("first_charge", transaction_id="tx", amount_minor=20, currency="JPY")],
        )
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"],
                [
                    self.event(
                        "refund",
                        at=self.now - 1,
                        transaction_id="tx",
                        amount_minor=21,
                        currency="JPY",
                    )
                ],
            )
        with self.assertRaises(Conflict):
            self.growth.import_events(
                self.goal["id"],
                [
                    self.event(
                        "refund",
                        at=self.now - 1,
                        transaction_id="tx",
                        amount_minor=2,
                        currency="USD",
                    )
                ],
            )

    def test_store_aggregates_are_separate_revisions_not_additive_and_unknown_not_zero(self):
        self.growth.import_events(self.goal["id"], [self.event("first_download")])
        m = StoreMetric(
            channel_id=self.channel["id"],
            day="2026-10-13",
            source="ASC",
            evidence_reference="export-13",
            first_downloads=9,
        )
        self.growth.import_metrics(self.goal["id"], [m])
        self.growth.import_metrics(self.goal["id"], [m.model_copy(update={"first_downloads": 12})])
        r = self.report()
        self.assertEqual(
            (r["store_metrics"]["first_downloads"], r["event_funnel"]["first_download"]), (12, 1)
        )
        self.assertIsNone(r["store_metrics"]["product_page_views"])
        self.assertEqual(r["progress"]["store_downloads"], 0.12)

    def test_cohort_excludes_old_downloads_and_country_unknown_and_pre_download_events(self):
        p = self.goal["payload"] | {"countries": ["US"]}
        self.growth.save_goal(GrowthGoal(**p), self.goal["id"])
        self.growth.import_events(
            self.goal["id"],
            [
                self.event("first_download", country=""),
                self.event("first_download", user="old", at=self.now - 8 * 86400),
                self.event(
                    "first_charge",
                    user="old",
                    at=self.now - 1,
                    transaction_id="oldtx",
                    currency="USD",
                    amount_minor=100,
                ),
                self.event("first_download", user="right", at=self.now - 100),
                self.event("first_generation", user="right", at=self.now - 200),
            ],
        )
        r = self.report()
        self.assertEqual(r["event_funnel"]["first_download"], 1)
        self.assertEqual(r["event_funnel"]["first_generation"], 0)
        self.assertEqual(r["event_funnel"]["first_charge"], 0)
        self.assertEqual(r["excluded_country_events"], 1)

    def test_real_provider_token_required_and_duplicate_channel_markers_rejected(self):
        draft = self.growth.add_channel(
            self.goal["id"], GrowthChannel(name="Draft", campaign_token="draft")
        )
        self.assertIsNone(draft["url"])
        self.assertIn("pt=123456&ct=tg01&mt=8", self.channel["url"])
        with self.assertRaises(Conflict):
            self.growth.add_channel(
                self.goal["id"],
                GrowthChannel(name="Again", campaign_token="tg01", provider_token="123456"),
            )
        with self.assertRaises(ValidationError):
            GrowthGoal(
                **(
                    self.goal["payload"]
                    | {"app_store_url": "https://apps.apple.com.evil/app/id123"}
                )
            )
        with self.assertRaises(Conflict):
            self.growth.save_goal(
                GrowthGoal(**(self.goal["payload"] | {"mode": "simulation"})), self.goal["id"]
            )

    def test_disabled_feeds_and_repeat_reads_preserve_reviewed_permission_and_draft(self):
        a = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Read test",
                backend_label="read-test",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(a["id"])
        feed = self.growth.save_feed(FeedInput(account_id=a["id"], target="@read_test"))
        self.assertEqual(self.growth.poll_feeds()["completed"], 0)
        self.growth.save_feed(
            FeedInput(account_id=a["id"], target="@read_test", enabled=True), feed["id"]
        )
        self.assertEqual(self.growth.poll_feeds()["completed"], 1)
        row = Campaigns(self.engine).content()["items"][0]
        p = OpportunityInput(
            **(
                row["payload"]
                | {
                    "permission": "allowed",
                    "permission_evidence": "group-rule",
                    "draft_text": "Reviewed exact draft",
                }
            )
        )
        self.growth.review_content(row["id"], p, row["revision"])
        self.now += 1801
        self.assertEqual(self.growth.poll_feeds()["completed"], 1)
        saved = Campaigns(self.engine).content()["items"][0]
        self.assertEqual(saved["payload"]["draft_text"], "Reviewed exact draft")
        self.assertEqual(saved["payload"]["permission"], "allowed")
        with self.assertRaises(Conflict):
            self.growth.review_content(row["id"], p, row["revision"])
        with self.assertRaises(Conflict):
            self.growth.review_content(
                row["id"], p.model_copy(update={"target": "@other_group"}), saved["revision"]
            )

    def test_reply_inbox_sync_followup_is_draft_only_and_completion_needs_verified_matching_reply(
        self,
    ):
        a = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Synthetic live record",
                backend_label="synthetic-live",
                mode="live",
            )
        )
        policy = {
            "starts_at": iso(self.now - 60),
            "ends_at": iso(self.now + 86400),
            "target_allowlist": ["@test_group"],
        }
        task = self.engine.create_task(
            TaskInput(name="Origin", account_ids=[a["id"]], policy=policy)
        )
        # Synthetic platform evidence, never call a real transport.
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,0,?,?,?,'confirmed',?,?)",
                (
                    "origin",
                    task["id"],
                    a["id"],
                    canonical({"target": "@test_group", "text": "Original", "action": "reply"}),
                    "test-hash",
                    self.now - 100,
                    "https://t.me/test_group/1",
                    canonical({"verified": True}),
                ),
            )
        Statistics(self.store, clock=lambda: self.now).record_feedback(
            "origin", "123:900", self.now - 10
        )
        first = self.growth.inbox()
        self.assertEqual(first["total"], 1)
        self.assertEqual(self.growth.inbox()["total"], 1)
        reply = first["items"][0]
        with self.assertRaises(Conflict):
            self.growth.update_reply(reply["id"], InboxUpdate(state="responded"))
        following = self.growth.create_followup(
            reply["id"], TaskInput(name="Follow up", account_ids=[a["id"]], policy=policy)
        )
        self.assertEqual(following["approvals"], [])
        self.assertEqual(following["items"], [])
        self.assertEqual(following["reply_context"]["reply_to"], 900)
        self.assertEqual(
            self.growth.create_followup(
                reply["id"], TaskInput(name="Retry", account_ids=[a["id"]], policy=policy)
            )["id"],
            following["id"],
        )
        with self.assertRaises(Conflict):
            self.growth.update_reply(
                reply["id"], InboxUpdate(state="responded", followup_task_id=following["id"])
            )
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,0,?,?,?,'confirmed',?,?)",
                (
                    "follow",
                    following["id"],
                    a["id"],
                    canonical(
                        {
                            "target": "@test_group",
                            "reply_to": 900,
                            "text": "Reply",
                            "action": "reply",
                        }
                    ),
                    "test-hash",
                    self.now,
                    "https://t.me/test_group/2",
                    canonical({"verified": True}),
                ),
            )
        self.growth.update_reply(
            reply["id"], InboxUpdate(state="responded", followup_task_id=following["id"])
        )
        self.assertEqual(self.growth.inbox()["counts"]["responded"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM approvals")["n"], 0)

    def test_existing_database_schema_upgrade_is_idempotent(self):
        Store(self.store.path)
        self.assertEqual(len(self.growth.goals()), 1)

    def test_global_store_extract_does_not_double_count_territory_extract(self):
        self.growth.import_metrics(
            self.goal["id"],
            [
                StoreMetric(
                    channel_id=self.channel["id"],
                    day="2026-10-13",
                    source="ASC",
                    evidence_reference="global",
                    first_downloads=10,
                ),
                StoreMetric(
                    channel_id=self.channel["id"],
                    day="2026-10-13",
                    country="US",
                    source="ASC",
                    evidence_reference="US",
                    first_downloads=4,
                ),
            ],
        )
        self.assertEqual(self.report()["store_metrics"]["first_downloads"], 10)
        self.growth.save_goal(
            GrowthGoal(**(self.goal["payload"] | {"countries": ["US"]})), self.goal["id"]
        )
        self.assertEqual(self.report()["store_metrics"]["first_downloads"], 4)

    def test_interaction_targets_use_verified_publications_from_linked_campaign_only(self):
        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Simulated KPI",
                backend_label="sim-kpi",
                mode="simulation",
                min_interval=0,
            )
        )
        pool = Pools(self.engine).save(PoolInput(name="KPI pool", account_ids=[account["id"]]))
        campaigns = Campaigns(self.engine)
        imported = campaigns.import_content(
            [
                OpportunityInput(
                    platform="telegram", external_id="101", target="@kpi_group", reply_to=101
                )
            ]
        )
        policy = {
            "starts_at": iso(self.now - 60),
            "ends_at": iso(self.now + 86400),
            "min_interval": 0,
        }
        campaign = campaigns.create(
            CampaignInput(
                name="KPI campaign",
                pool_id=pool["id"],
                opportunity_ids=imported["ids"],
                policy=policy,
            )
        )
        with self.assertRaises(Conflict):
            self.growth.save_goal(
                GrowthGoal(**(self.goal["payload"] | {"campaign_ids": [campaign["id"]]}))
            )
        goal = self.growth.save_goal(
            GrowthGoal(
                **(
                    self.goal["payload"]
                    | {
                        "mode": "simulation",
                        "campaign_ids": [campaign["id"]],
                        "daily_interactions": 2,
                    }
                )
            )
        )
        task = self.engine.create_task(
            TaskInput(name="KPI batch", account_ids=[account["id"]], policy=policy)
        )
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET campaign_id=? WHERE id=?", (campaign["id"], task["id"]))
            for n, verified in enumerate((True, False)):
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,?,?,?,?,'confirmed',?,?)",
                    (
                        f"kpi-{n}",
                        task["id"],
                        account["id"],
                        n,
                        "{}",
                        "test",
                        self.now - 10,
                        "simulation://kpi",
                        canonical({"verified": verified}),
                    ),
                )
                db.execute(
                    "INSERT INTO publication_evidence VALUES(?,?,?,?)",
                    (f"kpi-{n}", self.now - 10, self.now, "test"),
                )
        report = self.growth.report(goal["id"])
        self.assertEqual(
            report["interactions"],
            {"confirmed_in_window": 1, "confirmed_today": 1, "daily_target_progress": 0.5},
        )


class GrowthAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            db_path=Path(self.tmp.name) / "api.db",
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="growth-test-key",
        )

    def call(self, path, method="GET", body=None, auth=True):
        return asyncio.run(
            request(
                self.app,
                path,
                method,
                body,
                {"authorization": "Bearer growth-test-key"} if auth else {},
            )
        )

    def test_new_reads_and_imports_require_auth_and_pipeline_returns_unknown_until_imported(self):
        for route in (
            "/api/growth/goals",
            "/api/growth/readiness",
            "/api/growth/inbox",
            "/api/growth/feeds",
        ):
            self.assertEqual(self.call(route, auth=False)[0], 401)
        now = datetime.now(timezone.utc).timestamp()
        status, goal, _ = self.call(
            "/api/growth/goals",
            "POST",
            {
                "name": "API goal",
                "app_store_url": "https://apps.apple.com/app/id123456",
                "starts_at": iso(now - 86400),
                "ends_at": iso(now + 86400),
            },
        )
        self.assertEqual(status, 201)
        status, report, _ = self.call("/api/growth/goals/" + goal["id"])
        self.assertEqual(status, 200)
        self.assertIsNone(report["store_metrics"]["first_downloads"])
        self.assertEqual(
            self.call("/api/growth/goals/" + goal["id"] + "/events", "POST", {"items": []})[0], 422
        )
        self.assertEqual(
            self.call(
                "/api/growth/goals/" + goal["id"] + "/events", "POST", {"items": []}, auth=False
            )[0],
            401,
        )
        self.assertTrue(self.call("/api/overview")[1]["global_stop"])


if __name__ == "__main__":
    unittest.main()
