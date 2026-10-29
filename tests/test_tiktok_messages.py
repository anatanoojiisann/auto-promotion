import asyncio
import unittest

import test_tiktok_browser as browser_tests

from auto_promotion.alert_summary import collect_summary, summary_notice
from auto_promotion.core import Conflict
from auto_promotion.models import ItemInput, OpportunityInput, Policy, TaskInput
from auto_promotion.tiktok_messages import validate_dm_item


class TikTokMessageTests(unittest.TestCase):
    setUp = browser_tests.TikTokBrowserTests.setUp
    identity = browser_tests.TikTokBrowserTests.identity

    def snapshot(self):
        return {
            **{k: v for k, v in self.identity().items() if k != "profile_url"},
            "source_url": "https://www.tiktok.com/messages?scene=business",
            "inbox_verified": True,
            "coverage": "partial",
            "conversations": [
                {
                    "conversation_id": "0:1:9:10",
                    "peer_username": "peer_one",
                    "peer_verified": True,
                    "source_url": "https://www.tiktok.com/messages?scene=business",
                    "messages": [
                        {
                            "message_id": "100",
                            "id_verified": True,
                            "direction": "inbound",
                            "sender_username": "peer_one",
                            "text": "A private question",
                            "occurred_at": self.now - 100,
                            "timestamp_verified": True,
                        },
                        {
                            "message_id": "200",
                            "id_verified": True,
                            "direction": "outbound",
                            "sender_username": "web_test",
                            "text": "A historical answer",
                        },
                    ],
                }
            ],
        }

    def enable(self):
        self.browser.messages.collect(self.aid, self.snapshot())
        self.browser.messages.enable(self.aid)
        self.browser.observe(self.aid, self.identity())

    def ready(self, reply=False, start=True):
        self.enable()
        task = self.engine.create_task(
            TaskInput(
                name="Explicit private task",
                account_ids=[self.aid],
                policy=Policy(
                    starts_at=browser_tests.iso(self.now - 60),
                    ends_at=browser_tests.iso(self.now + 1800),
                    execution_channel="local",
                ),
            )
        )
        value = ItemInput(
            account_id=self.aid,
            action="reply_direct_message" if reply else "send_direct_message",
            target="https://www.tiktok.com/@peer_one",
            recipient_id="peer_one",
            text="A precise private answer",
            scheduled_at=browser_tests.iso(self.now - 1),
            tiktok_conversation_id="0:1:9:10",
            tiktok_message_id="100" if reply else "",
        )
        self.engine.set_items(task["id"], [value])
        if start:
            self.engine.start_task(task["id"])
        return self.engine.task(task["id"]), self.engine.task(task["id"])["items"][0]

    def proof(self, item):
        return {
            **self.identity(),
            "payload_hash": item["payload_hash"],
            "target": item["payload"]["target"],
            "recipient_id": "peer_one",
            "recipient_verified": True,
            "conversation_id": "0:1:9:10",
            "conversation_verified": True,
            "source_url": "https://www.tiktok.com/messages?scene=business",
            "context_verified": True,
            "can_reply": True,
            "can_message": True,
            "no_duplicate": True,
            "source_quote": "Private conversation with verified peer",
            "editor_text": item["payload"]["text"],
            "before_message_ids": ["100", "200"],
            "parent_message_id": item["payload"]["tiktok_message_id"],
        }

    def receipt(self, item):
        return {
            "status": "confirmed",
            "verified": True,
            "identity": "web_test",
            "recipient_id": "peer_one",
            "recipient_verified": True,
            "conversation_id": "0:1:9:10",
            "message_id": "300",
            "message_id_verified": True,
            "observed_at": self.now,
            "published_at": None,
            "direction": "outbound",
            "observed_text": item["payload"]["text"],
            "external_reference": "https://www.tiktok.com/messages?scene=business",
            "parent_target": item["payload"]["target"],
            "parent_message_id": item["payload"]["tiktok_message_id"],
            "author_verified": True,
            "parent_verified": True,
            "independent_read": True,
            "evidence": "Independent DOM reread: new native outgoing message ID matched exact body and bound recipient",
        }

    def test_collection_deduplicates_without_sending_or_creating_shared_opportunities(self):
        self.browser.messages.collect(self.aid, self.snapshot())
        self.browser.messages.collect(self.aid, self.snapshot())
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_messages")["n"], 2)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM opportunities")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM items")["n"], 0)
        self.assertEqual(
            self.browser.messages.inbox(self.aid)["conversations"][0]["message_count"], 2
        )
        audit = " ".join(r["details"] for r in self.store.rows("SELECT details FROM audit"))
        self.assertNotIn("A private question", audit)

    def test_collection_rejects_stale_identity_wrong_instance_sender_and_host_atomically(self):
        for changed in (
            {"identity": "other"},
            {"instance_id": "other"},
            {"observed_at": self.now - 301},
            {"source_url": "https://evil.example/messages"},
            {"inbox_verified": False},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.browser.messages.collect(self.aid, {**self.snapshot(), **changed})
        data = self.snapshot()
        data["conversations"][0]["messages"][0]["sender_username"] = "web_test"
        with self.assertRaises(ValueError):
            self.browser.messages.collect(self.aid, data)
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_messages")["n"], 0)

    def test_private_tasks_require_collection_and_capability_but_do_not_enable_writer(self):
        with self.assertRaises(ValueError):
            self.browser.messages.enable(self.aid)
        self.enable()
        self.assertIn("send_direct_message", self.engine.account(self.aid)["capabilities"])
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.browser.messages.enable(self.aid)
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )

    def test_native_messages_cannot_move_between_conversations(self):
        self.enable()
        changed = self.snapshot()
        changed["conversations"][0]["conversation_id"] = "0:1:9:11"
        with self.assertRaises(ValueError):
            self.browser.messages.collect(self.aid, changed)
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_conversations")["n"], 1)

    def test_current_account_outbound_blocks_remain_effective_for_private_messages(self):
        from auto_promotion.account_blocks import observe

        _, item = self.ready()
        with self.store.connect(write=True) as db:
            observe(
                db,
                self.aid,
                {
                    "account_block_reason": "account_guard_unavailable",
                    "confirmed_not_submitted": True,
                },
                now=self.now,
                operation="execute",
            )
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_dm_task_uses_normal_queue_once_and_preserves_native_receipt(self):
        task, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        result = self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(result["message_id"], "300")
        self.assertEqual(result["interaction_channel"], "direct_message")
        self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(self.engine.task(task["id"])["summary"], {"confirmed": 1})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM tiktok_dm_sent_receipts")["n"], 1)
        self.assertEqual(self.browser.pending()["feedback_jobs"], [])
        with self.assertRaises(ValueError):
            self.browser.feedback(item["id"], {})

    def test_dm_reply_requires_verified_incoming_native_parent(self):
        _, item = self.ready(reply=True)
        with self.assertRaises(ValueError):
            self.browser.begin(item["id"], {**self.proof(item), "parent_message_id": "200"})
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], self.receipt(item), owner)

    def test_wrong_peer_conversation_editor_or_permission_prevents_claim(self):
        _, item = self.ready()
        for changed in (
            {"recipient_id": "other"},
            {"conversation_id": "0:1:1:2"},
            {"can_message": False},
            {"editor_text": "changed"},
            {"authenticated_menu_verified": False},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.browser.begin(item["id"], {**self.proof(item), **changed})
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_draft_stop_and_once_only_guard_protect_private_sending(self):
        task, item = self.ready(start=False)
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.engine.start_task(task["id"])
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        with self.assertRaises(Exception):
            self.browser.guard(item["id"], owner, self.proof(item))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
        self.browser.guard(item["id"], owner, self.proof(item))
        with self.assertRaises(Conflict):
            self.browser.guard(item["id"], owner, self.proof(item))

    def test_old_or_inbound_wrong_peer_receipts_cannot_be_counted(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        for changed in (
            {"message_id": "200"},
            {"direction": "inbound"},
            {"recipient_id": "other"},
            {"conversation_id": "0:1:3:4"},
            {"message_id_verified": False},
            {"independent_read": False},
        ):
            with self.subTest(changed=changed), self.assertRaises(Conflict):
                self.browser.result(item["id"], {**self.receipt(item), **changed}, owner)
        self.assertEqual(self.store.one("SELECT count(*) n FROM publication_evidence")["n"], 0)

    def test_unknown_private_send_is_nonretryable_and_reconciles_original_attempt(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], {"status": "unknown"}, owner)
        with self.assertRaises(Conflict):
            self.browser.begin(item["id"], self.proof(item))
        self.now += 61
        unresolved = self.browser.pending()["unresolved"][0]
        self.assertEqual(unresolved["private_attempt"]["before_message_ids"], ["100", "200"])
        self.browser.result(item["id"], self.receipt(item), owner)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_historical_inbox_outbound_is_not_new_interaction_and_source_can_report_zero(self):
        self.enable()
        with self.store.connect() as db:
            summary = collect_summary(db, self.now + 1)
        tiktok = next(p for p in summary["platforms"] if p["platform"] == "tiktok")
        self.assertEqual(tiktok["direct_message_interactions"], 0)
        self.assertEqual(tiktok["sent"], 0)
        self.assertIn("私信互动 0 条", summary_notice(summary))

    def test_confirmed_private_send_enters_broadcast_but_never_private_body(self):
        _, item = self.ready()
        owner = self.browser.begin(item["id"], self.proof(item))["owner"]
        self.browser.guard(item["id"], owner, self.proof(item))
        self.browser.result(item["id"], self.receipt(item), owner)
        with self.store.connect() as db:
            summary = collect_summary(db, self.now + 1)
        text = summary_notice(summary)
        self.assertIn("TikTok：发送 1 条｜成功 1 条｜失败 0 条｜私信互动 1 条", text)
        self.assertNotIn(item["payload"]["text"], text)

    def test_private_content_cannot_enter_shared_pool_and_invalid_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            OpportunityInput(
                platform="tiktok",
                external_id="private",
                target="https://www.tiktok.com/@peer_one",
                action="send_direct_message",
            )
        _, item = self.ready()
        for changed in (
            {"recipient_id": "someone"},
            {"tiktok_video_id": "123"},
            {"text": "x" * 1001},
            {"target": "https://evil.example/@peer_one"},
            {"tiktok_message_id": "100"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_dm_item({**item["payload"], **changed})

    def test_private_api_requires_authentication_and_returns_owned_account_conversations(self):
        from test_api import request

        from auto_promotion.adapters import AdapterRegistry
        from auto_promotion.api import create_app

        self.enable()
        app = create_app(
            db_path=self.store.path, registry=AdapterRegistry(self.root), token="private-test"
        )
        path = f"/api/tiktok-browser/accounts/{self.aid}/dm/inbox"
        status, _, _ = asyncio.run(request(app, path))
        self.assertEqual(status, 401)
        status, body, _ = asyncio.run(
            request(app, path, headers={"authorization": "Bearer private-test"})
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["conversations"]), 1)


if __name__ == "__main__":
    unittest.main()
