"""No browser or real account access; exercise session identity and ownership rules."""

import unittest
from unittest.mock import patch

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.tabbit import call


class TabbitConnectionTests(unittest.TestCase):
    settings = {"identity": "brand", "tabbit_instance": "0123456789ABCDEF"}
    inventory = {
        "task": {"reused": False},
        "truncated": False,
        "tabs": [{"state": "available", "tabId": 17, "url": "https://x.com/brand"}],
    }

    def response(self, handle="brand"):
        return {
            "status": "succeeded",
            "result": {"value": {"handles": [handle], "profile": "/" + handle}},
        }

    def test_verified_session_releases_and_retains_user_tab(self):
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, self.response(), {"ok": True}],
        ) as cli:
            result = call("x", self.settings, "health")
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["account_identity"], "brand")
        self.assertEqual(result["permissions"], [])
        self.assertIn("17", cli.call_args_list[1].args[0])
        self.assertEqual(cli.call_args_list[-1].args[0], ["finish", "--task", "Verify X brand"])

    def test_changed_identity_fails_closed(self):
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, self.response("other"), {"ok": True}],
        ):
            result = call("x", self.settings, "health")
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["account_identity"])

    def test_profile_must_match_account_menu(self):
        response = self.response()
        response["result"]["value"]["profile"] = "/other"
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, response, {"ok": True}],
        ):
            self.assertFalse(call("x", self.settings, "health")["identity_verified"])

    def test_pending_receipt_is_preserved(self):
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, {"status": "running"}, {"status": "running"}],
        ) as cli:
            with self.assertRaisesRegex(BackendError, "TabbitCheckPending"):
                call("x", self.settings, "health")
        self.assertEqual(cli.call_count, 3)
        self.assertEqual(cli.call_args.args[0][0], "receipt")

    def test_pending_receipt_can_complete(self):
        response = self.response()
        response["result"]["type"] = "inline"
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, {"status": "running"}, response, {"ok": True}],
        ):
            self.assertTrue(call("x", self.settings, "health")["healthy"])

    def test_existing_unsettled_task_is_not_replaced(self):
        with patch(
            "auto_promotion.backends.tabbit.cli", return_value={"task": {"reused": True}}
        ) as cli:
            with self.assertRaisesRegex(BackendError, "TabbitCheckPending"):
                call("x", self.settings, "health")
        self.assertEqual(cli.call_count, 1)

    def test_interactions_never_reach_browser(self):
        with patch("auto_promotion.backends.tabbit.cli") as cli:
            self.assertFalse(call("x", self.settings, "preflight")["allowed"])
            for operation in ("execute", "discover", "join"):
                with self.assertRaisesRegex(BackendError, "TabbitReadOnlyConnection"):
                    call("x", self.settings, operation)
        cli.assert_not_called()

    def test_opted_in_reply_prepares_reauthorizes_submits_and_verifies_once(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}
        receipt = {
            "status": "unknown",
            "external_reference": "https://x.com/brand/status/789",
            "target_verified": True,
        }
        verified = {
            **receipt,
            "status": "confirmed",
            "verified": True,
            "published_at": "2026-10-15T06:00:00+00:00",
        }
        calls = []

        def checkpoint(value):
            calls.append(value)

        def response(value):
            return {"status": "succeeded", "result": {"value": value}}

        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[
                self.inventory,
                response({"prepared": True}),
                response(receipt),
                response(verified),
                {"ok": True},
            ],
        ) as cli:
            result = call(
                "x",
                settings,
                "execute",
                {
                    "item_id": "one",
                    "payload": {
                        "action": "reply",
                        "target": "https://x.com/target/status/123",
                        "text": "exact",
                        "media": [],
                    },
                },
                checkpoint,
                lambda: calls.append("authorized"),
            )
        self.assertEqual(calls[0], "authorized")
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["verified"])
        self.assertIsInstance(result["published_at"], (float, int))
        self.assertEqual(cli.call_count, 5)

    def test_revoked_authorization_never_submits_prepared_reply(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}

        def stopped():
            raise BackendError("ExecutionStopped")

        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[
                self.inventory,
                {"status": "succeeded", "result": {"value": {"prepared": True}}},
                {"ok": True},
            ],
        ) as cli:
            with self.assertRaisesRegex(BackendError, "ExecutionStopped"):
                call(
                    "x",
                    settings,
                    "execute",
                    {
                        "item_id": "one",
                        "payload": {
                            "action": "reply",
                            "target": "https://x.com/target/status/123",
                            "text": "exact",
                            "media": [],
                        },
                    },
                    lambda _: None,
                    stopped,
                )
        self.assertEqual(cli.call_count, 3)

    def test_uncertain_submit_preserves_task_and_never_verifies_or_replays(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[
                self.inventory,
                {"status": "succeeded", "result": {"value": {"prepared": True}}},
                {"status": "running"},
                {"status": "running"},
            ],
        ) as cli:
            with self.assertRaisesRegex(BackendError, "TabbitCheckPending"):
                call(
                    "x",
                    settings,
                    "execute",
                    {
                        "item_id": "one",
                        "payload": {
                            "action": "reply",
                            "target": "https://x.com/target/status/123",
                            "text": "exact",
                            "media": [],
                        },
                    },
                    lambda _: None,
                    lambda: None,
                )
        self.assertEqual(cli.call_count, 4)
        self.assertEqual(cli.call_args.args[0][0], "receipt")

    def test_owned_original_with_png_is_prepared_authorized_and_verified_once(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}
        receipt = {
            "status": "unknown",
            "external_reference": "https://x.com/brand/status/789",
            "target_verified": True,
        }
        verified = {
            **receipt,
            "status": "confirmed",
            "verified": True,
            "published_at": "2026-10-20T12:00:00Z",
        }
        events = []

        def respond(value):
            return {"status": "succeeded", "result": {"value": value}}

        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[
                self.inventory,
                respond({"prepared": True}),
                respond(receipt),
                respond(verified),
                {"ok": True},
            ],
        ) as cli:
            result = call(
                "x",
                settings,
                "execute",
                {
                    "item_id": "post-one",
                    "payload": {
                        "action": "post",
                        "target_owned": True,
                        "target": "https://x.com/brand",
                        "text": "exact post",
                        "media": [{"path": "/synthetic/approved.png", "mime_type": "image/png"}],
                    },
                },
                lambda value: events.append(value),
                lambda: events.append("authorized"),
            )
        self.assertTrue(result["verified"])
        self.assertEqual(events[0], "authorized")
        self.assertEqual(cli.call_count, 5)
        self.assertIn("setInputFiles", cli.call_args_list[1].kwargs["code"])
        self.assertIn("X post post-one", cli.call_args_list[0].args[0])

    def test_original_cannot_target_another_account(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}
        with patch("auto_promotion.backends.tabbit.cli") as cli:
            with self.assertRaisesRegex(BackendError, "XOwnedTargetRequired"):
                call(
                    "x",
                    settings,
                    "execute",
                    {
                        "item_id": "post-one",
                        "payload": {
                            "action": "post",
                            "target_owned": True,
                            "target": "https://x.com/other",
                            "text": "exact",
                            "media": [],
                        },
                    },
                    lambda _: None,
                    lambda: None,
                )
        cli.assert_not_called()

    def test_settled_prepare_failure_is_reconciled_without_browser_or_retry(self):
        settings = {**self.settings, "automatic_actions": ["reply"]}
        with patch("auto_promotion.backends.tabbit.cli") as cli:
            result = call(
                "x",
                settings,
                "verify",
                {
                    "item_id": "one",
                    "payload": {
                        "action": "reply",
                        "target": "https://x.com/target/status/123",
                        "text": "exact",
                        "media": [],
                    },
                    "result": {
                        "reason": "TabbitReplyUnavailable",
                        "execution_step": "reply_composer",
                        "submission_started": False,
                    },
                },
                lambda _: None,
                lambda: None,
            )
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["verified"])
        cli.assert_not_called()

    def test_high_frequency_discover_observes_without_submitting(self):
        response = {
            "status": "succeeded",
            "result": {
                "value": {
                    "url": "https://x.com/search?q=AI%20video&f=live",
                    "posts": [
                        {
                            "id": "123456",
                            "author_id": "@someone",
                            "datetime": "2026-10-22T09:00:00Z",
                            "text": "Need an AI video tool",
                            "query_visible_text": "Need an AI video tool",
                        }
                    ],
                }
            },
        }
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[self.inventory, response, {"ok": True}],
        ) as cli:
            result = call(
                "x",
                self.settings,
                "discover",
                {
                    "high_frequency": True,
                    "read_only": True,
                    "query": "AI video",
                    "lower_at": 0,
                    "upper_at": 9999999999,
                    "cursor": None,
                },
            )
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["candidates"][0]["url"], "https://x.com/i/web/status/123456")
        program = cli.call_args_list[1].kwargs["code"]
        self.assertIn("await identity();", program)
        self.assertIn("primaryColumn", program)
        self.assertNotIn("button.click()", program)
        self.assertNotIn("CreateTweet", program)
        self.assertEqual(cli.call_args_list[-1].args[0][0], "finish")

    def test_high_frequency_discover_can_open_a_new_tab_when_inventory_has_no_x_tab(self):
        inventory = {**self.inventory, "tabs": []}
        response = {
            "status": "succeeded",
            "result": {"value": {"url": "https://x.com/search", "posts": []}},
        }
        with patch(
            "auto_promotion.backends.tabbit.cli",
            side_effect=[inventory, response, {"ok": True}],
        ) as cli:
            result = call(
                "x",
                self.settings,
                "discover",
                {
                    "high_frequency": True,
                    "read_only": True,
                    "query": "Telegram",
                    "lower_at": 0,
                    "upper_at": 9999999999,
                },
            )
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("--tab", cli.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
