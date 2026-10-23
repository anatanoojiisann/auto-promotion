"""Read throttling and health evidence: temporary databases and fake clients only."""

import asyncio
import os
import socket
import sqlite3
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock, patch

import test_engine as fixtures
import test_native_backends as native_fixtures

from auto_promotion import native_bridge
from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.backends.common import BackendError, Receipts, profile_path
from auto_promotion.backends.telegram import TelegramBackend
from auto_promotion.core import Engine
from auto_promotion.models import AccountInput, CandidateInput, TaskInput


class ReadHealthTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.f.tmp.name + "/accounts"})
        environment.start()
        self.addCleanup(environment.stop)
        self.network = patch.object(
            socket, "create_connection", side_effect=AssertionError("offline")
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.a = self.f.accounts["telegram"]
        with self.f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live' WHERE id=?", (self.a["id"],))
        self.a = self.f.engine.account(self.a["id"])

    def cool(self):
        with self.f.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET cooldown_until=? WHERE id=?", (self.f.now + 3600, self.a["id"])
            )

    def test_stale_account_verify_cannot_bypass_new_cooldown(self):
        self.cool()
        with patch.object(self.f.provider, "call", return_value={"verified": False}) as backend:
            result = self.f.engine.call(self.a, "verify", {"payload": {"target": "@group"}})
        self.assertEqual(backend.call_count, 0)
        self.assertEqual(result["reason"], "AccountCooldown")
        self.assertFalse(
            result.get("rate_limited", False), "local skip is not a new server FloodWait"
        )

    def test_state_is_reloaded_after_acquiring_account_lane(self):
        original = self.f.engine.lanes.acquire

        @contextmanager
        def acquire(*args, **kwargs):
            with original(*args, **kwargs) as owner:
                self.cool()
                yield owner

        with (
            patch.object(self.f.engine.lanes, "acquire", acquire),
            patch.object(self.f.provider, "call") as backend,
        ):
            self.f.engine.call(self.a, "preflight", {"payload": {"target": "@group"}})
        self.assertEqual(backend.call_count, 0)

    def historical_items(self, account, count):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='simulation' WHERE id=?", (account["id"],))
        task = f.engine.create_task(
            TaskInput(
                name="synthetic history",
                account_ids=[account["id"]],
                policy={
                    "starts_at": fixtures.iso(f.now - 86400),
                    "ends_at": fixtures.iso(f.now + 86400),
                },
            )
        )
        f.engine.start_task(task["id"], sending=False)
        # Draft only: no start/execute/real platform call.
        f.engine.set_items(
            task["id"],
            [
                f.item(
                    account_id=account["id"],
                    text="synthetic " + str(n),
                    scheduled_at=fixtures.iso(f.now - (count - n) * 120),
                )
                for n in range(count)
            ],
        )
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live' WHERE id=?", (account["id"],))
            db.execute("UPDATE tasks SET mode='live' WHERE id=?", (task["id"],))
            db.execute("UPDATE items SET status='unknown' WHERE task_id=?", (task["id"],))

    def test_first_flood_wait_blocks_remaining_same_account_but_not_other_account(self):
        f = self.f
        other = f.engine.add_account(
            AccountInput(
                platform="telegram", mode="simulation", label="other", backend_label="other"
            )
        )
        self.historical_items(self.a, 3)
        self.historical_items(other, 2)
        calls = []

        def backend(account, operation, payload):
            self.assertEqual(operation, "verify")
            calls.append(account["id"])
            return {
                "verified": False,
                "rate_limited": account["id"] == self.a["id"],
                "retry_after_seconds": 3600,
                "reason": "FloodWaitError",
            }

        with patch.object(f.provider, "call", side_effect=backend):
            f.engine.collect_statistics()
        self.assertEqual(calls.count(self.a["id"]), 1)
        self.assertEqual(calls.count(other["id"]), 2)
        self.assertEqual(
            {x["status"] for x in f.store.rows("SELECT status FROM items")}, {"unknown"}
        )
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_missing_receipt_does_not_even_construct_or_connect_telegram_client(self):
        factory = Mock(side_effect=AssertionError("no receipt, no client"))
        with patch(
            "auto_promotion.backends.telegram.read_profile", return_value={"identity": "42"}
        ):
            backend = TelegramBackend("synthetic", client_factory=factory)
            result = asyncio.run(
                backend.run(
                    "verify",
                    {
                        "payload": {
                            "action": "send_message",
                            "target": "@group",
                            "text": "x",
                            "media": [],
                        },
                        "receipt": {},
                    },
                    Mock(),
                    Mock(),
                )
            )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "no_durable_receipt")
        self.assertEqual(factory.call_count, 0)

    def test_healthy_check_is_briefly_cached_but_preflight_still_calls_platform(self):
        f = self.f
        original = f.provider.call

        def actual(account, operation, payload):
            return (
                {"allowed": True}
                if operation == "preflight"
                else original(account, operation, payload)
            )

        with patch.object(f.provider, "call", side_effect=actual) as backend:
            f.engine.check_account(self.a["id"])
            f.engine.check_account(self.a["id"])
            self.assertEqual(sum(c.args[1] == "health" for c in backend.call_args_list), 1)
            for _ in range(2):
                f.engine.call(
                    f.engine.account(self.a["id"]), "preflight", {"payload": {"target": "@group"}}
                )
            self.assertEqual(sum(c.args[1] == "preflight" for c in backend.call_args_list), 2)

    def test_transient_health_failure_is_not_login_required_and_has_bounded_backoff(self):
        f = self.f
        with patch.object(
            f.provider, "call", side_effect=BackendError("IncompleteReadError")
        ) as backend:
            result = f.engine.check_account(self.a["id"])
            self.assertEqual(result["connection_state"], "temporary_unavailable")
            self.assertEqual(result["health"]["reason"], "IncompleteReadError")
            self.assertFalse(result["writable"])
            f.now += 4
            f.engine.check_account(self.a["id"])
            self.assertEqual(backend.call_count, 1)
            self.assertGreater(result["health"]["next_check_at"], f.now)

    def test_cooldown_display_overrides_old_healthy_without_refreshing_deadline(self):
        self.cool()
        account = self.f.engine.check_account(self.a["id"])
        self.assertEqual(account["connection_state"], "cooldown")
        self.assertFalse(account["writable"])
        self.assertEqual(account["cooldown_until"], self.f.now + 3600)

    def test_download_after_http403_is_navigation_evidence_not_login_failure(self):
        callbacks = {}
        page = Mock()
        page.on.side_effect = lambda event, callback: callbacks.update({event: callback})

        def navigate(*args, **kwargs):
            if "response" in callbacks:
                callbacks["response"](
                    Mock(
                        status=403,
                        url="https://x.com",
                        frame=page.main_frame,
                        request=Mock(is_navigation_request=lambda: True),
                    )
                )
            raise RuntimeError("Page.goto: Download is starting SECRET_CANARY")

        page.goto.side_effect = navigate
        backend = BrowserBackend("x", "synthetic", Mock(), page, {"identity": "synthetic"})
        with self.assertRaises(BackendError) as caught:
            backend.run("health", {}, Mock(), Mock())
        self.assertEqual(caught.exception.safe_code, "BrowserNavigationAccessDenied")
        self.assertEqual(caught.exception.execution_step, "home_navigation")
        self.assertNotIn("SECRET", str(caught.exception))

    def ready_live(self):
        f = native_fixtures.ClaimTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        with f.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='not_attempted',attempt_id=NULL,claimed_at=NULL")
            db.execute("DELETE FROM attempts")
            db.execute("UPDATE accounts SET next_at=0")
            db.execute("UPDATE tasks SET next_at=0")
        return f

    def test_live_execution_does_not_reuse_display_health_cache(self):
        f = self.ready_live()

        def provider(account, operation, payload):
            return (
                {"healthy": False, "identity_verified": False, "reason": "TelegramLoginRequired"}
                if operation == "health"
                else {"allowed": True}
            )

        with patch.object(f.engine.adapters, "call", side_effect=provider) as backend:
            f.engine.run_once()
        self.assertEqual([c.args[1] for c in backend.call_args_list], ["health"])
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(f.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_db_busy_health_cannot_fall_back_to_stored_healthy_before_claim(self):
        f = self.ready_live()
        with (
            patch.object(
                f.engine,
                "_check_account_locked",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch.object(f.engine.adapters, "call", return_value={"allowed": True}) as backend,
        ):
            f.engine.run_once()
        self.assertEqual(backend.call_count, 0)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])

    def test_cached_health_is_read_only_and_expires(self):
        f = self.f
        f.engine.check_account(self.a["id"])
        original = f.store.connect

        @contextmanager
        def readonly(*args, **kwargs):
            self.assertFalse(kwargs.get("write", False))
            with original(*args, **kwargs) as db:
                yield db

        with (
            patch.object(f.store, "connect", readonly),
            patch.object(f.provider, "call") as backend,
        ):
            self.assertTrue(f.engine.check_account(self.a["id"])["health"]["probe_cached"])
            self.assertEqual(backend.call_count, 0)
        f.now += 16
        with patch.object(f.provider, "call", wraps=f.provider.call) as backend:
            f.engine.check_account(self.a["id"])
            self.assertEqual(backend.call_count, 1)

    def test_cached_healthy_never_overrides_disable_or_new_cooldown(self):
        f = self.f
        f.engine.check_account(self.a["id"])
        f.engine.disable_account(self.a["id"], True)
        with patch.object(f.provider, "call") as backend:
            result = f.engine.check_account(self.a["id"], force=True)
            self.assertEqual(result["connection_state"], "disabled")
            self.assertFalse(result["writable"])
            f.engine.disable_account(self.a["id"], False)
            self.cool()
            self.assertEqual(
                f.engine.check_account(self.a["id"], force=True)["connection_state"], "cooldown"
            )
            self.assertEqual(backend.call_count, 0)

    def test_force_cannot_skip_failure_backoff_but_expiry_can_reprobe(self):
        f = self.f
        with patch.object(
            f.provider, "call", side_effect=BackendError("IncompleteReadError")
        ) as backend:
            first = f.engine.check_account(self.a["id"])
            f.now += 4
            f.engine.check_account(self.a["id"], force=True)
            self.assertEqual(backend.call_count, 1)
            f.now = first["health"]["next_check_at"] + 1
            second = f.engine.check_account(self.a["id"])
            self.assertEqual(backend.call_count, 2)
            self.assertEqual(second["health"]["next_check_at"] - f.now, 60)

    def test_identity_failure_is_explicit_and_not_treated_as_transient(self):
        f = self.f
        f.engine.check_account(self.a["id"])
        with patch.object(
            f.provider,
            "call",
            return_value={
                "healthy": False,
                "identity_verified": False,
                "reason": "TelegramLoginRequired",
            },
        ):
            result = f.engine.check_account(self.a["id"], force=True)
        self.assertEqual(result["connection_state"], "authorization_required")
        self.assertFalse(result["writable"])
        self.assertIn("last_verified_at", result["health"])

    def test_connection_changed_during_probe_cannot_receive_old_verified_result(self):
        f = self.f
        f.engine.check_account(self.a["id"])
        before = f.engine.account(self.a["id"])["checked_at"]

        def changed(account, operation, payload):
            with f.store.connect(write=True) as db:
                db.execute(
                    "UPDATE accounts SET backend_label='replacement' WHERE id=?", (self.a["id"],)
                )
            return {"healthy": True, "identity_verified": True, "account_identity": "old-identity"}

        with patch.object(f.provider, "call", side_effect=changed):
            result = f.engine.check_account(self.a["id"], force=True)
        self.assertEqual(result["connection_state"], "unverified")
        self.assertEqual(result["checked_at"], before)
        self.assertFalse(result["writable"])
        self.assertIsNone(
            f.store.one("SELECT identity_hash FROM accounts WHERE id=?", (self.a["id"],))[
                "identity_hash"
            ]
        )

    def test_profile_metadata_change_invalidates_display_cache(self):
        f = self.f
        root = profile_path("telegram", self.a["backend_label"])
        root.mkdir(parents=True)
        path = root / "credentials.json"
        path.write_text('{"synthetic":true}')
        f.engine.check_account(self.a["id"])
        path.write_text('{"synthetic":true,"revision":2}')
        with patch.object(f.provider, "call", wraps=f.provider.call) as backend:
            f.engine.check_account(self.a["id"])
        self.assertEqual(backend.call_count, 1)

    def test_two_engines_share_lane_and_deduplicate_health_probe(self):
        f = self.f
        other = Engine(f.store, f.registry, clock=lambda: f.now)
        entered, release = threading.Event(), threading.Event()
        original = f.provider.call

        def probe(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args)

        with (
            patch.object(f.provider, "call", side_effect=probe) as backend,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            running = workers.submit(f.engine.check_account, self.a["id"])
            try:
                self.assertTrue(entered.wait(3))
                waiting = other.check_account(self.a["id"])
                self.assertEqual(waiting["connection_state"], "busy")
                self.assertFalse(waiting["writable"])
            finally:
                release.set()
            self.assertEqual(running.result(3)["connection_state"], "healthy")
            self.assertTrue(other.check_account(self.a["id"])["health"]["probe_cached"])
            self.assertEqual(backend.call_count, 1)

    def test_lane_db_preparation_failure_does_not_call_provider_or_mark_auth_lost(self):
        f = self.f
        with (
            patch.object(
                f.engine.lanes,
                "acquire",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch.object(f.provider, "call") as backend,
        ):
            result = f.engine.check_account(self.a["id"], force=True)
        self.assertEqual(backend.call_count, 0)
        self.assertEqual(result["connection_state"], "temporary_unavailable")
        self.assertEqual(result["health"]["reason"], "LocalDatabaseUnavailable")
        self.assertEqual(
            f.store.one("SELECT status FROM accounts WHERE id=?", (self.a["id"],))["status"],
            "healthy",
        )

    def test_local_skips_and_reapplied_same_flood_result_do_not_extend_deadline(self):
        f = self.f
        with patch.object(
            f.provider,
            "call",
            return_value={
                "verified": False,
                "rate_limited": True,
                "retry_after_seconds": 120,
                "reason": "FloodWaitError",
            },
        ) as backend:
            result = f.engine.call(self.a, "verify", {})
            deadline = f.engine.account(self.a["id"])["cooldown_until"]
            f.now += 5
            f.engine.apply_cooldown(self.a["id"], result)
            skipped = f.engine.call(self.a, "verify", {})
            self.assertEqual(backend.call_count, 1)
        self.assertEqual(f.engine.account(self.a["id"])["cooldown_until"], deadline)
        self.assertFalse(skipped.get("rate_limited", False))

    def test_invalid_receipt_ids_never_construct_client(self):
        for ids in (None, [], [True], ["123"], [0], [-1], {"id": 123}):
            with (
                self.subTest(ids=ids),
                patch(
                    "auto_promotion.backends.telegram.read_profile", return_value={"identity": "42"}
                ),
            ):
                factory = Mock(side_effect=AssertionError("no client"))
                backend = TelegramBackend("synthetic", client_factory=factory)
                result = asyncio.run(
                    backend.run("verify", {"receipt": {"message_ids": ids}}, Mock(), Mock())
                )
                self.assertEqual(result["reason"], "no_durable_receipt")
                factory.assert_not_called()

    def test_private_receipt_is_loaded_before_early_id_guard_and_verified_read_only(self):
        f = native_fixtures.TelegramTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        backend = f.backend()
        f.client.messages[123] = types.SimpleNamespace(
            id=123, out=True, message="exact", media=None, reply_to_msg_id=None
        )
        receipts = Receipts("telegram", "default")
        receipts.begin("execute:synthetic", {"synthetic": True})
        receipts.save("execute:synthetic", {"status": "unknown", "message_ids": [123]})

        def invoke(label, operation, payload, checkpoint, authorize):
            return asyncio.run(backend.run(operation, payload, checkpoint, authorize))

        request = {
            "platform": "telegram",
            "account": "default",
            "operation": "verify",
            "payload": {
                "item_id": "synthetic",
                "payload": {
                    "action": "send_message",
                    "target": "@public_group",
                    "text": "exact",
                    "media": [],
                    "reply_to": None,
                },
                "result": {},
            },
        }
        with patch("auto_promotion.backends.telegram.call", side_effect=invoke):
            result = native_bridge.dispatch(request)
        self.assertTrue(result["verified"])
        self.assertEqual(f.client.sends, [])

    def test_same_size_mtime_atomic_profile_replacement_invalidates_cache(self):
        f = self.f
        root = profile_path("telegram", self.a["backend_label"])
        root.mkdir(parents=True)
        path = root / "credentials.json"
        path.write_text('{"synthetic":1}')
        f.engine.check_account(self.a["id"])
        before = path.stat()
        replacement = root / "replacement.tmp"
        replacement.write_text('{"synthetic":2}')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        with patch.object(f.provider, "call", wraps=f.provider.call) as backend:
            f.engine.check_account(self.a["id"])
        self.assertEqual(backend.call_count, 1)

    def test_native_account_busy_preserves_stored_identity_without_granting_write(self):
        f = self.f
        before = f.engine.account(self.a["id"])
        with patch.object(f.provider, "call", side_effect=BackendError("AccountBusy")):
            result = f.engine.check_account(self.a["id"], force=True)
        self.assertEqual(result["connection_state"], "busy")
        self.assertFalse(result["writable"])
        self.assertEqual(f.engine.account(self.a["id"])["health"], before["health"])

    def test_join_execution_also_requires_fresh_health_before_claim(self):
        f = self.f
        f.engine.live_enabled = True
        f.engine.check_account(self.a["id"])
        task = f.engine.create_task(
            TaskInput(
                name="synthetic join",
                account_ids=[self.a["id"]],
                policy={
                    "starts_at": fixtures.iso(f.now - 100),
                    "ends_at": fixtures.iso(f.now + 3600),
                    "max_joins": 1,
                },
            )
        )
        f.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=self.a["id"],
                target="@public_group",
                text="AI video",
                language="en",
                confidence=0.95,
                members=1000,
                activity=20,
            ),
        )
        f.engine.start_task(task["id"], sending=False)
        with (
            patch.object(
                f.provider,
                "call",
                return_value={
                    "healthy": False,
                    "identity_verified": False,
                    "reason": "TelegramLoginRequired",
                },
            ) as backend,
            patch.object(f.engine.adapters, "commissioned", return_value=True),
        ):
            f.engine.run_joins(task["id"])
        self.assertEqual([c.args[1] for c in backend.call_args_list], ["health"])
        self.assertEqual(f.store.rows("SELECT * FROM join_attempts"), [])

    def test_home_timeout_and_download_are_not_reported_as_logout(self):
        for error, expected in (
            (TimeoutError("secret URL"), "BrowserNavigationTimeout"),
            (RuntimeError("Download is starting secret"), "BrowserNavigationDownload"),
        ):
            with self.subTest(expected=expected):
                page = Mock()
                page.goto.side_effect = error
                backend = BrowserBackend("x", "synthetic", Mock(), page, {"identity": "synthetic"})
                with self.assertRaises(BackendError) as caught:
                    backend.run("health", {}, Mock(), Mock())
                self.assertEqual(caught.exception.safe_code, expected)
                self.assertEqual(caught.exception.execution_step, "home_navigation")
                page.get_by_test_id.assert_not_called()
                page.goto.assert_called_once()

    def test_health_diagnostics_preserve_safe_phase_not_exception_text(self):
        error = BackendError("BrowserNavigationAccessDenied")
        error.execution_step = "home_navigation"
        error.submission_started = False
        request = {"platform": "x", "account": "synthetic", "operation": "health", "payload": {}}
        with patch("auto_promotion.backends.browser.call", side_effect=error):
            result = native_bridge.dispatch(request)
        self.assertEqual(result["reason"], "BrowserNavigationAccessDenied")
        self.assertEqual(result["execution_step"], "home_navigation")
        self.assertFalse(result["submission_started"])

    def test_configuration_revision_after_guard_is_not_stamped_on_old_result(self):
        from auto_promotion import health_checks

        f = self.f
        root = profile_path("telegram", self.a["backend_label"])
        root.mkdir(parents=True)
        path = root / "credentials.json"
        path.write_text('{"synthetic":1}')
        original = health_checks.probe_key
        replaced = []

        def after_guard(account, database, **kwargs):
            value = original(account, database, **kwargs)
            if isinstance(account.get("health"), str) and not replaced:
                replaced.append(True)
                path.write_text('{"synthetic":2}')
            return value

        with patch.object(health_checks, "probe_key", side_effect=after_guard):
            result = f.engine.check_account(self.a["id"], force=True)
        self.assertEqual(replaced, [True])
        self.assertEqual(result["connection_state"], "unverified")
        self.assertFalse(result["writable"])
        self.assertNotEqual(
            result["health"]["probe_key"], original(f.engine.account(self.a["id"]), f.store.path)
        )


if __name__ == "__main__":
    unittest.main()
