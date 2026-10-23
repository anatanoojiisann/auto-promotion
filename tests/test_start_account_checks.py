"""Task starts use current read-only evidence; no real accounts or network."""

import asyncio
import json
import os
import socket
import unittest
from unittest.mock import patch

import test_engine as fixtures
from test_api import request

from auto_promotion.api import create_app
from auto_promotion.backends.common import BackendError
from auto_promotion.core import Conflict
from auto_promotion.dispatcher import AccountBusy


class StartAccountChecksTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        for context in (
            patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.f.tmp.name + "/accounts"}),
            patch.object(socket, "create_connection", side_effect=AssertionError("offline")),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.engine, self.store = self.f.engine, self.f.store
        self.account_id = self.f.accounts["telegram"]["id"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live',min_interval=120")
        self.engine.check_account(self.account_id, force=True)
        self.task = self.f.task(min_interval=120)
        self.engine.set_items(self.task["id"], [self.f.item()])

    def assert_untouched(self, state="draft"):
        task = self.engine.task(self.task["id"])
        self.assertEqual(task["state"], state)
        self.assertIsNone(task["manifest_hash"])
        self.assertEqual(task["items"][0]["status"], "not_attempted")
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(self.store.rows("SELECT * FROM join_attempts"), [])
        self.assertEqual(self.store.rows("SELECT * FROM execution_jobs"), [])
        self.assertEqual(self.f.provider.sends, 0)

    def test_explicit_start_refreshes_even_a_recent_positive_probe(self):
        original = self.f.provider.call
        with patch.object(self.f.provider, "call", side_effect=original) as call:
            task = self.engine.start_task(self.task["id"], source="codex")
        self.assertEqual([c.args[1] for c in call.call_args_list], ["health"])
        self.assertEqual(task["phase"], "ready")
        self.assertTrue(task["readiness"]["accounts_ready"])
        self.assertEqual(self.f.provider.sends, 0)

    def test_verification_blocks_start_before_freeze_queue_or_attempt(self):
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "healthy": True,
                "identity_verified": True,
                "challenge_required": True,
            },
        ) as call:
            with self.assertRaisesRegex(Conflict, "telegram.*人机验证"):
                self.engine.start_and_run(self.task["id"], source="codex")
            with self.assertRaisesRegex(Conflict, "人机验证"):
                self.engine.start_task(self.task["id"], sending=False)
        self.assertEqual(call.call_count, 1, "failed checks keep their backoff")
        self.assert_untouched()
        checks = self.engine.task(self.task["id"])["readiness"]["accounts"]
        self.assertEqual(checks[0]["state"], "verification_required")
        audit = self.store.one("SELECT details FROM audit WHERE kind='task.account_check'")
        self.assertFalse(json.loads(audit["details"])["accounts"][0]["ready"])

    def test_restriction_or_unavailable_probe_cannot_use_old_healthy_state(self):
        for result in (
            {"healthy": True, "identity_verified": True, "restricted": True},
            {"healthy": False, "reason": "BrowserLoginRequired"},
        ):
            with self.subTest(result=result):
                self.f.now += 301
                with patch.object(self.f.provider, "call", return_value=result):
                    with self.assertRaisesRegex(Conflict, "启动前账号检查未通过"):
                        self.engine.start_task(self.task["id"])
                self.assert_untouched()

    def test_timeout_blocks_without_reclassifying_as_logged_out(self):
        with patch.object(self.f.provider, "call", side_effect=BackendError("TimeoutError")):
            with self.assertRaisesRegex(Conflict, "暂时无法确认"):
                self.engine.start_task(self.task["id"])
        self.assertEqual(
            self.engine.account(self.account_id)["connection_state"], "temporary_unavailable"
        )
        self.assert_untouched()

    def test_disabled_and_cooling_accounts_are_not_probed(self):
        for sql, message in (
            ("disabled=1", "停用"),
            (f"disabled=0,cooldown_until={self.f.now + 3600}", "冷却"),
        ):
            with self.subTest(message=message):
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE accounts SET " + sql + " WHERE id=?", (self.account_id,))
                with patch.object(self.f.provider, "call") as call:
                    with self.assertRaisesRegex(Conflict, message):
                        self.engine.start_task(self.task["id"], sending=False)
                call.assert_not_called()
                self.assert_untouched()

    def test_busy_account_blocks_even_if_last_saved_health_was_positive(self):
        with patch.object(self.engine.lanes, "acquire", side_effect=AccountBusy):
            with self.assertRaisesRegex(Conflict, "正在执行其他操作"):
                self.engine.start_task(self.task["id"])
        self.assert_untouched()

    def test_resume_checks_accounts_and_preserves_frozen_manifest_on_failure(self):
        started = self.engine.start_task(self.task["id"])
        self.engine.change_state(self.task["id"], "paused")
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "healthy": False,
                "reason": "BrowserVerificationRequired",
            },
        ):
            with self.assertRaisesRegex(Conflict, "人机验证"):
                self.engine.change_state(self.task["id"], "active")
        task = self.engine.task(self.task["id"])
        self.assertEqual(task["state"], "paused")
        self.assertEqual(task["manifest_hash"], started["manifest_hash"])
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])

    def test_state_change_after_probe_is_rechecked_before_task_activation(self):
        original = self.engine._check_start_accounts

        def checked(*args, **kwargs):
            original(*args, **kwargs)
            self.engine.disable_account(self.account_id, True)

        with patch.object(self.engine, "_check_start_accounts", side_effect=checked):
            with self.assertRaisesRegex(Conflict, "状态已变化"):
                self.engine.start_task(self.task["id"])
        self.assert_untouched()

    def test_every_selected_account_is_checked_and_all_blockers_are_reported(self):
        task = self.f.task(["telegram", "x"], min_interval=120)
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "healthy": False,
                "reason": "BrowserVerificationRequired",
            },
        ) as call:
            with self.assertRaisesRegex(Conflict, "telegram.*x"):
                self.engine.start_task(task["id"], sending=False)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.engine.task(task["id"])["state"], "draft")

    def test_recovery_after_failed_probe_backoff_requires_a_new_start(self):
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "healthy": False,
                "reason": "BrowserVerificationRequired",
            },
        ):
            with self.assertRaises(Conflict):
                self.engine.start_task(self.task["id"])
        self.f.now += 31
        self.engine.check_account(self.account_id)
        self.assert_untouched()
        self.assertEqual(self.engine.start_task(self.task["id"])["phase"], "ready")

    def test_client_start_join_and_resume_return_readable_conflicts(self):
        app = create_app(
            db_path=self.store.path,
            registry=self.f.registry,
            clock=lambda: self.f.now,
            token="offline-test-key",
        )
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "healthy": False,
                "reason": "BrowserVerificationRequired",
            },
        ):
            for action, body in (("start", None), ("join", None), ("state", {"state": "active"})):
                with self.subTest(action=action):
                    status, result, _ = asyncio.run(
                        request(
                            app,
                            f"/api/tasks/{self.task['id']}/{action}",
                            "POST",
                            body,
                            {"authorization": "Bearer offline-test-key"},
                        )
                    )
                    self.assertEqual(status, 409)
                    self.assertIn("人机验证", result["error"])
                    self.assert_untouched()
