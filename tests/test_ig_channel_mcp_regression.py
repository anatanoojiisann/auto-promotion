import asyncio
import sqlite3
import unittest
from unittest.mock import patch

import test_local_authorization as fixtures
from mcp.server.fastmcp.exceptions import ToolError
from test_api import request

from auto_promotion.api import create_app
from auto_promotion.mcp_server import create_server


class ChannelMcpRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LocalAuthorizationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_locked_database_reports_safe_specific_error_without_attempt(self):
        f = self.fixture
        server = create_server(f.engine)
        error = sqlite3.OperationalError("secret-CANARY")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with (
            patch("auto_promotion.local_authorization.authorize_local_channel", side_effect=error),
            self.assertRaises(ToolError) as caught,
        ):
            asyncio.run(
                server.call_tool(
                    "authorize_local_channel",
                    {
                        "task_id": f.task["id"],
                        "account_id": f.aid,
                        "coordinate_once": True,
                        "source": "synthetic explicit instruction",
                    },
                )
            )
        self.assertIn("LocalDatabaseUnavailable", str(caught.exception))
        self.assertNotIn("CANARY", str(caught.exception))
        f.assert_no_attempts()

    def test_workbench_locked_database_reports_safe_specific_error_without_attempt(self):
        f = self.fixture
        app = create_app(
            db_path=f.store.path,
            registry=f.engine.adapters,
            clock=f.engine.clock,
            token="synthetic-api",
        )
        error = sqlite3.OperationalError("secret-CANARY")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch("auto_promotion.api.authorize_local_channel", side_effect=error):
            status, result, _ = asyncio.run(
                request(
                    app,
                    "/api/tasks/" + f.task["id"] + "/authorize-local-channel",
                    "POST",
                    {"account_id": f.aid, "coordinate_once": True, "source": "synthetic"},
                    {"authorization": "Bearer synthetic-api"},
                )
            )
        self.assertEqual(status, 409)
        self.assertEqual(result, {"error": "LocalDatabaseUnavailable", "authorization_only": True})
        self.assertNotIn("CANARY", str(result))
        f.assert_no_attempts()


if __name__ == "__main__":
    unittest.main()
