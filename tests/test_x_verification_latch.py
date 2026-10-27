"""Offline regression for Milo's verification -> healthy false recovery."""

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_persistent_pools as fixtures

from auto_promotion import native_bridge
from auto_promotion.account_blocks import active_block, observe
from auto_promotion.account_checks import summary
from auto_promotion.backends.browser_status import check_context_status
from auto_promotion.backends.common import BackendError
from auto_promotion.core import Engine


class VerificationLatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready()
        self.env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(Path(self.fixture.temp.name) / "accounts")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.request = {
            "platform": "x", "account": "x-test", "operation": "health",
            "account_id": self.account["id"], "database": str(self.store.path), "payload": {},
        }

    def block(self, reason="BrowserVerificationRequired"):
        with self.store.connect(write=True) as db:
            return observe(db, self.account["id"], {"reason": reason},
                           now=self.engine.clock(), operation="execute",
                           payload={"item_id": "old-item"}, task_id=self.task["id"])

    def test_reason_only_legacy_verification_becomes_durable_stop(self):
        block = self.block()
        self.assertEqual(block["reason"], "x_challenge")
        restarted = Engine(self.store, self.engine.adapters, clock=self.engine.clock)
        self.assertFalse(restarted.account(self.account["id"])["writable"])
        self.assertIsNone(restarted._claim(self.store.one("SELECT id FROM items")["id"]))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_timeout_is_not_relabelled_as_captcha(self):
        self.assertIsNone(self.block("TimeoutError"))

    def test_engine_catches_legacy_health_result_without_new_guard_fields(self):
        with patch.object(self.engine, "_call_unlocked", return_value={
            "healthy": False, "reason": "BrowserVerificationRequired",
        }):
            self.engine.call(self.engine.account(self.account["id"]), "health")
        self.assertEqual(self.engine.account(self.account["id"])["outbound_block"]["reason"], "x_challenge")

    def test_healthy_identity_does_not_make_blocked_account_ready(self):
        self.block()
        with patch.object(self.engine, "call", return_value={
            "healthy": True, "identity_verified": True,
            "account_identity": "synthetic", "reason": "account_read_verified",
        }):
            checked = self.engine.check_account(self.account["id"], force=True)
        self.assertFalse(checked["writable"])
        self.assertFalse(summary(checked, self.engine.clock())["ready"])
        self.assertEqual(summary(checked, self.engine.clock())["state"], "verification_required")
        self.assertFalse(checked["outbound_block"]["health_ok"])

    def test_normal_native_health_stops_before_opening_browser(self):
        self.block()
        with patch("auto_promotion.backends.browser.call") as browser:
            result = native_bridge.dispatch(self.request)
        browser.assert_not_called()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["reason"], "BrowserVerificationRequired")

    def test_cached_stop_does_not_invalidate_explicit_recovery_evidence(self):
        from auto_promotion.account_blocks import blocked_health

        block = self.block()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_blocks SET health_ok=1,health_checked_at=? WHERE account_id=?",
                       (self.engine.clock(), self.account["id"]))
            observed = observe(db, self.account["id"], blocked_health(block),
                               now=self.engine.clock(), operation="health")
        self.assertEqual(observed["generation"], block["generation"])
        self.assertTrue(observed["health_ok"])

    def test_ordinary_check_preserves_successful_recovery_probe_in_database(self):
        from auto_promotion.models import digest

        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET identity_hash=? WHERE id=?",
                       (digest(["x", "synthetic"]), self.account["id"]))
        self.block()
        with patch.object(self.engine, "call", return_value={
            "healthy": True, "identity_verified": True, "account_identity": "synthetic",
        }):
            checked = self.engine.check_account_block(self.account["id"])
        generation = checked["outbound_block"]["generation"]
        with patch.object(self.engine, "call") as browser:
            stopped = self.engine.check_account(self.account["id"], force=True)
        browser.assert_not_called()
        self.assertFalse(stopped["writable"])
        current = self.engine.account(self.account["id"])
        self.assertTrue(current["outbound_block"]["health_ok"])
        self.assertEqual(current["outbound_block"]["generation"], generation)
        self.assertEqual(current["status"], "healthy")

    def test_stale_or_boolean_recovery_generation_does_not_probe(self):
        block = self.block()
        for generation in [block["generation"] - 1, True, "1"]:
            with self.subTest(generation=generation), patch("auto_promotion.backends.browser.call") as browser:
                result = native_bridge.dispatch({**self.request, "payload": {"account_block_recovery_generation": generation}})
            browser.assert_not_called()
            self.assertFalse(result["healthy"])

    def test_explicit_matching_recovery_probe_does_not_clear_stop(self):
        block = self.block()
        request = {**self.request, "payload": {"account_block_recovery_generation": block["generation"]}}
        with patch("auto_promotion.backends.browser.call", return_value={
            "healthy": True, "identity_verified": True, "account_identity": "synthetic",
        }) as browser:
            result = native_bridge.dispatch(request)
        browser.assert_called_once()
        self.assertTrue(result["healthy"])
        with self.store.connect() as db:
            self.assertIsNotNone(active_block(db, self.account["id"], self.engine.clock()))

    def test_legacy_health_exception_is_persisted_before_return(self):
        with patch("auto_promotion.backends.browser.call", side_effect=BackendError("BrowserVerificationRequired")):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(self.engine.account(self.account["id"])["outbound_block"]["reason"], "x_challenge")

    def test_legacy_returned_health_signal_is_persisted(self):
        with patch("auto_promotion.backends.browser.call", return_value={
            "healthy": False, "identity_verified": False, "reason": "BrowserVerificationRequired",
        }):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertIsNotNone(self.engine.account(self.account["id"])["outbound_block"])


class ExistingPageGuardTests(unittest.TestCase):
    def test_secondary_access_page_is_seen_before_dom_or_navigation(self):
        home = Mock(url="https://x.com/home")
        home.is_closed.return_value = False
        home.evaluate.return_value = None
        challenge = Mock(url="https://x.com/account/access?private=never-log-this")
        challenge.is_closed.return_value = False
        with self.assertRaisesRegex(BackendError, "BrowserVerificationRequired") as error:
            check_context_status(Mock(pages=[home, challenge]))
        self.assertNotIn("private", str(error.exception))
        challenge.evaluate.assert_not_called()
        home.goto.assert_not_called()

    def test_closed_or_unrelated_pages_do_not_block_x(self):
        closed = Mock(url="https://x.com/account/access")
        closed.is_closed.return_value = True
        unrelated = Mock(url="https://example.test/account/access")
        unrelated.is_closed.return_value = False
        check_context_status(Mock(pages=[closed, unrelated]))
        closed.evaluate.assert_not_called()
        unrelated.evaluate.assert_not_called()

    def test_backend_checks_existing_pages_before_selecting_home(self):
        from auto_promotion.backends.browser import call

        home = Mock(url="https://x.com/home")
        home.is_closed.return_value = False
        home.evaluate.return_value = None
        challenge = Mock(url="https://x.com/account/access")
        challenge.is_closed.return_value = False
        context = Mock(pages=[home, challenge])
        with patch("playwright.sync_api.sync_playwright"), \
             patch("auto_promotion.backends.browser.read_profile", return_value={"identity": "synthetic"}), \
             patch("auto_promotion.backends.browser.profile_path", return_value=Path("synthetic-profile")), \
             patch("auto_promotion.backends.browser.launch_context", return_value=context), \
             patch("auto_promotion.backends.browser.BrowserBackend") as backend:
            with self.assertRaisesRegex(BackendError, "BrowserVerificationRequired"):
                call("x", "offline", "health", {}, None, None)
        backend.assert_not_called()
        context.close.assert_called_once()
        home.goto.assert_not_called()
