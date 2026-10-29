import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.alert_summary import collect_summary
from auto_promotion.cloud_control import CloudControl
from auto_promotion.core import Conflict, Engine
from auto_promotion.meta_browser import MetaBrowser, event_key
from auto_promotion.meta_migration import register
from auto_promotion.models import ItemInput, Policy, TaskInput
from auto_promotion.statistics import Statistics
from auto_promotion.store import Store


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class MetaBrowserTests(unittest.TestCase):
    def setUp(self):
        runtime = Path.cwd() / "tmp" / "meta-browser-tests"
        runtime.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=runtime)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ,
            {
                "AP_ACCOUNTS_DIR": str(self.root / "accounts"),
                "AUTO_PROMOTION_LIVE_WRITES": "1",
                "AP_FACEBOOK_OWNER": "auto-promotion",
                "AP_INSTAGRAM_OWNER": "auto-promotion",
            },
        ).start()
        patch.object(CloudControl, "configured", return_value=None).start()
        self.store = Store(self.root / "test.db")
        self.engine = Engine(self.store, AdapterRegistry(self.root), live_enabled=True)
        self.browser = MetaBrowser(self.engine)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
            db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")
        self.source = self.root / "old.json"
        self.write_source({"status": "stopped_by_user", "items": []})
        self.registrations = [
            {
                "platform": "facebook",
                "account_id": "facebook:123456789",
                "label": "Existing Facebook",
                "details": {
                    "connector": "chrome_browser_use",
                    "profile_url": "https://www.facebook.com/profile.php?id=123456789",
                    "workflow_paths": [str(self.source)],
                    "scope": "Existing Facebook",
                },
            }
        ]
        self.aid = register(self.browser, self.registrations)[0]

    def write_source(self, data):
        self.source.write_text(json.dumps(data), encoding="utf-8")

    def identity(self):
        c = self.browser.connection(self.aid)
        return {
            "identity": c["identity"],
            "profile_url": c["profile_url"],
            "browser": c["browser"],
            "identity_verified": True,
            "observed_at": time.time(),
            "evidence": "Authenticated account menu and profile matched",
        }

    def ready(self, target="https://www.facebook.com/groups/example/posts/7654321/"):
        self.browser.observe(self.aid, self.identity())
        now = time.time()
        task = self.engine.create_task(
            TaskInput(
                name="Explicitly started Meta task",
                account_ids=[self.aid],
                policy=Policy(
                    starts_at=iso(now - 60), ends_at=iso(now + 900), execution_channel="local"
                ),
            )
        )
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.aid,
                    action="comment",
                    target=target,
                    text="A precise helpful answer",
                    scheduled_at=iso(now - 1),
                )
            ],
        )
        self.engine.start_task(task["id"])
        task = self.engine.task(task["id"])
        self.assertEqual(task["phase"], "ready")
        item = task["items"][0]
        return task, item

    def proof(self, item):
        return {
            **self.identity(),
            "payload_hash": item["payload_hash"],
            "target": item["payload"]["target"],
            "context_verified": True,
            "can_reply": True,
            "no_duplicate": True,
            "source_quote": "An unresolved question at the specified target",
        }

    def receipt(self, item):
        return {
            "status": "confirmed",
            "verified": True,
            "identity": "123456789",
            "observed_at": time.time(),
            "published_at": time.time() - 0.1,
            "external_reference": item["payload"]["target"] + "?comment_id=90001",
            "parent_target": item["payload"]["target"],
            "observed_text": item["payload"]["text"],
            "author_verified": True,
            "parent_verified": True,
            "evidence": "Independent public comment view matched author, text, parent and timestamp",
        }

    def test_explicit_task_uses_normal_queue_one_attempt_and_statistics(self):
        task, item = self.ready()
        pending = self.browser.pending()
        self.assertEqual([j["item_id"] for j in pending["jobs"]], [item["id"]])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        begun = self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertTrue(self.browser.guard(item["id"], begun["owner"])["allowed"])
        with self.assertRaises(Conflict):
            self.browser.guard(item["id"], begun["owner"])
        receipt = self.receipt(item)
        self.browser.result(item["id"], receipt, begun["owner"])
        self.browser.result(item["id"], receipt, begun["owner"])
        current = self.engine.task(task["id"])
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["summary"], {"confirmed": 1})
        stats = Statistics(self.store).snapshot(since=time.time() - 60, until=time.time())
        facebook = next(p for p in stats["platforms"] if p["platform"] == "facebook")
        self.assertEqual(facebook["published_success"], 1)
        with self.store.connect() as db:
            summary = collect_summary(db, time.time() + 1)
        self.assertEqual(
            next(p for p in summary["platforms"] if p["platform"] == "facebook")["success"], 1
        )
        self.assertIsNone(facebook["received_replies"])
        self.browser.feedback(
            item["id"],
            {
                "identity": "123456789",
                "external_reference": receipt["external_reference"],
                "observed_at": time.time(),
                "evidence": "Only part of the thread was read",
                "coverage": "partial",
                "events": [],
            },
        )
        self.assertEqual(self.store.one("SELECT status FROM feedback_checks")["status"], "partial")

    def test_global_stop_between_begin_and_click_blocks_submission(self):
        _, item = self.ready()
        begun = self.browser.begin(item["id"], self.proof(item))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        with self.assertRaises(Exception):
            self.browser.guard(item["id"], begun["owner"])
        self.assertEqual(
            self.store.one("SELECT submission_started FROM meta_browser_claims")[
                "submission_started"
            ],
            0,
        )
        self.browser.result(
            item["id"], {"status": "failed_known", "confirmed_not_submitted": True}, begun["owner"]
        )
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "failed_known",
        )

    def test_unknown_outcome_and_expired_claim_are_never_replayed(self):
        _, item = self.ready()
        begun = self.browser.begin(item["id"], self.proof(item))
        self.browser.guard(item["id"], begun["owner"])
        self.browser.result(item["id"], {"status": "unknown"}, begun["owner"])
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.browser.pending()["jobs"], [])
        self.browser.result(item["id"], self.receipt(item), begun["owner"])
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "confirmed",
        )

    def test_archive_preserves_stop_unknown_and_dedupes_without_attempts(self):
        self.write_source(
            {
                "status": "stopped_by_user",
                "created_at": iso(time.time() - 86400),
                "items": [
                    {
                        "id": "L03",
                        "status": "unknown",
                        "url": "https://www.facebook.com/groups/g/posts/101/",
                        "draft": "An uncertain old comment",
                    },
                    {
                        "id": "L04",
                        "status": "cancelled",
                        "url": "https://www.facebook.com/groups/g/posts/102/",
                        "draft": "Never submitted",
                    },
                ],
            }
        )
        self.browser.sync()
        self.browser.sync()
        self.assertEqual(self.store.one("SELECT count(*) n FROM items")["n"], 2)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM execution_jobs")["n"], 0)
        source = self.store.one("SELECT * FROM meta_browser_sources")
        task = self.engine.task(source["task_id"])
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(task["legacy_source"]["details"]["legacy_status"], "stopped_by_user")
        self.assertEqual(task["summary"], {"unknown": 1, "cancelled": 1})
        with self.assertRaises(Conflict):
            self.engine.start_task(task["id"])
        with self.assertRaises(Conflict):
            self.engine.change_state(task["id"], "active")
        self.assertEqual(self.browser.pending()["jobs"], [])

    def test_busy_sources_and_cross_account_duplicates_block_new_attempt(self):
        _, item = self.ready()
        self.write_source({"status": "observing_replies", "items": []})
        self.browser.sync()
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.write_source(
            {
                "status": "completed",
                "items": [
                    {
                        "id": "old",
                        "status": "sent",
                        "url": item["payload"]["target"],
                        "comment_url": item["payload"]["target"] + "?comment_id=91",
                        "draft": "Old accepted comment",
                        "sent_at": iso(time.time() - 8000),
                    }
                ],
            }
        )
        self.browser.sync()
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_bad_receipt_does_not_create_success_or_zero_reply_rate(self):
        _, item = self.ready()
        begun = self.browser.begin(item["id"], self.proof(item))
        self.browser.guard(item["id"], begun["owner"])
        receipt = self.receipt(item)
        receipt["observed_text"] = "A different comment"
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], receipt, begun["owner"])
        receipt = self.receipt(item)
        receipt["external_reference"] = (
            "https://www.facebook.com/groups/other/posts/999/?comment_id=11"
        )
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], receipt, begun["owner"])
        self.assertIsNone(
            self.store.one("SELECT * FROM publication_evidence WHERE item_id=?", (item["id"],))
        )

    def test_archive_missing_file_blocks_then_recovers_without_resending(self):
        self.source.unlink()
        self.browser.sync()
        self.assertEqual(
            self.store.one("SELECT state FROM meta_browser_sources")["state"], "unknown"
        )
        self.write_source({"status": "stopped_by_user", "items": []})
        self.browser.sync()
        self.assertEqual(
            self.store.one("SELECT state FROM meta_browser_sources")["state"], "stopped"
        )

    def add_shared(self):
        shared = self.root / "shared-state.json"
        shared.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "account": "Existing Facebook",
                    "items": [],
                    "original_field": "keep this",
                }
            ),
            encoding="utf-8",
        )
        with self.store.connect(write=True) as db:
            details = json.loads(
                db.execute(
                    "SELECT details FROM meta_browser_accounts WHERE account_id=?", (self.aid,)
                ).fetchone()[0]
            )
            details["coordination_paths"] = [str(shared)]
            db.execute(
                "UPDATE meta_browser_accounts SET details=? WHERE account_id=?",
                (json.dumps(details), self.aid),
            )
        return shared

    def test_shared_lock_cooldown_and_unknown_marker_are_preserved(self):
        shared = self.add_shared()
        _, item = self.ready()
        begun = self.browser.begin(item["id"], self.proof(item))
        lock = self.root / "interaction.lock"
        self.assertTrue(lock.is_file())
        marker = json.loads(shared.read_text())["coordination_inflight"]
        self.assertEqual(marker["item_id"], item["id"])
        self.browser.guard(item["id"], begun["owner"])
        self.browser.result(item["id"], {"status": "unknown"}, begun["owner"])
        self.assertFalse(lock.exists())
        self.assertEqual(
            json.loads(shared.read_text())["coordination_inflight"]["status"], "unknown"
        )
        self.browser.result(item["id"], self.receipt(item), begun["owner"])
        current = json.loads(shared.read_text())
        self.assertEqual(current["original_field"], "keep this")
        self.assertEqual(current["status"], "completed")
        self.assertIsNone(current["coordination_inflight"])
        self.assertEqual(current["coordination_last_outbound"]["item_id"], item["id"])
        self.assertTrue(Path(current["coordination_last_outbound"]["state_path"]).is_file())

    def test_existing_shared_lock_leaves_new_job_unattempted(self):
        self.add_shared()
        _, item = self.ready()
        lock = self.root / "interaction.lock"
        lock.write_text('{"owner":"legacy-flow","expires_at":9999999999}', encoding="utf-8")
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(json.loads(lock.read_text())["owner"], "legacy-flow")

    def test_workbench_api_exposes_meta_and_never_starts_a_draft(self):
        from fastapi.testclient import TestClient

        from auto_promotion.api import create_app

        app = create_app(
            db_path=self.store.path,
            registry=self.engine.adapters,
            token="meta-test",
            worker=False,
            report_worker=False,
        )
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer meta-test"
            response = client.get("/api/meta-browser/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["accounts"][0]["account_id"], self.aid)
            invalid = client.post(f"/api/meta-browser/accounts/{self.aid}/observe", json={})
            self.assertEqual(invalid.status_code, 400)
            response = client.post(
                f"/api/meta-browser/accounts/{self.aid}/observe", json=self.identity()
            )
            self.assertEqual(response.status_code, 200)
            services = client.get("/api/services").json()
            self.assertIn("facebook", {p["platform"] for p in services})
            self.assertEqual(client.get("/api/meta-browser/jobs").json()["jobs"], [])
            self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_pending_cloud_release_cannot_execute_or_disrupt_other_platform_sync(self):
        from auto_promotion.cloud_control import export_snapshot

        _, item = self.ready()
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_control_required','1')")
            db.execute(
                "INSERT INTO meta VALUES('meta_browser_cloud_catalog','pending_user_approval')"
            )
        self.assertTrue(self.browser.status()["cloud_catalog_pending"])
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        snapshot = export_snapshot(self.engine)
        self.assertEqual(snapshot["accounts"], [])
        self.assertEqual(snapshot["tasks"], [])
        self.assertEqual(snapshot["jobs"], [])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_mixed_windows_paths_do_not_misidentify_a_workflow_as_shared_coordination(self):
        from auto_promotion.meta_coordination import LegacyCoordination
        from auto_promotion.meta_migration import import_existing

        workspace = self.root / "g-p-fixture"
        workflow = workspace / "interaction-plans" / "today" / "execution-state.json"
        shared = workspace / "outputs" / "shared" / "state.json"
        workflow.parent.mkdir(parents=True)
        shared.parent.mkdir(parents=True)
        shared.write_text('{"status":"completed","items":[]}', encoding="utf-8")
        workflow.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "items": [],
                    "linked_account_state_paths": [
                        "interaction-plans/today/execution-state.json",
                        "outputs/shared/state.json",
                    ],
                }
            ),
            encoding="utf-8",
        )
        policy = self.root / "completion-cycle-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "external_accounts": [
                        {
                            "platform": "instagram",
                            "account_id": "instagram:existing_ig",
                            "label": "Existing IG",
                            "details": {
                                "connector": "edge_browser_use",
                                "scope": "Existing IG",
                                "profile_url": "https://www.instagram.com/existing_ig/",
                                "workflow_paths": [workflow.as_posix()],
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        aid = import_existing(self.browser)["account_ids"][0]
        connection = self.browser.connection(aid)
        self.assertEqual(connection["details"]["coordination_paths"], [str(shared.resolve())])
        self.assertEqual(LegacyCoordination(connection).lock, shared.parent / "interaction.lock")

    def test_canonical_targets_preserve_ig_shortcode_case_and_facebook_post_identity(self):
        self.assertEqual(
            event_key("instagram", "https://www.instagram.com/p/Ab-C_1/c/100/"),
            event_key("instagram", "https://www.instagram.com/author/reel/Ab-C_1/"),
        )
        self.assertNotEqual(
            event_key("instagram", "https://www.instagram.com/p/ABC/"),
            event_key("instagram", "https://www.instagram.com/p/abc/"),
        )
        self.assertEqual(
            event_key("facebook", "https://www.facebook.com/groups/name/posts/42/?comment_id=2"),
            event_key("facebook", "https://m.facebook.com/groups/22/permalink/42/"),
        )


if __name__ == "__main__":
    unittest.main()
