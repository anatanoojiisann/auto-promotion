import io
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_local_authorization as authorization_fixtures
import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable, export_snapshot
from auto_promotion.models import AccountInput
from auto_promotion.task_tools import TaskTools


class Response(io.BytesIO):
    status = 200


class ChannelSyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine = self.fixture.engine
        self.store = self.fixture.store
        self.task, self.account = self.fixture.ready()
        self.cloud = CloudControl(
            "http://127.0.0.1:8791",
            node_id="synthetic",
            node_token="synthetic-node",
            operator_token="synthetic-operator",
        )

    @staticmethod
    def locked():
        error = sqlite3.OperationalError("private-path-CANARY")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        error.db_phase = "write_lock"
        error.lock_wait_ms = 123
        return error

    def test_empty_block_list_does_not_take_a_write_lock(self):
        engine = SimpleNamespace(store=SimpleNamespace(connect=lambda **_: self.fail("write lock")))
        CloudControl.import_account_blocks(engine, {"account_blocks": []})

    def test_local_lock_after_http_success_is_not_reported_as_transport_failure(self):
        self.cloud._block_engine = self.engine
        body = json.dumps({"account_blocks": [{"account_id": self.account["id"]}]}).encode()
        with (
            patch("auto_promotion.cloud_control.urlopen", return_value=Response(body)),
            patch.object(self.cloud, "import_account_blocks", side_effect=self.locked()),
            self.assertRaises(CloudUnavailable) as caught,
        ):
            self.cloud.request("/v1/heartbeat", {})
        self.assertEqual(str(caught.exception), "LocalDatabaseUnavailable")
        self.assertEqual(caught.exception.diagnostics["cause"], "sqlite_busy")
        self.assertTrue(caught.exception.diagnostics["remote_response_received"])
        self.assertIsNone(self.cloud.last_contact_monotonic)
        self.assertNotIn("CANARY", json.dumps(caught.exception.diagnostics))

    def test_malformed_local_error_metadata_is_sanitized_and_remains_fail_closed(self):
        error = sqlite3.OperationalError("secret-CANARY")
        error.sqlite_errorcode = "invalid-CANARY"
        error.db_phase = []
        error.lock_wait_ms = "invalid-CANARY"
        self.cloud._block_engine = self.engine
        body = json.dumps({"account_blocks": [{"account_id": self.account["id"]}]}).encode()
        with (
            patch("auto_promotion.cloud_control.urlopen", return_value=Response(body)),
            patch.object(self.cloud, "import_account_blocks", side_effect=error),
            self.assertRaises(CloudUnavailable) as caught,
        ):
            self.cloud.request("/v1/heartbeat", {})
        self.assertEqual(str(caught.exception), "LocalDatabaseUnavailable")
        self.assertEqual(caught.exception.diagnostics["cause"], "sqlite_error")
        self.assertNotIn("CANARY", json.dumps(caught.exception.diagnostics))

    def test_task_scoped_sync_filters_before_import_and_imports_once(self):
        response = {
            "account_blocks": [
                {"account_id": self.account["id"]},
                {"account_id": "unrelated-account"},
            ]
        }
        imports = []
        with (
            patch(
                "auto_promotion.cloud_control.urlopen",
                side_effect=lambda *a, **k: Response(json.dumps(response).encode()),
            ) as transport,
            patch.object(
                self.cloud,
                "import_account_blocks",
                side_effect=lambda engine, result: imports.append(result["account_blocks"]),
            ),
        ):
            result = self.cloud._sync(
                self.engine,
                task_ids=[self.task["id"]],
                authorization_account=self.account["id"],
            )
        self.assertGreater(result["changed_records"], 0)
        self.assertTrue(imports)
        self.assertEqual(len(imports), transport.call_count)
        self.assertTrue(
            all([r["account_id"] for r in rows] == [self.account["id"]] for rows in imports)
        )

    def test_scoped_sync_applies_selected_block_without_touching_other_account(self):
        other = self.engine.add_account(
            AccountInput(
                platform="x", label="Other test", backend_label="other-test", mode="simulation"
            )
        )
        accounts = {a["id"]: a for a in export_snapshot(self.engine)["accounts"]}
        block = {
            "state": "active",
            "reason": "x_challenge",
            "since": self.engine.clock(),
            "until": None,
            "generation": 7,
        }
        response = {
            "account_blocks": [
                {
                    "account_id": a["id"],
                    "identity": accounts[a["id"]]["identity"],
                    "outbound_block_state": block,
                }
                for a in (self.account, other)
            ]
        }
        with patch(
            "auto_promotion.cloud_control.urlopen",
            side_effect=lambda *a, **k: Response(json.dumps(response).encode()),
        ):
            self.cloud._sync(
                self.engine,
                task_ids=[self.task["id"]],
                authorization_account=self.account["id"],
            )
        self.assertEqual(self.engine.account(self.account["id"])["outbound_block"]["generation"], 7)
        self.assertIsNone(self.engine.account(other["id"])["outbound_block"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_scoped_import_lock_does_not_acknowledge_remote_snapshot(self):
        response = {"account_blocks": [{"account_id": self.account["id"]}]}
        with (
            patch(
                "auto_promotion.cloud_control.urlopen",
                side_effect=lambda *a, **k: Response(json.dumps(response).encode()),
            ),
            patch.object(self.cloud, "import_account_blocks", side_effect=self.locked()),
            self.assertRaises(CloudUnavailable) as caught,
        ):
            self.cloud._sync(
                self.engine,
                task_ids=[self.task["id"]],
                authorization_account=self.account["id"],
            )
        self.assertEqual(str(caught.exception), "LocalDatabaseUnavailable")
        self.assertTrue(caught.exception.diagnostics["remote_response_received"])
        self.assertEqual(caught.exception.diagnostics["sync_scope"], "task")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM cloud_sync_records")["n"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)


class ScopedHandoffAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = authorization_fixtures.LocalAuthorizationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_local_import_lock_is_exported_without_authorizing_or_sending(self):
        f = self.fixture
        error = sqlite3.OperationalError("secret-CANARY")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with (
            patch.object(f.cloud, "import_account_blocks", side_effect=error),
            self.assertRaises(CloudUnavailable) as caught,
        ):
            f.authorize()
        self.assertEqual(str(caught.exception), "LocalDatabaseUnavailable")
        self.assertEqual(
            f.store.one("SELECT state FROM account_task_channels WHERE account_id=?", (f.aid,))[
                "state"
            ],
            "handoff",
        )
        evidence = TaskTools(f.engine).evidence(f.task["id"])
        failure = next(a for a in evidence["audit"] if a["kind"] == "execution.channel_failed")
        self.assertEqual(failure["details"]["reason"], "LocalDatabaseUnavailable")
        self.assertEqual(failure["details"]["execution_step"], "account_block_import")
        self.assertEqual(failure["details"]["exception_type"], "SQLiteError")
        self.assertNotIn("CANARY", json.dumps(evidence))
        f.assert_no_attempts()


if __name__ == "__main__":
    unittest.main()
