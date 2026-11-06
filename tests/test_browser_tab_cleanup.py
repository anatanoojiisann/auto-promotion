"""Task tab cleanup must preserve active work and independently saved receipts."""

import unittest
from unittest.mock import patch

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.tabbit import call


class BrowserTabCleanupTests(unittest.TestCase):
    settings = {
        "identity": "brand",
        "tabbit_instance": "0123456789ABCDEF",
        "automatic_actions": ["reply"],
    }

    def verify(self, outcome, finish):
        saved = []
        commands = []

        def cli(arguments, instance, **kwargs):
            commands.append(arguments)
            if arguments[0] == "create":
                return {"task": {"reused": False}}
            if arguments[0] == "nodejs":
                self.assertNotIn("--tab", arguments)
                self.assertNotIn("--group", arguments)
                return {"status": "succeeded", "result": {"value": dict(outcome)}}
            self.assertEqual(arguments[0], "finish")
            self.assertEqual(len(saved), 1)  # Save receipt before releasing/closing tabs.
            if isinstance(finish, Exception):
                raise finish
            return finish

        with patch("auto_promotion.backends.tabbit.cli", side_effect=cli):
            result = call(
                "x",
                self.settings,
                "verify",
                {
                    "item_id": "cleanup-test",
                    "payload": {
                        "action": "reply",
                        "target": "https://x.com/target/status/123",
                        "text": "exact",
                        "media": [],
                    },
                    "receipt": {"external_reference": "https://x.com/brand/status/789"},
                },
                lambda value: saved.append(dict(value)),
                lambda: self.fail("Must not send"),
            )
        return result, commands

    def test_verified_completion_closes_only_task_owned_pages(self):
        result, commands = self.verify({"status": "confirmed", "verified": True}, {"ok": True})
        self.assertEqual(commands[-1], ["finish", "--task", "X reply cleanup-test", "--discard"])
        self.assertEqual(result["browser_cleanup_state"], "closed")

    def test_unknown_or_unverified_results_retain_pages(self):
        for outcome in (
            {"status": "unknown", "verified": False},
            {"status": "confirmed", "verified": False},
            {"status": "unknown", "verified": True},
        ):
            with self.subTest(outcome=outcome):
                _, commands = self.verify(outcome, {"ok": True})
                self.assertNotIn("--discard", commands[-1])

    def test_cleanup_failure_does_not_replace_verified_publication(self):
        for finish in (
            BackendError("TabbitConnectionUnavailable"),
            {"ok": True, "forcedCleanup": True},
            {"ok": False},
        ):
            with self.subTest(finish=finish):
                result, _ = self.verify({"status": "confirmed", "verified": True}, finish)
                self.assertEqual(result["status"], "confirmed")
                self.assertTrue(result["verified"])
                self.assertEqual(result["browser_cleanup_state"], "incomplete")
