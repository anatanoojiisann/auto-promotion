"""New browser observations must not reuse historical receipt guard events."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_persistent_pools as fixtures
from test_x_browser_safety import Page

from auto_promotion import native_bridge
from auto_promotion.account_blocks import observe
from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.core import Conflict
from auto_promotion.models import digest


class XGuardEventRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready()
        self.now = self.engine.clock()
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(Path(private.name))})
        environment.start()
        self.addCleanup(environment.stop)
        self.old = {
            "status": "unknown",
            "verified": False,
            "submission_started": True,
            "external_reference": "https://x.com/synthetic/status/123",
            "account_block_reason": "x_challenge",
            "account_block_event_id": "a" * 32,
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash=? WHERE id=?",
                (digest(["x", "synthetic"]), self.account["id"]),
            )
            observe(db, self.account["id"], self.old, now=self.now, operation="verify")
            db.execute("UPDATE items SET status='unknown',attempt_id='already-attempted'")

    def healthy_probe(self):
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            account = self.engine.check_account_block(self.account["id"])
        self.assertTrue(account["outbound_block"]["health_ok"])
        return account["outbound_block"]["generation"]

    def test_new_verify_challenge_invalidates_health_and_propagates_one_fresh_event(self):
        generation = self.healthy_probe()

        def call(platform, label, operation, payload, checkpoint, authorize):
            backend = BrowserBackend(
                platform,
                label,
                None,
                Page("https://x.com/account/access"),
                {"identity": "synthetic"},
            )
            backend.navigate_home = lambda: None
            return backend.run(operation, payload, checkpoint, authorize)

        request = {
            "platform": "x",
            "account": "synthetic",
            "account_id": self.account["id"],
            "database": self.store.path,
            "operation": "verify",
            "payload": {
                "item_id": "old-item",
                "result": dict(self.old),
                "payload": {"action": "reply"},
            },
        }
        with patch("auto_promotion.backends.browser.call", call):
            result = native_bridge.dispatch(request)
        self.assertNotEqual(result["account_block_event_id"], self.old["account_block_event_id"])
        current = self.engine.account(self.account["id"])["outbound_block"]
        self.assertFalse(current["health_ok"])
        self.assertGreater(current["generation"], generation)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["external_reference"], self.old["external_reference"])
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "unknown")
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], generation)

        # Native checkpoint and the subsequent Engine observation are the SAME
        # signal. Even a later successful probe must survive that duplicate.
        next_generation = self.healthy_probe()
        event = result["account_block_event_id"]
        with self.store.connect(write=True) as db:
            observe(db, self.account["id"], result, now=self.now, operation="verify")
        current = self.engine.account(self.account["id"])["outbound_block"]
        self.assertEqual(result["account_block_event_id"], event)
        self.assertTrue(current["health_ok"])
        self.assertEqual(current["generation"], next_generation)
        self.assertEqual(
            len(self.store.rows("SELECT * FROM audit WHERE kind='account.outbound_blocked'")), 1
        )
        with patch("auto_promotion.backends.browser.call", call):
            another = native_bridge.dispatch(request)
        self.assertNotEqual(another["account_block_event_id"], event)
        current = self.engine.account(self.account["id"])["outbound_block"]
        self.assertFalse(current["health_ok"])
        self.assertGreater(current["generation"], next_generation)

    def test_clean_verify_does_not_replay_historical_guard_fields(self):
        backend = BrowserBackend("x", "synthetic", None, Page(), {"identity": "synthetic"})
        backend._run = lambda operation, payload, checkpoint, authorize: {
            **payload["receipt"],
            "status": "unknown",
            "verified": False,
        }
        result = backend.run("verify", {"receipt": dict(self.old)}, Mock(), Mock())
        self.assertEqual(result["external_reference"], self.old["external_reference"])
        self.assertNotIn("account_block_event_id", result)
        self.assertNotIn("account_block_reason", result)


if __name__ == "__main__":
    unittest.main()
