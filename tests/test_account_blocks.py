"""Offline account stop / recovery and once-only regression contracts."""

import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.account_blocks import active_block, observe
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import digest
from auto_promotion.queue_store import JobQueue


class AccountBlockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready()
        self.now = self.engine.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash=? WHERE id=?",
                (digest(["x", "synthetic"]), self.account["id"]),
            )

    def observe(self, result, target="one", item="one", at=None):
        with self.store.connect(write=True) as db:
            return observe(
                db,
                self.account["id"],
                result,
                now=at or self.now,
                operation="execute",
                payload={"item_id": item, "payload": {"target": target}},
                task_id=self.task["id"],
            )

    def test_challenge_survives_restart_and_prevents_pool_claim(self):
        self.observe({"account_block_reason": "x_challenge", "failure_class": "challenge"})
        restarted = Engine(self.store, self.engine.adapters, clock=self.engine.clock)
        self.assertEqual(
            restarted.account(self.account["id"])["outbound_block"]["reason"], "x_challenge"
        )
        queue = JobQueue(restarted)
        queue.sync()
        self.assertIsNone(queue.claim("simulation.x", "other-worker"))
        self.assertIsNone(restarted._claim(self.store.one("SELECT id FROM items")["id"]))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_cross_target_speaking_rejections_pause_without_permanent_ban(self):
        rejection = {
            "status": "failed_known",
            "account_restriction": "telegram_speaking_restricted",
            "reason": "UserBannedInChannelError",
        }
        self.assertIsNone(self.observe(rejection))
        self.assertIsNone(self.observe(rejection, item="two"))
        block = self.observe(rejection, target="two", item="three")
        self.assertEqual(block["reason"], "telegram_speaking_restricted")
        self.assertFalse(self.engine.account(self.account["id"])["disabled"])
        self.assertEqual(
            len(self.store.rows("SELECT * FROM audit WHERE kind='account.outbound_blocked'")), 1
        )

    def test_page_timeouts_trip_bounded_circuit_without_claiming_captcha(self):
        result = {"browser_page_anomaly": True, "failure_class": "page_anomaly"}
        self.assertIsNone(self.observe(result, item="one"))
        self.assertIsNone(self.observe(result, item="two"))
        block = self.observe(result, item="three")
        self.assertEqual(block["reason"], "browser_page_anomaly")
        self.assertEqual(block["until"], self.now + 900)
        with self.store.connect() as db:
            self.assertIsNone(active_block(db, self.account["id"], self.now + 901))

    def test_explicit_recovery_requires_fresh_matching_check(self):
        self.observe({"account_block_reason": "x_challenge"})
        block = self.engine.account(self.account["id"])["outbound_block"]
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], block["generation"])
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            checked = self.engine.check_account_block(self.account["id"])
        self.assertTrue(checked["outbound_block"]["health_ok"])
        self.assertFalse(checked["writable"])
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], block["generation"] + 1)
        resumed = self.engine.resume_account_block(self.account["id"], block["generation"])
        self.assertIsNone(resumed["outbound_block"])
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_failed_health_check_and_identity_change_cannot_resume(self):
        self.observe({"account_block_reason": "x_challenge"})
        with patch.object(
            self.engine,
            "call",
            return_value={"healthy": True, "identity_verified": True, "account_identity": "wrong"},
        ):
            checked = self.engine.check_account_block(self.account["id"])
        self.assertFalse(checked["outbound_block"]["health_ok"])
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(
                self.account["id"], checked["outbound_block"]["generation"]
            )

    def test_repeated_same_block_does_not_repeat_notification(self):
        for _ in range(3):
            self.observe({"account_block_reason": "x_challenge"})
        self.assertEqual(
            len(self.store.rows("SELECT * FROM audit WHERE kind='account.outbound_blocked'")), 1
        )

    def test_ordinary_healthy_probe_cannot_clear_stop_or_mark_recovery_success(self):
        self.observe({"account_block_reason": "x_challenge"})
        with patch.object(
            self.engine,
            "_call_unlocked",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ) as ordinary_probe:
            checked = self.engine.check_account(self.account["id"], force=True)
        ordinary_probe.assert_not_called()
        self.assertEqual(checked["connection_state"], "verification_required")
        self.assertFalse(checked["writable"])
        self.assertFalse(checked["outbound_block"]["health_ok"])
        restarted = Engine(self.store, self.engine.adapters, clock=self.engine.clock)
        self.assertEqual(
            restarted.account(self.account["id"])["outbound_block"]["reason"], "x_challenge"
        )
        self.assertIsNone(restarted._claim(self.store.one("SELECT id FROM items")["id"]))
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_late_recovery_health_success_cannot_verify_new_challenge_generation(self):
        self.observe({"account_block_reason": "x_challenge"})
        # Obtain a prior successful recovery observation so another detection
        # advances its generation. A later response must not verify that epoch.
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            prior = self.engine.check_account_block(self.account["id"])["outbound_block"]

        def delayed(*_args):
            self.observe({"account_block_reason": "x_challenge"}, item="later")
            return {"healthy": True, "identity_verified": True, "account_identity": "synthetic"}

        with patch.object(self.engine, "_call_unlocked", side_effect=delayed):
            checked = self.engine.check_account_block(self.account["id"])
        self.assertGreater(checked["outbound_block"]["generation"], prior["generation"])
        self.assertFalse(checked["outbound_block"]["health_ok"])
        self.assertFalse(checked["writable"])
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], prior["generation"])

    def test_first_recovery_probe_cannot_verify_new_challenge_detected_while_in_flight(self):
        self.observe({"account_block_reason": "x_challenge"})
        original = self.engine.account(self.account["id"])["outbound_block"]
        self.assertIsNone(original["health_checked_at"])

        def delayed(*_args):
            self.observe({"account_block_reason": "x_challenge"}, item="first-probe-new-evidence")
            return {"healthy": True, "identity_verified": True, "account_identity": "synthetic"}

        with patch.object(self.engine, "_call_unlocked", side_effect=delayed):
            checked = self.engine.check_account_block(self.account["id"])
        block = checked["outbound_block"]
        self.assertGreater(block["generation"], original["generation"])
        self.assertFalse(block["health_ok"])
        self.assertFalse(checked["writable"])
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], block["generation"])

    def test_new_challenge_invalidates_previous_successful_health_probe(self):
        self.observe({"account_block_reason": "x_challenge"})
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            checked = self.engine.check_account_block(self.account["id"])
        generation = checked["outbound_block"]["generation"]
        self.observe({"account_block_reason": "x_challenge"}, item="new-challenge")
        current = self.engine.account(self.account["id"])["outbound_block"]
        self.assertFalse(current["health_ok"])
        self.assertGreater(current["generation"], generation)
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(self.account["id"], generation)

    def test_duplicate_evidence_does_not_invalidate_probe_or_notify_twice(self):
        evidence = {"account_block_reason": "x_challenge"}
        self.observe(evidence)
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            checked = self.engine.check_account_block(self.account["id"])
        self.observe(evidence)
        self.assertEqual(
            self.engine.account(self.account["id"])["outbound_block"], checked["outbound_block"]
        )

    def test_resume_expired_health_keeps_stop_and_items(self):
        self.observe({"account_block_reason": "x_challenge"})
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            checked = self.engine.check_account_block(self.account["id"])
        self.engine.clock = lambda: self.now + 301
        with self.assertRaises(Conflict):
            self.engine.resume_account_block(
                self.account["id"], checked["outbound_block"]["generation"]
            )
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_resume_does_not_reopen_unknown_or_missed_or_attempted_items(self):
        self.observe({"account_block_reason": "x_challenge"})
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown',attempt_id='synthetic-attempt'")
        with patch.object(
            self.engine,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "account_identity": "synthetic",
            },
        ):
            checked = self.engine.check_account_block(self.account["id"])
        self.engine.resume_account_block(
            self.account["id"], checked["outbound_block"]["generation"]
        )
        self.assertEqual(
            self.store.one("SELECT status,attempt_id FROM items"),
            {"status": "unknown", "attempt_id": "synthetic-attempt"},
        )
        queue = JobQueue(self.engine)
        queue.sync()
        self.assertIsNone(queue.claim("simulation.x", "replacement"))


class NativeBlockPersistenceTests(unittest.TestCase):
    def setUp(self):
        import test_native_backends as native_fixtures

        self.fixture = native_fixtures.ClaimTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.account, self.request = self.fixture.account, self.fixture.request

    def test_final_authorize_rechecks_block_after_other_worker_claimed(self):
        from auto_promotion.backends.common import BackendError, verify_claim

        verify_claim(self.request)
        with self.store.connect(write=True) as db:
            observe(
                db,
                self.account["id"],
                {"account_block_reason": "x_challenge"},
                now=self.engine.clock(),
                operation="health",
            )
        with self.assertRaisesRegex(BackendError, "AccountOutboundBlocked"):
            verify_claim(self.request)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_private_journal_recovers_after_central_guard_write_failure(self):
        import sqlite3

        from auto_promotion import native_bridge
        from auto_promotion.account_blocks import read_private_stop
        from auto_promotion.backends.common import BackendError, verify_claim

        def backend(label, operation, payload, checkpoint, authorize):
            result = {
                "status": "failed_known",
                "confirmed_not_submitted": True,
                "account_block_reason": "x_challenge",
                "reason": "XChallengeRequired",
            }
            checkpoint(result)
            return result

        with (
            patch("auto_promotion.backends.telegram.call", backend),
            patch(
                "auto_promotion.account_blocks.observe",
                side_effect=sqlite3.OperationalError("synthetic lock"),
            ),
        ):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(result["reason"], "XChallengeRequired")
        self.assertIsNotNone(read_private_stop("telegram", "default"))
        with self.assertRaisesRegex(BackendError, "AccountOutboundBlocked"):
            verify_claim(self.request)
        restarted = Engine(self.store, self.engine.adapters, clock=self.engine.clock)
        self.assertEqual(
            restarted.account(self.account["id"])["outbound_block"]["reason"], "x_challenge"
        )
        self.assertIsNone(read_private_stop("telegram", "default"))

    def test_clearing_old_private_event_cannot_remove_concurrent_new_event(self):
        from concurrent.futures import ThreadPoolExecutor

        from auto_promotion.account_blocks import (
            clear_private_stop,
            read_private_stop,
            save_private_stop,
        )

        old = save_private_stop("telegram", "default", {"account_block_reason": "x_challenge"})
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(
                save_private_stop,
                "telegram",
                "default",
                {"account_block_reason": "x_login_required"},
            )
            new = future.result()
        clear_private_stop("telegram", "default", old)
        self.assertEqual(read_private_stop("telegram", "default")["event"], new)

    def test_task_export_keeps_running_sha_rpc_counts_cooldown_and_block(self):
        from auto_promotion.task_tools import TaskTools

        claim = self.request["payload"]
        result = {
            "status": "failed_known",
            "verified": False,
            "reason": "UserBannedInChannelError",
            "account_restriction": "telegram_speaking_restricted",
            "rpc_counts": {"mutation": 1},
            "rpc_count_source": "telethon_call",
            "cooldown_source": "telegram_rpc_restriction",
            "runtime_git_commit_at_start": "a" * 40,
            "runtime_source_fingerprint": "b" * 64,
            "runtime_started_at": self.engine.clock(),
        }
        self.engine.finish(claim, result)
        with self.store.connect(write=True) as db:
            observe(
                db,
                self.account["id"],
                {"account_block_reason": "x_challenge"},
                now=self.engine.clock(),
                operation="health",
                task_id=claim["task_id"],
            )
        evidence = TaskTools(self.engine).evidence(claim["task_id"])
        saved = evidence["items"][0]["result"]
        self.assertEqual(saved["runtime_git_commit_at_start"], "a" * 40)
        self.assertEqual(saved["rpc_counts"], {"mutation": 1})
        self.assertEqual(saved["cooldown_source"], "telegram_rpc_restriction")
        self.assertEqual(evidence["account_blocks"][0]["reason"], "x_challenge")

    def test_permission_error_reading_journal_fails_closed(self):
        from pathlib import Path

        from auto_promotion.account_blocks import read_private_stop

        with patch.object(Path, "iterdir", side_effect=PermissionError("synthetic")):
            result = read_private_stop("telegram", "default")
        self.assertEqual(result["result"]["account_block_reason"], "account_guard_unavailable")

    def test_partial_and_fsynced_staging_crashes_fail_closed_on_restart(self):
        from auto_promotion.account_blocks import private_stop_path, read_private_stop
        from auto_promotion.backends.common import BackendError, verify_claim

        root = private_stop_path("telegram", "default")
        root.mkdir(parents=True, exist_ok=True)
        for contents in (
            '{"event":',
            '{"event":"' + "a" * 32 + '","result":{"account_block_reason":"x_challenge"}}',
        ):
            with self.subTest(contents=contents):
                staging = root / ("a" * 32 + "." + "b" * 32 + ".tmp")
                staging.write_text(contents, encoding="utf-8")
                marker = read_private_stop("telegram", "default")
                self.assertEqual(
                    marker["result"]["account_block_reason"], "account_guard_unavailable"
                )
                with self.assertRaisesRegex(BackendError, "AccountOutboundBlocked"):
                    verify_claim(self.request)
                restarted = Engine(self.store, self.engine.adapters, clock=self.engine.clock)
                self.assertEqual(
                    restarted.account(self.account["id"])["outbound_block"]["reason"],
                    "account_guard_unavailable",
                )
                staging.unlink()

    def test_private_journal_failure_keeps_platform_evidence_and_central_stop(self):
        from auto_promotion import native_bridge

        def backend(label, operation, payload, checkpoint, authorize):
            result = {
                "status": "failed_known",
                "confirmed_not_submitted": True,
                "account_block_reason": "x_challenge",
                "reason": "XChallengeRequired",
            }
            checkpoint(result)
            return result

        with (
            patch("auto_promotion.backends.telegram.call", backend),
            patch(
                "auto_promotion.account_blocks.save_private_stop",
                side_effect=OSError("synthetic rename failed"),
            ),
        ):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(result["reason"], "XChallengeRequired")
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(
            self.engine.account(self.account["id"])["outbound_block"]["reason"], "x_challenge"
        )

    def test_both_guard_stores_unavailable_reports_explicit_stop(self):
        import sqlite3

        from auto_promotion import native_bridge

        def backend(label, operation, payload, checkpoint, authorize):
            result = {
                "status": "failed_known",
                "confirmed_not_submitted": True,
                "account_block_reason": "x_challenge",
                "reason": "XChallengeRequired",
            }
            checkpoint(result)
            return result

        with (
            patch("auto_promotion.backends.telegram.call", backend),
            patch(
                "auto_promotion.account_blocks.save_private_stop", side_effect=OSError("synthetic")
            ),
            patch(
                "auto_promotion.account_blocks.observe",
                side_effect=sqlite3.OperationalError("synthetic"),
            ),
        ):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(result["reason"], "XChallengeRequired")
        self.assertTrue(result["guard_persistence_failed"])
        self.assertEqual(result["account_block_reason"], "account_guard_unavailable")

    def test_recovery_bypasses_prior_failed_health_probe_cache(self):
        from auto_promotion.models import digest

        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash=? WHERE id=?",
                (digest(["telegram", "42"]), self.account["id"]),
            )
            observe(
                db,
                self.account["id"],
                {"account_block_reason": "x_challenge"},
                now=self.engine.clock(),
                operation="health",
            )
        with patch.object(
            self.engine,
            "call",
            side_effect=[
                {"healthy": False, "identity_verified": False, "reason": "XChallengeRequired"},
                {"healthy": True, "identity_verified": True, "account_identity": "42"},
            ],
        ) as calls:
            first = self.engine.check_account_block(self.account["id"])
            second = self.engine.check_account_block(self.account["id"])
        self.assertFalse(first["outbound_block"]["health_ok"])
        self.assertTrue(second["outbound_block"]["health_ok"])
        self.assertEqual(calls.call_count, 2)

    def test_two_workers_cannot_submit_when_block_arrives_after_initial_authorize(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from auto_promotion import native_bridge

        entered, release = threading.Event(), threading.Event()
        sends = []

        def backend(label, operation, payload, checkpoint, authorize):
            checkpoint.progress("submission_authorization", False)
            entered.set()
            if not release.wait(3):
                raise AssertionError("synthetic worker timed out")
            authorize()
            sends.append(True)
            raise AssertionError("blocked worker reached submit")

        with (
            patch("auto_promotion.backends.telegram.call", backend),
            ThreadPoolExecutor(max_workers=1) as workers,
        ):
            future = workers.submit(native_bridge.dispatch, self.request)
            try:
                self.assertTrue(entered.wait(3))
                with self.store.connect(write=True) as db:
                    observe(
                        db,
                        self.account["id"],
                        {"account_block_reason": "x_challenge"},
                        now=self.engine.clock(),
                        operation="health",
                    )
            finally:
                release.set()
            result = future.result()
        self.assertEqual(sends, [])
        self.assertEqual(result["reason"], "AccountOutboundBlocked")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)
