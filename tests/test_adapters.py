import asyncio
import contextlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_promotion import provider_bridge as bridge


class AdapterContractTests(unittest.TestCase):
    def telegram(self, responses, operation, payload):
        calls = []

        class Session:
            def __init__(self, *args):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                pass

            async def call_tool(self, name, args):
                import json

                calls.append((name, args))
                value = responses[name]
                text = value if isinstance(value, str) else json.dumps(value)
                return types.SimpleNamespace(
                    isError=False, content=[types.SimpleNamespace(text=text)]
                )

        @contextlib.asynccontextmanager
        async def stream(url):
            yield ("read", "write", None)

        modules = {
            "mcp": types.SimpleNamespace(ClientSession=Session),
            "mcp.client": types.ModuleType("mcp.client"),
            "mcp.client.streamable_http": types.SimpleNamespace(streamablehttp_client=stream),
        }
        with (
            patch.dict(sys.modules, modules),
            patch.dict(
                os.environ,
                {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": "auto-promotion"},
            ),
        ):
            result = asyncio.run(bridge.telegram(operation, "test-account", payload))
        self.assertTrue(all(c[1]["account"] == "test-account" for c in calls))
        return result, calls

    def item(self, action="send_message"):
        return {
            "item_id": "item-1",
            "task_id": "AP-TEST",
            "payload": {
                "action": action,
                "target": "@group",
                "text": "exact",
                "scheduled_at": "2026-09-08T12:00:00+08:00",
                "media": [],
                "reply_to": None,
            },
        }

    def test_telegram_structured_message_receipt(self):
        result, calls = self.telegram(
            {"send_message": {"sent": True, "message_id": 42, "verified": True}},
            "execute",
            self.item(),
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(calls[0][1]["verify_receipt"])
        self.assertEqual(result["external_reference"], "telegram:@group:42")

    def test_telegram_schedule_independent_lookup(self):
        r, calls = self.telegram(
            {
                "send_scheduled_message": {
                    "scheduled": True,
                    "active": True,
                    "verified": True,
                    "message_id": 42,
                },
                "get_scheduled_messages": "ID: 42 | Scheduled: date | Text: exact",
            },
            "execute",
            self.item("schedule_message"),
        )
        self.assertEqual(
            [c[0] for c in calls], ["send_scheduled_message", "get_scheduled_messages"]
        )
        self.assertEqual(r["lookup_message_ids"], ["42"])

    def test_old_telegram_response_is_not_success(self):
        r, _ = self.telegram({"send_message": "Message sent successfully."}, "execute", self.item())
        self.assertEqual(r["status"], "unknown")

    def test_telegram_reconcile_only_reads(self):
        p = self.item()
        p.update(result={"message_ids": [42]}, external_reference="telegram:@group:42")
        r, calls = self.telegram({"verify_sent_message": {"verified": True}}, "verify", p)
        self.assertEqual(calls[0][0], "verify_sent_message")
        self.assertTrue(r["verified"])

    def test_telegram_pending_join_preserved(self):
        r, _ = self.telegram(
            {"join_public_promotion_group": {"status": "pending_admin_approval", "verified": True}},
            "join",
            {"target": "@group", "min_interval": 120},
        )
        self.assertEqual(r["status"], "pending_admin_approval")

    def test_reddit_calls_original_rate_limit_and_writer(self):
        calls = []
        browser = types.SimpleNamespace(
            publish_post=lambda *args: (
                calls.append(("post", args))
                or {"verified": True, "externalReference": "https://reddit.com/r/test/comments/1"}
            ),
            publish_reply=lambda *args: None,
        )
        rate = types.SimpleNamespace(
            reserve_reddit_action_slot=lambda account: calls.append(("slot", account))
        )
        with (
            patch.dict(sys.modules, {"reddit_browser": browser, "rate_limit": rate}),
            patch.dict(
                os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_REDDIT_OWNER": "auto-promotion"}
            ),
        ):
            r = bridge.reddit(
                "execute",
                "operator",
                {
                    "payload": {
                        "action": "post",
                        "target": "test",
                        "title": "Title",
                        "text": "Exact",
                    }
                },
                Path("/tmp"),
            )
        self.assertEqual(calls, [("slot", "operator"), ("post", ("test", "Title", "Exact"))])
        self.assertEqual(r["status"], "confirmed")

    def test_live_writer_gate(self):
        with patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "0"}):
            with self.assertRaises(RuntimeError):
                bridge.assert_writer("x")

    def test_no_fabricated_scheduled_ids(self):
        self.assertEqual(bridge.scheduled_ids("No scheduled messages in chat 123."), [])
        self.assertEqual(
            bridge.scheduled_ids({"messages": [{"message_id": 9}, {"id": 10}]}), [9, 10]
        )


if __name__ == "__main__":
    unittest.main()
