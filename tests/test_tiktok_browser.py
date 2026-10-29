import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.alert_summary import collect_summary
from auto_promotion.backends.common import BackendError, read_profile, save_profile
from auto_promotion.cloud_control import CloudControl
from auto_promotion.core import Conflict, Engine
from auto_promotion.meta_browser import MetaBrowser, event_key
from auto_promotion.models import AccountInput, CandidateInput, ItemInput, Policy, TaskInput, digest
from auto_promotion.routing import route_for
from auto_promotion.statistics import Statistics
from auto_promotion.store import Store
from auto_promotion.tiktok_browser import (
    TikTokBrowser,
    TikTokBrowserConnect,
    validate_browser_item,
    video_target,
)


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class TikTokBrowserTests(unittest.TestCase):
    def setUp(self):
        folder = Path.cwd() / "tmp" / "tiktok-browser-tests"
        folder.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "AP_ACCOUNTS_DIR": str(self.root / "accounts"),
                "AUTO_PROMOTION_LIVE_WRITES": "1",
                "AP_TIKTOK_OWNER": "auto-promotion",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        cloud = patch.object(CloudControl, "configured", return_value=None)
        cloud.start()
        self.addCleanup(cloud.stop)
        self.now = time.time()
        self.store = Store(self.root / "test.db")
        self.engine = Engine(
            self.store, AdapterRegistry(self.root), clock=lambda: self.now, live_enabled=True
        )
        self.browser = TikTokBrowser(self.engine)
        self.value = TikTokBrowserConnect(
            label="TikTok test",
            backend_label="web-test",
            username="web_test",
            browser="tabbit",
            instance_id="ABC123",
        )
        self.aid = self.browser.connect(self.value)["account"]["id"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
            db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")

    def identity(self):
        return {
            "identity": "web_test",
            "profile_url": "https://www.tiktok.com/@web_test",
            "browser": "tabbit",
            "instance_id": "ABC123",
            "identity_verified": True,
            "authenticated_menu_verified": True,
            "observed_at": self.now,
            "evidence": "Current signed-in Profile navigation and profile heading match",
        }

    def test_id_only_registered_desktop_preparation_and_revocation_before_guard(self):
        self.browser.observe(self.aid, self.identity())
        task = self.engine.create_task(
            TaskInput(
                name="Registered desktop ID entry",
                account_ids=[self.aid],
                policy=Policy(
                    starts_at=iso(self.now - 60),
                    ends_at=iso(self.now + 1800),
                    execution_channel="local",
                ),
            )
        )
        self.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=self.aid,
                target="https://www.tiktok.com/@creator/video/123456789",
                is_public=True,
                language="en",
                confidence=1,
            ),
        )
        saved = self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.aid,
                    action="comment",
                    text="A precise helpful answer",
                    tiktok_video_id="123456789",
                    scheduled_at=iso(self.now - 1),
                )
            ],
        )
        item = saved["items"][0]
        self.assertTrue(item["payload"]["tiktok_target_evidence"])
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        from auto_promotion.tiktok import TikTokInteractionInput, TikTokManager

        checked = TikTokManager(self.engine).check_interaction(
            self.aid,
            TikTokInteractionInput(action="comment", video_id="123456789", task_id=task["id"]),
        )
        self.assertEqual(checked["state"], "needs_desktop_preflight")
        self.assertEqual(checked["capability_scope"], "registered_desktop_video")
        self.assertFalse(checked["execution_ready"])
        self.engine.start_task(task["id"])
        proof = self.proof(item)
        owner = self.browser.begin(item["id"], proof)["owner"]
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE candidates SET decision='rejected' WHERE id=?",
                (item["payload"]["tiktok_target_evidence"]["source_id"],),
            )
        with self.assertRaisesRegex(BackendError, "TikTokVideoEvidenceRevoked"):
            self.browser.guard(item["id"], owner, proof)
        self.assertEqual(
            self.store.one("SELECT submission_started FROM meta_browser_claims")[
                "submission_started"
            ],
            0,
        )

    def test_legacy_registered_route_and_capabilities_do_not_require_candidate_flag(self):
        account = self.engine.account(self.aid)
        route = route_for(account)
        self.assertEqual(route.pool, "tiktok.desktop")
        self.assertEqual(account["tiktok_transport"], "desktop")
        self.assertFalse(account.get("tiktok_candidate_disabled", False))
        self.assertEqual(route.resource, "desktop:tabbit:" + digest("ABC123")[:24])

    def ready(self, *, reply=False, start=True, video="123456789"):
        self.browser.observe(self.aid, self.identity())
        task = self.engine.create_task(
            TaskInput(
                name="TikTok desktop test",
                account_ids=[self.aid],
                policy=Policy(
                    starts_at=iso(self.now - 60),
                    ends_at=iso(self.now + 1800),
                    execution_channel="local",
                ),
            )
        )
        target = "https://www.tiktok.com/@creator/video/" + video
        if reply:
            target += "?comment_id=123456700"
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.aid,
                    action="reply" if reply else "comment",
                    target=target,
                    text="A precise helpful answer",
                    tiktok_video_id=video,
                    tiktok_comment_id="123456700" if reply else "",
                    scheduled_at=iso(self.now - 1),
                    target_owned=False,
                )
            ],
        )
        if start:
            self.engine.start_task(task["id"])
        return self.engine.task(task["id"]), self.engine.task(task["id"])["items"][0]

    def proof(self, item):
        p = item["payload"]
        return {
            **self.identity(),
            "payload_hash": item["payload_hash"],
            "target": p["target"],
            "video_id": p["tiktok_video_id"],
            "parent_comment_id": p["tiktok_comment_id"],
            "parent_id_verified": True,
            "parent_author_profile": "/@question_author",
            "editor_text": p["text"],
            "context_verified": True,
            "can_reply": True,
            "no_duplicate": True,
            "source_quote": "A public question at this video",
        }

    def receipt(self, item):
        p = item["payload"]
        return {
            "status": "confirmed",
            "verified": True,
            "identity": "web_test",
            "observed_at": self.now,
            "published_at": self.now - 0.1,
            "comment_id": "987654321",
            "video_id": p["tiktok_video_id"],
            "parent_comment_id": p["tiktok_comment_id"],
            "external_reference": video_target(p["target"])["url"] + "?comment_id=987654321",
            "parent_target": p["target"],
            "observed_text": p["text"],
            "author_verified": True,
            "parent_verified": True,
            "independent_read": True,
            "evidence": "Reloaded video thread: author, complete text, comment ID and parent matched",
        }

    def test_enrollment_is_idempotent_and_does_not_start_or_enable(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET disabled=1,cooldown_until=?,daily_limit=2 WHERE id=?",
                (self.now + 500, self.aid),
            )
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.assertFalse(self.browser.connect(self.value)["execution_started"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM accounts")["n"], 1)
        a = self.engine.account(self.aid)
        self.assertEqual(a["daily_limit"], 2)
        self.assertTrue(a["disabled"])
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(a["execution_backend"], "desktop")
        self.assertEqual(route_for(a).pool, "tiktok.desktop")

    def test_registration_cannot_replace_api_profile_or_duplicate_browser_identity(self):
        api = {"identity": "api-open-id", "access_token": "test-only-secret"}
        save_profile("tiktok", "api-existing", api)
        with self.assertRaises(Conflict):
            self.browser.connect(self.value.model_copy(update={"backend_label": "api-existing"}))
        self.assertEqual(read_profile("tiktok", "api-existing"), {**api, "label": "api-existing"})
        with self.assertRaises(Conflict):
            self.browser.connect(self.value.model_copy(update={"backend_label": "duplicate"}))
        with self.assertRaises(Conflict):
            self.browser.connect(self.value.model_copy(update={"browser": "chrome"}))

    def test_profile_only_or_stale_or_wrong_instance_never_verifies_login(self):
        for changed in (
            {"authenticated_menu_verified": False},
            {"observed_at": self.now - 301},
            {"instance_id": "other"},
            {"identity": "someone_else"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.browser.observe(self.aid, {**self.identity(), **changed})
        self.assertFalse(self.engine.account(self.aid)["writable"])

    def test_blocked_page_invalidates_prior_identity(self):
        self.browser.observe(self.aid, self.identity())
        self.assertTrue(self.engine.account(self.aid)["writable"])
        self.browser.observe(
            self.aid,
            {
                "blocked_reason": "BrowserVerificationRequired",
                "observed_at": self.now,
                "instance_id": "ABC123",
            },
        )
        self.assertFalse(self.engine.account(self.aid)["writable"])
        self.assertFalse(self.browser.status()["accounts"][0]["identity_observation_fresh"])

    def test_comment_and_reply_share_queue_attempts_receipts_and_statistics(self):
        task, item = self.ready(reply=True)
        self.assertEqual([j["item_id"] for j in self.browser.pending()["jobs"]], [item["id"]])
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], self.receipt(item), owner)
        self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(self.engine.task(task["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        snapshot = Statistics(self.store, clock=lambda: self.now).snapshot(
            since=self.now - 60, until=self.now + 1
        )
        row = next(p for p in snapshot["platforms"] if p["platform"] == "tiktok")
        self.assertEqual(row["published_success"], 1)
        self.assertIsNone(row["received_replies"])
        with self.store.connect() as db:
            summary = collect_summary(db, self.now + 1)
        self.assertEqual(
            next(p for p in summary["platforms"] if p["platform"] == "tiktok")["success"], 1
        )

    def test_draft_cannot_be_claimed(self):
        _, item = self.ready(start=False)
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_missing_platform_time_is_preserved_without_guessing(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], {**self.receipt(item), "published_at": None}, owner)
        result = Statistics(self.store, clock=lambda: self.now).snapshot(
            since=self.now - 60, until=self.now + 1
        )
        row = next(p for p in result["platforms"] if p["platform"] == "tiktok")
        self.assertEqual(row["confirmed_without_publication_time"], 1)
        self.assertIsNone(
            self.store.one("SELECT published_at FROM publication_evidence")["published_at"]
        )

    def test_pause_and_global_stop_before_click_prevent_submit(self):
        task, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        with self.assertRaises(Exception):
            self.browser.guard(item["id"], owner, self.proof(item))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        self.engine.change_state(task["id"], "paused")
        with self.assertRaises(Exception):
            self.browser.guard(item["id"], owner, self.proof(item))
        self.assertEqual(
            self.store.one("SELECT submission_started FROM meta_browser_claims")[
                "submission_started"
            ],
            0,
        )

    def test_guard_needs_fresh_editor_parent_and_one_use(self):
        _, item = self.ready(reply=True)
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        for changed in (
            {"editor_text": "changed"},
            {"parent_comment_id": "5"},
            {"observed_at": self.now - 301},
            {"instance_id": "other"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.browser.guard(item["id"], owner, {**self.proof(item), **changed})
        self.browser.guard(item["id"], owner, self.proof(item))
        with self.assertRaises(Conflict):
            self.browser.guard(item["id"], owner, self.proof(item))

    def test_mismatched_receipt_cannot_be_counted(self):
        _, item = self.ready(reply=True)
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        for changed in (
            {"parent_comment_id": "111"},
            {"video_id": "9"},
            {"observed_text": "different"},
            {"identity": "other"},
            {"independent_read": False},
            {"comment_id": "123456700"},
            {"external_reference": "https://www.tiktok.com/@creator/video/123456789"},
        ):
            with self.subTest(changed=changed), self.assertRaises(Conflict):
                self.browser.result(item["id"], {**self.receipt(item), **changed}, owner)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)

    def test_unknown_cannot_be_resent_or_downgraded_to_known_failure(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        with self.assertRaises(Conflict):
            self.browser.result(
                item["id"], {"status": "failed_known", "confirmed_not_submitted": True}, owner
            )
        self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_same_video_tracking_links_deduplicate_across_tasks(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        _, second = self.ready()
        with self.assertRaises(Conflict):
            self.browser.begin(second["id"], self.proof(second))
        self.assertEqual(
            event_key("tiktok", item["payload"]["target"]),
            event_key("tiktok", item["payload"]["target"] + "?is_from_webapp=1"),
        )

    def test_meta_endpoints_cannot_bypass_tiktok_validation(self):
        _, item = self.ready()
        meta = MetaBrowser(self.engine)
        self.assertEqual(meta.status()["accounts"], [])
        self.assertEqual(meta.pending()["jobs"], [])
        with self.assertRaises(ValueError):
            meta.begin(item["id"], self.proof(item))

    def test_reply_without_parent_id_evidence_cannot_be_claimed(self):
        _, item = self.ready(reply=True)
        with self.assertRaises(ValueError):
            self.browser.begin(item["id"], {**self.proof(item), "parent_id_verified": False})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_public_opportunity_uses_registered_browser_and_preserves_permission_checks(self):
        from auto_promotion.models import OpportunityInput
        from auto_promotion.planner import execution_mode

        opportunity = OpportunityInput(
            platform="tiktok",
            external_id="source-comment",
            target="https://www.tiktok.com/@creator/video/123?comment_id=456",
            action="reply",
            tiktok_video_id="123",
            tiktok_comment_id="456",
            permission="allowed",
            permission_evidence="Public video comment thread allows replies",
        )
        account = self.engine.account(self.aid)
        self.assertEqual(execution_mode(account, opportunity.model_dump())[0], "automatic")
        self.assertEqual(
            execution_mode(account, {**opportunity.model_dump(), "permission": "denied"})[0],
            "disabled",
        )
        self.assertEqual(
            execution_mode(account, {**opportunity.model_dump(), "is_public": False})[0], "disabled"
        )

    def test_no_background_adapter_executes_desktop_submission(self):
        _, item = self.ready()
        a = self.engine.account(self.aid)
        self.assertFalse(
            self.engine.adapters.call(a, "preflight", {"payload": item["payload"]})["allowed"]
        )
        with self.assertRaises(BackendError):
            self.engine.adapters.call(a, "execute", {})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_api_owned_video_boundary_is_unchanged(self):
        from auto_promotion.backends.tiktok import validate_item

        _, item = self.ready()
        with self.assertRaises(BackendError):
            validate_item(item["payload"])
        api = self.engine.add_account(
            AccountInput(platform="tiktok", label="API", backend_label="api")
        )
        self.assertEqual(route_for(api, profile={}).pool, "tiktok.organic")

    def test_invalid_urls_and_unsupported_actions_are_rejected(self):
        for target in (
            "http://www.tiktok.com/@x/video/1",
            "https://www.tiktok.com.evil/@x/video/1",
            "https://user@www.tiktok.com/@x/video/1",
            "https://www.tiktok.com/@x/video/1#c",
            "https://vm.tiktok.com/short/",
            "https://www.tiktok.com/@x/video/1?comment_id=1&comment_id=2",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                video_target(target)
        _, item = self.ready()
        for changed in (
            {"action": "like"},
            {"tiktok_video_id": "7"},
            {"text": "a" * 151},
            {"tiktok_comment_id": "3"},
            {"execution_mode": "manual"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_browser_item({**item["payload"], **changed})

    def test_api_routes_enroll_without_enabling_and_validate_missing_proof(self):
        from test_api import request

        from auto_promotion.api import create_app

        app = create_app(
            db_path=self.store.path, registry=AdapterRegistry(self.root), token="test-token"
        )
        status, body, _ = asyncio.run(
            request(
                app, "/api/tiktok-browser/status", headers={"authorization": "Bearer test-token"}
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"][0]["account_id"], self.aid)
        status, body, _ = asyncio.run(
            request(
                app,
                "/api/tiktok-browser/connect",
                "POST",
                self.value.model_dump(),
                {"authorization": "Bearer test-token"},
            )
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["execution_started"])
        status, _, _ = asyncio.run(
            request(
                app,
                "/api/tiktok-browser/items/missing/guard",
                "POST",
                {"owner": "test-owner"},
                {"authorization": "Bearer test-token"},
            )
        )
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
