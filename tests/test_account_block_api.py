"""Account recovery HTTP contract; all health checks are mocked."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Conflict


class AccountBlockAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            db_path=Path(self.tmp.name) / "test.db",
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="test-key",
        )

    def call(self, suffix, body=None, auth=True):
        return asyncio.run(
            request(
                self.app,
                "/api/accounts/test-account/outbound-block/" + suffix,
                "POST",
                body,
                {"authorization": "Bearer test-key"} if auth else {},
            )
        )

    def test_check_is_authenticated_and_calls_only_recovery_health_check(self):
        expected = {"id": "test-account", "outbound_block": {"health_ok": True}}
        with patch.object(
            self.app.state.engine, "check_account_block", create=True, return_value=expected
        ) as check:
            self.assertEqual(self.call("check", auth=False)[0], 401)
            self.assertEqual(self.call("check")[:2], (200, expected))
            check.assert_called_once_with("test-account")

    def test_resume_requires_strict_generation_and_never_checks_implicitly(self):
        expected = {"id": "test-account", "outbound_block": None}
        with patch.object(
            self.app.state.engine, "resume_account_block", create=True, return_value=expected
        ) as resume:
            for body in (
                {},
                {"generation": ""},
                {"generation": "3"},
                {"generation": True},
                {"generation": 0},
                {"generation": 1, "force": True},
            ):
                self.assertEqual(self.call("resume", body)[0], 422)
            self.assertEqual(self.call("resume", {"generation": 1})[:2], (200, expected))
            resume.assert_called_once_with("test-account", 1)

    def test_stale_or_unhealthy_recovery_remains_conflict(self):
        with patch.object(
            self.app.state.engine,
            "resume_account_block",
            create=True,
            side_effect=Conflict("AccountBlockHealthRequired"),
        ):
            self.assertEqual(self.call("resume", {"generation": 2})[0], 409)
