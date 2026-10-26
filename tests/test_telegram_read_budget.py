"""Account read budgets and private cache/cursor isolation without Telegram."""

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.telegram_reads import (
    ReadBudgetExceeded,
    TelegramReads,
)


class TelegramReadBudgetTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": private.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 1000.0
        self.policy = {
            "requests_per_window": 3,
            "window_seconds": 3600,
            "scan_rpc_limit": 40,
            "messages_per_page": 100,
            "groups_per_scan": 10,
            "cache_ttl_seconds": 300,
        }

    def guard(self, identity="42", revision="r1", label="test", **policy):
        return TelegramReads(
            label, identity, revision, {**self.policy, **policy}, clock=lambda: self.now
        )

    def test_budget_survives_restart_and_session_changes_without_refund(self):
        self.guard().reserve(2)
        changed = self.guard(revision="new-session", label="same-identity-another-label")
        changed.reserve(1)
        with self.assertRaises(ReadBudgetExceeded):
            self.guard(revision="third").reserve(1)
        self.guard(identity="43").reserve(1)

    def test_shorter_window_does_not_reset_existing_usage(self):
        self.guard().reserve(3)
        self.now += 100
        with self.assertRaises(ReadBudgetExceeded):
            self.guard(window_seconds=60).reserve(1)
        self.now = 4601
        self.guard(window_seconds=60).reserve(1)

    def test_limit_changes_retain_usage(self):
        self.guard().reserve(2)
        with self.assertRaises(ReadBudgetExceeded):
            self.guard(requests_per_window=1).reserve(1)
        self.guard(requests_per_window=4).reserve(2)
        with self.assertRaises(ReadBudgetExceeded):
            self.guard(requests_per_window=4).reserve(1)

    def test_batch_reservation_is_atomic_between_workers(self):
        self.guard()
        barrier = threading.Barrier(8)

        def reserve(_):
            guard = self.guard()
            barrier.wait()
            try:
                guard.reserve(2)
                return 2
            except ReadBudgetExceeded:
                return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 2)

    def test_scan_budget_does_not_refund_account_reservations(self):
        guard = self.guard(scan_rpc_limit=1)
        guard.reserve(1)
        with self.assertRaises(ReadBudgetExceeded) as caught:
            guard.reserve(1)
        self.assertEqual(caught.exception.reason, "TelegramReadScanBudgetExhausted")
        self.guard().reserve(2)
        with self.assertRaises(ReadBudgetExceeded):
            self.guard().reserve(1)

    def test_cache_is_scoped_to_identity_revision_and_original_observed_time(self):
        guard = self.guard()
        guard.put("directory", "same-query", {"rows": [1]}, observed_at=900)
        value, observed = self.guard().get("directory", "same-query")
        self.assertEqual((value, observed), ({"rows": [1]}, 900))
        self.assertIsNone(self.guard(identity="43").get("directory", "same-query"))
        self.assertIsNone(self.guard(revision="r2").get("directory", "same-query"))
        self.now = 1201
        self.assertIsNone(self.guard().get("directory", "same-query"))

    def test_cache_zero_disables_cache_without_skipping_budget(self):
        self.guard().put("directory", "key", {"rows": [1]})
        guard = self.guard(cache_ttl_seconds=0)
        self.assertIsNone(guard.get("directory", "key"))
        guard.reserve(3)
        with self.assertRaises(ReadBudgetExceeded):
            guard.reserve(1)

    def test_permissions_and_receipts_cannot_be_cached(self):
        for kind in ("permissions", "identity", "receipt"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.guard().put(kind, "key", {})

    def test_only_issued_scope_bound_cursor_is_accepted(self):
        guard = self.guard()
        token = {
            "v": 1,
            "scope": guard.scope,
            "identity": guard.identity,
            "peer_id": "-10042",
            "last": 3,
            "floor": 1,
            "head": 10,
            "complete": False,
            "initial_limit": 100,
        }
        guard.issue_cursor(token)
        self.assertTrue(self.guard().valid_cursor(token))
        self.assertFalse(self.guard().valid_cursor({**token, "last": 9}))
        self.assertFalse(self.guard(revision="r2").valid_cursor(token))
        self.assertFalse(self.guard(identity="43").valid_cursor(token))

    def test_latest_authoritative_policy_blocks_stale_and_raw_wider_payload(self):
        context = {"database": "synthetic", "account_id": "account"}
        with patch(
            "auto_promotion.telegram_reading.read_policy_context",
            return_value={
                "policy": {**self.policy, "requests_per_window": 2},
                "revision": 2,
            },
        ):
            guard = TelegramReads("test", "42", "r1", context=context, clock=lambda: self.now)
            guard.reserve(2)
        with self.assertRaises(ReadBudgetExceeded):
            self.guard(requests_per_window=100).reserve(1)
        # A delayed older call cannot overwrite the newer private authority.
        with (
            patch(
                "auto_promotion.telegram_reading.read_policy_context",
                return_value={
                    "policy": {**self.policy, "requests_per_window": 100},
                    "revision": 1,
                },
            ),
            self.assertRaises(ReadBudgetExceeded),
        ):
            guard.reserve(1)
        with patch("auto_promotion.telegram_reading.read_policy_context", side_effect=OSError):
            with self.assertRaises(BackendError) as caught:
                guard.reserve(1)
            self.assertEqual(caught.exception.safe_code, "TelegramReadPolicyUnavailable")

    def test_atomic_reservation_rechecks_authority_updated_after_refresh(self):
        context = {"database": "synthetic", "account_id": "account"}
        with patch(
            "auto_promotion.telegram_reading.read_policy_context",
            return_value={
                "policy": {**self.policy, "requests_per_window": 100},
                "revision": 1,
            },
        ):
            stale = TelegramReads("test", "42", "r1", context=context, clock=lambda: self.now)
            stale.reserve(0)
        with patch(
            "auto_promotion.telegram_reading.read_policy_context",
            return_value={
                "policy": {**self.policy, "requests_per_window": 1},
                "revision": 2,
            },
        ):
            current = TelegramReads("test", "42", "r1", context=context, clock=lambda: self.now)
            current.reserve(1)
        # Simulate a paused old worker whose refresh completed before the update.
        stale.refresh_policy = lambda: None
        with self.assertRaises(ReadBudgetExceeded):
            stale.reserve(1)


if __name__ == "__main__":
    unittest.main()
