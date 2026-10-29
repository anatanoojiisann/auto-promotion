import importlib.util
import json
import time
import unittest
from pathlib import Path

import test_meta_browser as fixtures

from auto_promotion.core import Conflict
from auto_promotion.meta_browser import claim_blocker
from auto_promotion.meta_skip import skip_unknown


class MetaModerationPendingTests(unittest.TestCase):
    setUp = fixtures.MetaBrowserTests.setUp
    write_source = fixtures.MetaBrowserTests.write_source
    identity = fixtures.MetaBrowserTests.identity
    ready = fixtures.MetaBrowserTests.ready
    proof = fixtures.MetaBrowserTests.proof
    receipt = fixtures.MetaBrowserTests.receipt

    def uncertain(self):
        task, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner)
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        return task, item, owner

    def pending_receipt(self, item):
        return {
            "status": "pending_admin_approval",
            "verified": False,
            "identity": "123456789",
            "identity_verified": True,
            "author_verified": True,
            "parent_verified": True,
            "independent_view": True,
            "parent_target": item["payload"]["target"],
            "observed_url": item["payload"]["target"],
            "observed_text": item["payload"]["text"],
            "moderation_label": "待审核",
            "observed_at": time.time(),
            "evidence": "Independent author, text, parent and pending label",
        }

    def blocker(self, item, target="https://www.facebook.com/groups/example/posts/88888/"):
        account = self.engine.account(self.aid)
        payload = {**item["payload"], "target": target}
        with self.store.connect() as db:
            return claim_blocker(db, account, payload, "another-item", time.time())

    def test_reviewed_pending_releases_only_account_gate_and_can_later_confirm(self):
        task, item, owner = self.uncertain()
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")
        before = self.store.one(
            "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
        )
        account_before = self.engine.account(self.aid)
        result = self.browser.result(item["id"], self.pending_receipt(item), owner)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["verified"])
        self.assertEqual(result["platform_status"], "pending_admin_approval")
        self.assertIsNone(self.blocker(item))
        self.assertEqual(self.blocker(item, item["payload"]["target"]), "meta_duplicate_target")
        self.assertEqual(
            before,
            self.store.one(
                "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
            ),
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)
        for key in (
            "next_at",
            "cooldown_until",
            "daily_limit",
            "hourly_limit",
            "min_interval",
            "disabled",
        ):
            self.assertEqual(account_before[key], self.engine.account(self.aid)[key])
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        with self.assertRaises(Conflict):
            self.browser.guard(item["id"], owner)
        # A later inconclusive read must not discard the previously verified pending state.
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        self.assertIsNone(self.blocker(item))
        self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "confirmed",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_incomplete_or_forged_review_evidence_does_not_release_gate(self):
        _, item, owner = self.uncertain()
        guarded = json.loads(
            self.store.one("SELECT result FROM meta_browser_claims WHERE item_id=?", (item["id"],))[
                "result"
            ]
        )["submission_guarded_at"]
        invalid = [
            ("identity", "another"),
            ("identity_verified", False),
            ("author_verified", False),
            ("parent_verified", False),
            ("independent_view", False),
            ("observed_text", "different"),
            ("parent_target", "https://www.facebook.com/groups/example/posts/999/"),
            ("observed_url", "https://www.facebook.com/groups/example/posts/999/"),
            ("moderation_label", "posted"),
            ("verified", True),
            ("evidence", ""),
            ("observed_at", time.time() - 301),
            ("observed_at", guarded - 0.001),
            ("external_reference", "https://example.com/c/1"),
        ]
        for key, value in invalid:
            with self.subTest(key=key):
                proof = self.pending_receipt(item)
                proof[key] = value
                with self.assertRaises(Conflict):
                    self.browser.result(item["id"], proof, owner)
                self.assertEqual(self.blocker(item), "meta_outcome_unknown")
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], self.pending_receipt(item), "wrong-owner")
        # Merely changing the item result label is not validated moderation evidence.
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET result=? WHERE id=?",
                (
                    json.dumps({"status": "unknown", "platform_status": "pending_admin_approval"}),
                    item["id"],
                ),
            )
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")

    def test_one_valid_pending_does_not_hide_a_different_unknown(self):
        task, item, owner = self.uncertain()
        self.browser.result(item["id"], self.pending_receipt(item), owner)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,attempt_id,claimed_at,external_reference,result) "
                "SELECT ?,task_id,account_id,position+1,payload,payload_hash,due_at,'unknown',NULL,NULL,NULL,'{}' FROM items WHERE id=?",
                ("another-unknown", item["id"]),
            )
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")
        for status in ("executing", "scheduled"):
            with self.subTest(status=status), self.store.connect(write=True) as db:
                db.execute("UPDATE items SET status=? WHERE id=?", (status, "another-unknown"))
            self.assertEqual(self.blocker(item), "meta_outcome_unknown")

    def test_pending_receipt_is_available_through_the_existing_mcp_interface(self):
        from auto_promotion.mcp_server import create_server

        _, item, owner = self.uncertain()
        server = create_server(self.engine)
        tool = server._tool_manager.get_tool("meta_browser_result")
        result = tool.fn(item_id=item["id"], owner=owner, receipt=self.pending_receipt(item))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["platform_status"], "pending_admin_approval")
        self.assertIsNone(self.blocker(item))

    def test_review_label_cannot_be_recorded_before_the_one_use_guard(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], self.pending_receipt(item), owner)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (item["id"],))["status"],
            "executing",
        )
        self.assertEqual(
            self.store.one(
                "SELECT submission_started FROM meta_browser_claims WHERE item_id=?", (item["id"],)
            )["submission_started"],
            0,
        )

    def test_legacy_claim_without_guard_timestamp_remains_blocked(self):
        _, item, owner = self.uncertain()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta_browser_claims SET result='{}' WHERE item_id=?", (item["id"],))
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], self.pending_receipt(item), owner)
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")

    def test_parent_comment_or_temporary_id_is_not_a_confirmed_reply(self):
        target = "https://www.facebook.com/groups/example/posts/7654321/?comment_id=12345"
        _, item = self.ready(target=target)
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner)
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        receipt = self.receipt(item)
        for reference in (
            target,
            target + "&reply_comment_id=client:77",
            target + "&reply_comment_id=12345",
            "https://www.facebook.com/groups/example/posts/7654321/?comment_id=999&reply_comment_id=777",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(Conflict):
                    self.browser.result(
                        item["id"], {**receipt, "external_reference": reference}, owner
                    )
        child = target + "&reply_comment_id=67890"
        self.assertEqual(
            self.browser.result(item["id"], {**receipt, "external_reference": child}, owner)[
                "status"
            ],
            "confirmed",
        )

    def test_skipped_unknown_can_be_confirmed_later_without_a_second_attempt(self):
        task, item, owner = self.uncertain()
        skip_unknown(self.browser, item["id"], owner, "Explicit request to skip this item")
        before = self.store.one(
            "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
        )
        with self.assertRaises(Conflict):
            self.browser.result(
                item["id"], {**self.receipt(item), "observed_text": "different"}, owner
            )
        with self.assertRaises(Conflict):
            self.browser.result(item["id"], self.receipt(item), "wrong-owner")
        result = self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(
            self.browser.result(item["id"], self.receipt(item), owner)["status"], "confirmed"
        )
        self.assertEqual(
            before,
            self.store.one(
                "SELECT attempt_id,claimed_at,payload_hash FROM items WHERE id=?", (item["id"],)
            ),
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 1)
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='meta_browser.user_skipped_unknown'"
            )["n"],
            1,
        )
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='meta_browser.skipped_submission_later_verified'"
            )["n"],
            1,
        )
        self.assertEqual(
            self.store.one(
                "SELECT state FROM meta_browser_contacts WHERE item_id=?", (item["id"],)
            )["state"],
            "confirmed",
        )
        self.assertEqual(
            self.store.one(
                "SELECT state FROM execution_jobs WHERE kind='send' AND reference_id=?",
                (item["id"],),
            )["state"],
            "confirmed",
        )
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))

    def test_guarded_controller_records_independent_review_and_unblocks_other_post(self):
        spec = importlib.util.spec_from_file_location(
            "moderation_controller",
            Path("outputs/facebook-p1-execution-20261026/executor/guarded_controller.py"),
        )
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        task, item = self.ready()
        entry = {
            "task_id": task["id"],
            "item_id": item["id"],
            "payload_hash": item["payload_hash"],
            "account_id": self.aid,
            "text": item["payload"]["text"],
            "target": item["payload"]["target"],
            "source_quote": "A question",
            "source_author": "Author",
        }
        meta = self.browser
        store = self.store

        class Ops:
            observe = staticmethod(meta.observe)
            begin = staticmethod(meta.begin)
            guard = staticmethod(meta.guard)
            result = staticmethod(meta.result)

            @staticmethod
            def submission_started(item_id):
                row = store.one(
                    "SELECT submission_started FROM meta_browser_claims WHERE item_id=?", (item_id,)
                )
                return bool(row["submission_started"]) if row else None

        case = self

        class Browser:
            submissions = 0

            def run(self, phase, script):
                if phase == "preflight":
                    return {"ready": True, "syntax_validated_in_browser": True}
                if phase == "fill":
                    return {"ready": True}
                if phase == "submit":
                    self.submissions += 1
                    return {"submit_keypress_issued": True}
                if phase == "moderation-review":
                    return {"pending_found": True, "receipt": case.pending_receipt(item)}
                raise AssertionError(phase)

        browser = Browser()
        folder = self.root / "prepared"
        result = controller.execute(
            Ops(), browser, entry, self.proof(item), folder, "Reply", time.time() + 180
        )
        self.assertEqual(browser.submissions, 1)
        self.assertEqual(result["platform_status"], "pending_admin_approval")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(self.blocker(item))
        self.assertEqual(self.blocker(item, item["payload"]["target"]), "meta_duplicate_target")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)
        root = self.root / "facebook-requeue-20261027"
        (root / "executor").mkdir(parents=True)
        (root / "requeue-plan.json").write_text(
            json.dumps(
                {"tasks": [dict(entry, state="active"), {"task_id": "next", "state": "draft"}]}
            ),
            encoding="utf-8",
        )
        (root / "executor/status.json").write_text(
            json.dumps(
                {
                    "mode": "scoped_restart",
                    "allowed_outbound_item_ids": [item["id"]],
                    "tasks": [{"task_id": task["id"]}],
                    "operator_inflight": {"item_id": item["id"]},
                }
            ),
            encoding="utf-8",
        )
        controller.persist_requeue_state(root, entry, self.store)
        state = json.loads((root / "executor/status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["mode"], "authorized_waiting")
        self.assertEqual(state["allowed_outbound_item_ids"], [])
        self.assertEqual(
            state["receipt_reviews"][item["id"]]["platform_status"], "pending_admin_approval"
        )
        self.assertEqual(
            state["next_activation"]["basis_attempt_id"],
            self.store.one("SELECT attempt_id FROM items WHERE id=?", (item["id"],))["attempt_id"],
        )
        self.assertGreaterEqual(state["next_activation"]["delay_minutes"], 61)
        self.assertLessEqual(state["next_activation"]["delay_minutes"], 70)
        self.assertNotIn("operator_inflight", state)

    def test_unverified_submission_schedules_only_read_only_review(self):
        spec = importlib.util.spec_from_file_location(
            "unknown_controller",
            Path("outputs/facebook-p1-execution-20261026/executor/guarded_controller.py"),
        )
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        task, item, owner = self.uncertain()
        entry = {"task_id": task["id"], "item_id": item["id"], "payload_hash": item["payload_hash"]}
        root = self.root / "facebook-requeue-20261027"
        (root / "executor").mkdir(parents=True)
        (root / "requeue-plan.json").write_text(
            json.dumps({"tasks": [dict(entry, state="active")]}), encoding="utf-8"
        )
        (root / "executor/status.json").write_text(
            json.dumps(
                {
                    "mode": "scoped_restart",
                    "allowed_outbound_item_ids": [item["id"]],
                    "tasks": [{"task_id": task["id"]}],
                }
            ),
            encoding="utf-8",
        )
        before = time.time()
        controller.persist_requeue_state(root, entry, self.store)
        state = json.loads((root / "executor/status.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["next_activation"])
        self.assertEqual(state["receipt_reviews"][item["id"]]["platform_status"], "outcome_unknown")
        self.assertFalse(state["receipt_reviews"][item["id"]]["retry_allowed"])
        from datetime import datetime

        due = datetime.fromisoformat(
            state["receipt_reviews"][item["id"]]["next_check_at"]
        ).timestamp()
        self.assertGreaterEqual(due, before + 119)
        self.assertLessEqual(due, time.time() + 121)
        self.assertEqual(self.blocker(item), "meta_outcome_unknown")


if __name__ == "__main__":
    unittest.main()
