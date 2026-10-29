import tempfile
import unittest
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput
from auto_promotion.store import Store


class Reader:
    def __init__(self):
        self.calls = []

    def call(self, account, operation, payload):
        self.calls.append((operation, payload))
        return {
            "candidates": [{"id": "123", "text": "video help", "source_event_at": 1800000000}],
            "read_state": "partial",
            "coverage_complete": False,
        }


class XReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reader = Reader()
        self.e = Engine(
            Store(Path(self.tmp.name) / "test.db"),
            AdapterRegistry(Path(self.tmp.name), overrides={"x": self.reader}),
            clock=lambda: 1800000000,
        )
        self.a = self.e.add_account(
            AccountInput(platform="x", label="reader", backend_label="reader", mode="live")
        )["id"]

    def test_real_task_reader_uses_bounded_readonly_contract_without_sending(self):
        value = self.e.search_x_content(self.a, "AI video help", timeout_seconds=15)
        operation, payload = self.reader.calls[0]
        self.assertEqual(operation, "discover")
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["high_frequency"])
        self.assertEqual(payload["upper_at"] - payload["lower_at"], 43200)
        self.assertEqual(payload["timeout_seconds"], 15)
        self.assertEqual(value["quality_status"], "context_review_required")
        self.assertFalse(value["coverage_complete"])
        self.assertEqual(value["decisions"], [])
        self.assertEqual(self.e.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.e.store.one("SELECT count(*) n FROM tasks")["n"], 0)

    def test_invalid_windows_or_budget_never_contact_reader(self):
        for options in (
            {"lookback_hours": float("nan")},
            {"lookback_hours": 0},
            {"timeout_seconds": 0},
            {"timeout_seconds": True},
            {"max_results": 101},
        ):
            with self.subTest(options=options), self.assertRaises(Conflict):
                self.e.search_x_content(self.a, "video", **options)
        self.assertEqual(self.reader.calls, [])

    def test_disabled_account_never_contacts_reader(self):
        with self.e.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (self.a,))
        result = self.e.search_x_content(self.a, "video")
        self.assertEqual(result["reason"], "AccountDisabled")
        self.assertEqual(self.reader.calls, [])

    def test_manual_challenge_block_does_not_probe_or_clear_it(self):
        from auto_promotion.account_blocks import observe

        with self.e.store.connect(write=True) as db:
            observe(
                db,
                self.a,
                {"reason": "BrowserVerificationRequired"},
                now=self.e.clock(),
                operation="discover",
            )
        result = self.e.search_x_content(self.a, "video")
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(self.reader.calls, [])
        self.assertIsNotNone(self.e.account(self.a)["outbound_block"])

    def test_persisted_read_cooldown_does_not_call_or_switch_account(self):
        with self.e.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET cooldown_until=? WHERE id=?", (self.e.clock() + 900, self.a)
            )
        result = self.e.search_x_content(self.a, "video")
        self.assertEqual(result["read_state"], "rate_limited")
        self.assertEqual(self.reader.calls, [])
