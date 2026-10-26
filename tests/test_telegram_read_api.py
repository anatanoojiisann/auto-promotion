"""Local Telegram read policy routes must not probe an account or expose secrets."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Conflict, NotFound

POLICY = {
    "requests_per_window": 120,
    "window_seconds": 3600,
    "scan_rpc_limit": 40,
    "messages_per_page": 100,
    "groups_per_scan": 10,
    "cache_ttl_seconds": 300,
}


class TelegramReadAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            db_path=Path(self.tmp.name) / "test.db",
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="test-key",
        )
        self.engine = self.app.state.engine

    def call(self, method="GET", body=None, auth=True, revision="0"):
        headers = {"authorization": "Bearer test-key"} if auth else {}
        if revision is not None:
            headers["if-match"] = revision
        return asyncio.run(
            request(
                self.app,
                "/api/accounts/local-account/telegram-reads",
                method,
                body,
                headers,
            )
        )

    def test_read_requires_authentication_and_never_probes(self):
        with (
            patch.object(
                self.engine,
                "telegram_read_policy",
                create=True,
                return_value={"account_id": "local-account", "policy": POLICY, "revision": 0},
            ) as read,
            patch.object(self.engine, "check_account", side_effect=AssertionError("no probe")),
        ):
            self.assertEqual(self.call(auth=False)[0], 401)
            self.assertEqual(
                self.call()[:2],
                (200, {"account_id": "local-account", "policy": POLICY, "revision": 0}),
            )
            read.assert_called_once_with("local-account")

    def test_save_passes_validated_policy_without_probe(self):
        with (
            patch.object(
                self.engine,
                "configure_telegram_reads",
                create=True,
                return_value={"account_id": "local-account", "policy": POLICY, "revision": 0},
            ) as save,
            patch.object(self.engine, "check_account", side_effect=AssertionError("no probe")),
        ):
            self.assertEqual(self.call("PUT", POLICY, auth=False)[0], 401)
            self.assertEqual(
                self.call("PUT", POLICY)[:2],
                (200, {"account_id": "local-account", "policy": POLICY, "revision": 0}),
            )
            save.assert_called_once()
            account_id, value = save.call_args.args
            self.assertEqual(account_id, "local-account")
            self.assertEqual(value.model_dump(), POLICY)
            self.assertEqual(save.call_args.kwargs, {"expected_revision": 0})

    def test_unknown_identity_or_credentials_cannot_be_submitted(self):
        with patch.object(self.engine, "configure_telegram_reads", create=True) as save:
            for extra in ("identity_hash", "access_hash", "session", "force"):
                self.assertEqual(self.call("PUT", {**POLICY, extra: "forbidden"})[0], 422)
            save.assert_not_called()

    def test_invalid_budgets_are_rejected_without_core_call(self):
        with patch.object(self.engine, "configure_telegram_reads", create=True) as save:
            for key in POLICY:
                for value in (-1, "120", True, 1.5, 10**12):
                    with self.subTest(key=key, value=value):
                        self.assertEqual(self.call("PUT", {**POLICY, key: value})[0], 422)
            save.assert_not_called()

    def test_wrong_platform_and_missing_account_preserve_core_rejection(self):
        for error, status in (
            (Conflict("TelegramAccountRequired"), 409),
            (NotFound("AccountNotFound"), 404),
        ):
            with patch.object(self.engine, "telegram_read_policy", create=True, side_effect=error):
                self.assertEqual(self.call()[0], status)

    def test_revision_header_is_strict_and_stale_edits_remain_conflicts(self):
        with patch.object(
            self.engine,
            "configure_telegram_reads",
            create=True,
            side_effect=Conflict("TelegramReadPolicyRevisionChanged"),
        ) as save:
            for revision in ("", "-1", "1.0", "true", "00", "1e3", " 1", str(2**64)):
                self.assertEqual(self.call("PUT", POLICY, revision=revision)[0], 422)
            save.assert_not_called()
            self.assertEqual(self.call("PUT", POLICY, revision="1")[0], 409)
            self.assertEqual(save.call_args.kwargs, {"expected_revision": 1})

    def test_response_excludes_private_fields(self):
        raw = {
            "account_id": "local-account",
            "policy": {**POLICY, "access_hash": "synthetic-private"},
            "revision": 0,
            "session": "synthetic-private",
        }
        with patch.object(self.engine, "telegram_read_policy", create=True, return_value=raw):
            self.assertEqual(
                self.call()[:2],
                (200, {"account_id": "local-account", "policy": POLICY, "revision": 0}),
            )

    def test_local_round_trip_fences_stale_save_without_health_probe(self):
        from auto_promotion.models import AccountInput

        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Synthetic",
                backend_label="fixture-only",
                mode="simulation",
            )
        )
        path = f"/api/accounts/{account['id']}/telegram-reads"
        headers = {"authorization": "Bearer test-key", "if-match": "0"}
        with patch.object(self.engine, "check_account", side_effect=AssertionError("no probe")):
            status, before, _ = asyncio.run(request(self.app, path, headers=headers))
            self.assertEqual(status, 200)
            self.assertEqual(before, {"account_id": account["id"], "policy": POLICY, "revision": 0})
            changed = {**POLICY, "requests_per_window": 80}
            status, after, _ = asyncio.run(request(self.app, path, "PUT", changed, headers))
            self.assertEqual(status, 200)
            self.assertEqual(after["policy"], changed)
            self.assertEqual(after["revision"], 1)
            self.assertEqual(asyncio.run(request(self.app, path, "PUT", POLICY, headers))[0], 409)
            self.assertEqual(
                asyncio.run(
                    request(self.app, path, "PUT", POLICY, {"authorization": "Bearer test-key"})
                )[0],
                409,
            )
            self.assertEqual(
                self.engine.account(account["id"])["checked_at"], account["checked_at"]
            )

    def test_non_telegram_local_account_cannot_read_or_save_policy(self):
        from auto_promotion.models import AccountInput

        account = self.engine.add_account(
            AccountInput(
                platform="x", label="Synthetic X", backend_label="fixture-only-x", mode="simulation"
            )
        )
        path = f"/api/accounts/{account['id']}/telegram-reads"
        headers = {"authorization": "Bearer test-key", "if-match": "0"}
        with patch.object(self.engine, "check_account", side_effect=AssertionError("no probe")):
            self.assertEqual(asyncio.run(request(self.app, path, headers=headers))[0], 409)
            self.assertEqual(asyncio.run(request(self.app, path, "PUT", POLICY, headers))[0], 409)

    def test_api_upper_bounds_match_offline_reader_policy(self):
        from auto_promotion.backends.telegram_reads import read_policy
        from auto_promotion.models import AccountInput
        from auto_promotion.telegram_reading import read_policy_context

        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Synthetic bounds",
                backend_label="fixture-bounds",
                mode="simulation",
            )
        )
        path = f"/api/accounts/{account['id']}/telegram-reads"
        headers = {"authorization": "Bearer test-key", "if-match": "0"}
        maximum = {
            "requests_per_window": 10000,
            "window_seconds": 86400,
            "scan_rpc_limit": 1000,
            "messages_per_page": 100,
            "groups_per_scan": 100,
            "cache_ttl_seconds": 3600,
        }
        status, saved, _ = asyncio.run(request(self.app, path, "PUT", maximum, headers))
        self.assertEqual(status, 200)
        loaded = self.engine.telegram_read_policy(account["id"])
        context = read_policy_context(
            {"database": self.engine.store.path, "account_id": account["id"]}
        )
        self.assertEqual(loaded["policy"], maximum)
        self.assertEqual(context["policy"], maximum)
        self.assertEqual(read_policy(loaded["policy"]), maximum)
        self.assertEqual(saved["revision"], 1)
        headers["if-match"] = "1"
        for key, value in maximum.items():
            with self.subTest(key=key):
                self.assertEqual(
                    asyncio.run(
                        request(self.app, path, "PUT", {**maximum, key: value + 1}, headers)
                    )[0],
                    422,
                )
