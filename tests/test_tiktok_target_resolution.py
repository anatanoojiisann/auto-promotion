"""ID-only entry uses local, account-scoped evidence; no real platform traffic."""

import json
import os
import unittest
from unittest.mock import patch

import test_tiktok_desktop as desktop
from test_engine import iso

from auto_promotion.backends.common import save_profile
from auto_promotion.campaigns import Campaigns
from auto_promotion.core import Conflict
from auto_promotion.models import (
    AccountInput,
    CandidateInput,
    ItemInput,
    OpportunityInput,
    TaskInput,
)


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        self.bridge = desktop.DesktopBridgeTests()
        self.bridge.setUp()
        self.addCleanup(self.bridge.doCleanups)
        self.engine = self.bridge.fixture.engine
        self.account = self.engine.add_account(
            AccountInput(platform="tiktok", label="desktop", backend_label="brand", mode="live")
        )
        self.task = self.engine.create_task(
            TaskInput(
                name="ID entry",
                account_ids=[self.account["id"]],
                policy={
                    "starts_at": iso(self.engine.clock() - 1),
                    "ends_at": iso(self.engine.clock() + 3600),
                    "min_interval": 120,
                },
            )
        )
        self.url = "https://www.tiktok.com/@target/video/123"

    def candidate(self, url=None, account=None):
        return self.engine.candidate(
            self.task["id"],
            CandidateInput(
                account_id=(account or self.account)["id"],
                target=url or self.url,
                language="en",
                confidence=1,
                is_public=True,
            ),
        )

    def item(self, **changes):
        return ItemInput(
            account_id=self.account["id"],
            action="comment",
            text="exact text",
            tiktok_video_id="123",
            scheduled_at=iso(self.engine.clock()),
            **changes,
        )

    def test_id_only_candidate_resolves_in_engine_then_actual_bridge_submits_once(self):
        self.candidate()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        payload = saved["items"][0]["payload"]
        self.assertEqual(payload["target"], self.url)
        from auto_promotion.task_tools import item_payload

        self.assertEqual(
            item_payload(payload)["tiktok_target_evidence"], payload["tiktok_target_evidence"]
        )
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])
        from auto_promotion import native_bridge

        request = {
            "platform": "tiktok",
            "account": "brand",
            "operation": "execute",
            "payload": {
                **self.bridge.payload,
                "payload": payload,
            },
        }
        with patch("auto_promotion.native_bridge.verify_claim"):
            result = native_bridge.dispatch(request)
            native_bridge.dispatch(request)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.bridge.phases, ["prepare", "submit", "verify"])

    def test_missing_id_evidence_is_blocked_then_candidate_can_be_added(self):
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(self.task["id"], [self.item()])
        self.assertEqual(self.engine.task(self.task["id"])["items"], [])
        self.candidate()
        self.assertEqual(
            self.engine.set_items(self.task["id"], [self.item()])["items"][0]["payload"]["target"],
            self.url,
        )

    def test_same_id_conflicting_authors_are_not_guessed(self):
        self.candidate()
        self.candidate("https://www.tiktok.com/@other/video/123")
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceConflict"):
            self.engine.set_items(self.task["id"], [self.item()])

    def test_other_account_or_other_task_candidate_does_not_resolve(self):
        self.candidate()
        save_profile(
            "tiktok", "other", {**self.bridge.settings, "identity": "other", "username": "other"}
        )
        other = self.engine.add_account(
            AccountInput(platform="tiktok", label="other", backend_label="other", mode="live")
        )
        task = self.engine.create_task(
            TaskInput(name="other", account_ids=[other["id"]], policy=self.task["policy"])
        )
        value = self.item().model_copy(update={"account_id": other["id"]})
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(task["id"], [value])
        task = self.engine.create_task(
            TaskInput(
                name="same account other task",
                account_ids=[self.account["id"]],
                policy=self.task["policy"],
            )
        )
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(task["id"], [self.item()])

    def opportunity(self, **changes):
        value = OpportunityInput(
            **{
                "platform": "tiktok",
                "external_id": "123",
                "action": "comment",
                "target": self.url,
                "tiktok_video_id": "123",
                "permission": "allowed",
                "permission_evidence": "fixture native observation",
                "permission_account_ids": [self.account["id"]],
                **changes,
            }
        )
        return Campaigns(self.engine).import_content([value])["ids"][0]

    def test_scoped_authorized_opportunity_resolves_without_task_candidate(self):
        self.opportunity()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.assertEqual(saved["items"][0]["payload"]["target"], self.url)

    def test_expired_or_unscoped_shared_evidence_is_not_used(self):
        identifier = self.opportunity(expires_at=iso(self.engine.clock() - 1))
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(self.task["id"], [self.item()])
        with self.engine.store.connect(write=True) as db:
            db.execute("DELETE FROM opportunities WHERE id=?", (identifier,))
        self.opportunity(permission_account_ids=[])
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(self.task["id"], [self.item()])

    def test_inconsistent_declared_video_url_is_blocked(self):
        self.opportunity(target="https://www.tiktok.com/@target/video/999")
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceInvalid"):
            self.engine.set_items(self.task["id"], [self.item()])

    def test_explicit_wrong_url_never_falls_back_to_valid_candidate(self):
        self.candidate()
        with self.assertRaisesRegex(Conflict, "TikTokVideoTargetMismatch"):
            self.engine.set_items(
                self.task["id"], [self.item(target="https://www.tiktok.com/@target/video/999")]
            )

    def revoke(self, identifier):
        with self.engine.store.connect(write=True) as db:
            value = json.loads(
                db.execute(
                    "SELECT payload FROM opportunities WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            value["permission"] = "denied"
            db.execute(
                "UPDATE opportunities SET payload=? WHERE id=?", (json.dumps(value), identifier)
            )

    def ready(self):
        guard_env = patch.dict(
            os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TIKTOK_OWNER": "auto-promotion"}
        )
        guard_env.start()
        self.addCleanup(guard_env.stop)
        self.engine.check_account(self.account["id"])
        self.engine.live_enabled = True
        self.engine.prepare(self.task["id"])
        self.engine.start_task(self.task["id"])
        self.engine.set_stop(False)

    def test_revoked_source_blocks_prepare(self):
        source = self.opportunity()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.assertEqual(
            saved["items"][0]["payload"]["tiktok_target_evidence"]["source_id"], source
        )
        self.revoke(source)
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRevoked"):
            self.engine.prepare(self.task["id"])
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])

    def test_revoked_after_start_blocks_claim_and_keeps_zero_attempts(self):
        source = self.opportunity()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.ready()
        self.revoke(source)
        self.assertIsNone(self.engine._claim(saved["items"][0]["id"], {"allowed": True}))
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(
            self.engine.task(self.task["id"])["items"][0]["result"]["skip_reason"],
            "tiktok_video_evidence_invalid",
        )

    def test_final_native_authorization_rechecks_revocation_after_claim(self):
        from auto_promotion.backends.common import BackendError, verify_claim

        source = self.opportunity()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.ready()
        claim = self.engine._claim(saved["items"][0]["id"], {"allowed": True})
        self.assertIsNotNone(claim, self.engine.task(self.task["id"])["items"][0]["result"])
        request = {
            "platform": "tiktok",
            "account": "brand",
            "account_id": self.account["id"],
            "database": self.engine.store.path,
            "operation": "execute",
            "payload": claim,
        }
        with (
            patch.dict(
                os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TIKTOK_OWNER": "auto-promotion"}
            ),
            patch("auto_promotion.backends.common.time.time", return_value=self.engine.clock()),
            patch("auto_promotion.cloud_control.CloudControl.configured", return_value=None),
        ):
            verify_claim(request)
            self.revoke(source)
            with self.assertRaisesRegex(BackendError, "TikTokVideoEvidenceRevoked"):
                verify_claim(request)
        self.assertEqual(self.bridge.phases, [])
        self.assertEqual(len(self.engine.store.rows("SELECT * FROM attempts")), 1)

    def test_opportunity_action_and_parent_cannot_be_inherited(self):
        identifier = self.opportunity()
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(
                self.task["id"],
                [self.item().model_copy(update={"action": "reply", "tiktok_comment_id": "456"})],
            )
        with self.engine.store.connect(write=True) as db:
            value = json.loads(
                db.execute(
                    "SELECT payload FROM opportunities WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            value.update(action="reply", tiktok_comment_id="999")
            db.execute(
                "UPDATE opportunities SET payload=? WHERE id=?", (json.dumps(value), identifier)
            )
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(
                self.task["id"],
                [self.item().model_copy(update={"action": "reply", "tiktok_comment_id": "456"})],
            )

    def test_frozen_target_does_not_re_resolve_or_unlock_unknown(self):
        self.candidate()
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.ready()
        before = self.engine.task(self.task["id"])
        with self.assertRaisesRegex(Conflict, "冻结"):
            self.engine.set_items(self.task["id"], [self.item()])
        after = self.engine.task(self.task["id"])
        self.assertEqual(before["manifest_hash"], after["manifest_hash"])
        self.assertEqual(before["items"], after["items"])
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown',attempt_id='already-attempted' WHERE id=?",
                (saved["items"][0]["id"],),
            )
        self.assertIsNone(self.engine._claim(saved["items"][0]["id"], {"allowed": True}))
        self.assertEqual(
            self.engine.task(self.task["id"])["items"][0]["attempt_id"], "already-attempted"
        )

    def test_rejected_or_foreign_host_candidate_never_supplies_a_target(self):
        self.engine.candidate(
            self.task["id"],
            CandidateInput(
                account_id=self.account["id"], target=self.url, language="en", confidence=0
            ),
        )
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            self.engine.set_items(self.task["id"], [self.item()])
        self.candidate("https://untrusted.example/@target/video/123")
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceInvalid"):
            self.engine.set_items(self.task["id"], [self.item()])

    def test_explicit_opportunity_selection_is_scoped_and_keeps_exact_url(self):
        from auto_promotion.tiktok_targets import resolve_video_target

        first = self.opportunity()
        self.url = "https://www.tiktok.com/@other/video/123"
        second = self.opportunity()
        selected = resolve_video_target(self.engine, self.account, "123", opportunity_id=first)
        self.assertEqual(selected["target"], "https://www.tiktok.com/@target/video/123")
        self.assertEqual(selected["target_evidence"]["source_id"], first)
        self.revoke(first)
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRequired"):
            resolve_video_target(self.engine, self.account, "123", opportunity_id=first)
        self.assertEqual(
            resolve_video_target(self.engine, self.account, "123", opportunity_id=second)["target"],
            self.url,
        )

    def test_source_expiry_after_start_blocks_claim(self):
        self.opportunity(expires_at=iso(self.engine.clock() + 1))
        saved = self.engine.set_items(self.task["id"], [self.item()])
        self.ready()
        before = self.engine.clock()
        self.engine.clock = lambda: before + 2
        self.assertIsNone(self.engine._claim(saved["items"][0]["id"], {"allowed": True}))
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])

    def test_candidate_lookup_budget_has_a_bounded_stop(self):
        from auto_promotion.models import canonical
        from auto_promotion.tiktok_targets import resolve_video_target

        self.candidate()
        with self.engine.store.connect(write=True) as db:
            for n in range(2000):
                target = f"https://www.tiktok.com/@target/video/{1000 + n}"
                db.execute(
                    "INSERT INTO candidates(id,task_id,account_id,target,payload,decision,reasons) VALUES(?,?,?,?,?,'qualified','[]')",
                    (
                        f"budget-{n}",
                        self.task["id"],
                        self.account["id"],
                        target,
                        canonical({"target": target}),
                    ),
                )
            db.execute("DELETE FROM candidates WHERE id='budget-1999'")
        self.assertEqual(
            resolve_video_target(self.engine, self.account, "123", task_id=self.task["id"])[
                "target"
            ],
            self.url,
        )
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO candidates(id,task_id,account_id,target,payload,decision,reasons) VALUES(?,?,?,?,?,'qualified','[]')",
                (
                    "budget-over",
                    self.task["id"],
                    self.account["id"],
                    "https://www.tiktok.com/@target/video/999999",
                    "{}",
                ),
            )
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceBudgetExceeded"):
            resolve_video_target(self.engine, self.account, "123", task_id=self.task["id"])

    def test_opportunity_lookup_budget_has_a_bounded_stop(self):
        from auto_promotion.models import canonical
        from auto_promotion.tiktok_targets import resolve_video_target

        identifier = self.opportunity()
        source = self.engine.store.one("SELECT * FROM opportunities WHERE id=?", (identifier,))
        with self.engine.store.connect(write=True) as db:
            for n in range(199):
                value = json.loads(source["payload"])
                value["external_id"] = f"other-{n}"
                db.execute(
                    "INSERT INTO opportunities VALUES(?,?,?,?,1,?,?)",
                    (
                        f"budget-{n}",
                        "tiktok",
                        f"key-{n}",
                        canonical(value),
                        self.engine.clock(),
                        self.engine.clock(),
                    ),
                )
        self.assertEqual(resolve_video_target(self.engine, self.account, "123")["target"], self.url)
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO opportunities VALUES(?,?,?,?,1,?,?)",
                (
                    "budget-over",
                    "tiktok",
                    "key-over",
                    source["payload"],
                    self.engine.clock(),
                    self.engine.clock(),
                ),
            )
        with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceBudgetExceeded"):
            resolve_video_target(self.engine, self.account, "123")

    def test_repeated_active_id_start_rechecks_frozen_source_without_external_work(self):
        self.candidate()
        self.engine.set_items(self.task["id"], [self.item()])
        self.ready()
        with patch.object(
            self.engine, "call", side_effect=AssertionError("No repeat preflight or send")
        ):
            duplicate = self.engine.start_and_run(self.task["id"])
            self.assertTrue(duplicate["execution_result"]["already_started"])
            self.assertEqual(duplicate["execution_result"]["processed"], [])
            with self.engine.store.connect(write=True) as db:
                db.execute(
                    "UPDATE candidates SET decision='rejected' WHERE task_id=?", (self.task["id"],)
                )
            with self.assertRaisesRegex(Conflict, "TikTokVideoEvidenceRevoked"):
                self.engine.start_and_run(self.task["id"])
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(self.bridge.phases, [])


class ResolutionMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_mcp_id_only_save_and_preparation_resolve_without_platform_calls(self):
        from importlib.util import find_spec

        if find_spec("mcp") is None:
            self.skipTest("MCP extra not installed")
        from auto_promotion.mcp_server import create_server

        fixture = TargetResolutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        engine, task, account = fixture.engine, fixture.task, fixture.account
        server = create_server(engine)

        async def call(name, **args):
            import json

            result = await server.call_tool(name, args)
            if isinstance(result, dict):
                return result
            if isinstance(result, tuple):
                return result[1]
            return json.loads(result[0].text)

        with patch.object(engine, "call", side_effect=AssertionError("No platform operation")):
            missing = await call(
                "tiktok_check_interaction_input",
                account_id=account["id"],
                action="comment",
                video_id="123",
                task_id=task["id"],
            )
            self.assertIn("TikTokVideoEvidenceRequired", missing["reason"])
            await call(
                "record_candidate",
                task_id=task["id"],
                candidate={
                    "account_id": account["id"],
                    "target": fixture.url,
                    "language": "en",
                    "confidence": 1,
                },
            )
            resolved = await call(
                "tiktok_check_interaction_input",
                account_id=account["id"],
                action="comment",
                video_id="123",
                task_id=task["id"],
            )
            self.assertEqual(resolved["resolved_target"], fixture.url)
            self.assertFalse(resolved["execution_ready"])
            saved = await call(
                "set_task_items",
                task_id=task["id"],
                items=[
                    {
                        "account_id": account["id"],
                        "action": "comment",
                        "tiktok_video_id": "123",
                        "text": "exact text",
                        "scheduled_at": iso(engine.clock()),
                    }
                ],
            )
        self.assertEqual(saved["items"][0]["payload"]["target"], fixture.url)
        self.assertEqual(engine.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(fixture.bridge.phases, [])
