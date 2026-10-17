import asyncio
import json
import os
import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_api import request

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.api import create_app
from auto_promotion.backends.browser import call as browser_call
from auto_promotion.backends.browser import launch_context
from auto_promotion.backends.common import BackendError, profile_path, read_profile, save_profile
from auto_promotion.connections import ConnectionInput, ConnectionManager
from auto_promotion.core import Conflict, Engine
from auto_promotion.store import Store


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(self.root / "profiles")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.engine = Engine(
            Store(self.root / "test.db"),
            AdapterRegistry(
                self.root, overrides={"instagram": SimulationAdapter(), "x": SimulationAdapter()}
            ),
        )
        self.manager = ConnectionManager(self.engine)
        self.thread = patch("auto_promotion.connections.threading.Thread")
        self.thread.start()
        self.addCleanup(self.thread.stop)

    def value(self, **changes):
        return ConnectionInput(
            **{
                "platform": "instagram",
                "username": "brand",
                "access_token": "private-test-token",
                "api_version": "v25.0",
                **changes,
            }
        )

    def graph(self, **observed):
        graph = MagicMock()
        graph.request.return_value = {"user_id": "700", "username": "brand", **observed}
        return patch("auto_promotion.backends.instagram.Graph", return_value=graph)

    def run_job(self, value):
        job = self.manager.start(value)
        self.manager.run(job["id"], value, self.manager.cancellations[job["id"]])
        return self.manager.get(job["id"])

    def test_verified_login_registers_live_account_once_without_enabling_writes(self):
        with self.graph():
            job = self.run_job(self.value())
            second = self.run_job(self.value())
        self.assertEqual(job["stage"], "connected")
        self.assertEqual(job["account_id"], second["account_id"])
        self.assertEqual(len(self.engine.accounts()), 1)
        self.assertEqual(self.engine.accounts()[0]["mode"], "live")
        self.assertFalse(self.engine.accounts()[0]["writable"])
        profile = read_profile(
            "instagram", "instagram-brand", accounts_dir=self.manager.accounts_dir
        )
        self.assertEqual(profile["identity"], "700")
        output = json.dumps(self.manager.list()) + json.dumps(self.engine.accounts())
        output += json.dumps(self.engine.store.rows("SELECT * FROM audit"))
        self.assertNotIn("private-test-token", output)
        self.assertEqual(job["instagram_user_id"], "700")

    def test_wrong_instagram_account_cannot_save_or_register(self):
        with self.graph(username="other"):
            job = self.run_job(self.value())
        self.assertEqual(job["reason"], "InstagramIdentityMismatch")
        self.assertEqual(self.engine.accounts(), [])
        self.assertFalse(
            (
                profile_path("instagram", "instagram-brand", accounts_dir=self.manager.accounts_dir)
                / "credentials.json"
            ).exists()
        )

    def test_identity_change_preserves_existing_profile(self):
        save_profile(
            "instagram",
            "instagram-brand",
            {"identity": "700"},
            accounts_dir=self.manager.accounts_dir,
        )
        with self.graph(user_id="701"):
            job = self.run_job(self.value())
        self.assertEqual(job["reason"], "ChangedIdentityRequiresNewAccountLabel")
        self.assertEqual(
            read_profile("instagram", "instagram-brand", accounts_dir=self.manager.accounts_dir)[
                "identity"
            ],
            "700",
        )
        self.assertEqual(self.engine.accounts(), [])

    def test_exception_payload_does_not_reveal_token(self):
        with patch(
            "auto_promotion.backends.instagram.Graph",
            side_effect=RuntimeError("private-test-token"),
        ):
            value = self.value()
            job = self.run_job(value)
        self.assertEqual(job["reason"], "RuntimeError")
        self.assertNotIn("private-test-token", json.dumps(job))
        self.assertIsNone(value.access_token)

    def test_duplicate_active_login_and_maximum_concurrency(self):
        value = self.value()
        self.manager.start(value)
        with self.assertRaises(Conflict):
            self.manager.start(self.value())
        self.manager.start(self.value(username="brand2"))
        with self.assertRaises(Conflict):
            self.manager.start(self.value(username="brand3"))

    def test_connection_history_survives_restart_without_secrets(self):
        with self.graph():
            job = self.run_job(self.value())
        restarted = ConnectionManager(self.engine)
        restored = restarted.get(job["id"])
        self.assertEqual(restored["stage"], "connected")
        self.assertEqual(restored["account_id"], job["account_id"])
        persisted = self.engine.store.rows("SELECT * FROM connection_jobs")
        self.assertNotIn("private-test-token", json.dumps(persisted))

    def test_incomplete_connection_is_marked_interrupted_after_restart(self):
        job = self.manager.start(self.value())
        restarted = ConnectionManager(self.engine)
        self.assertEqual(restarted.get(job["id"])["stage"], "interrupted")

    def test_cancelled_job_does_not_save_or_register(self):
        value = self.value()
        job = self.manager.start(value)
        self.manager.cancel(job["id"])
        with self.graph() as factory:
            self.manager.run(job["id"], value, self.manager.cancellations[job["id"]])
        factory.assert_not_called()
        self.assertEqual(self.manager.get(job["id"])["stage"], "cancelled")
        self.assertEqual(self.engine.accounts(), [])

    def test_saved_login_is_not_connected_until_health_check_passes(self):
        with (
            self.graph(),
            patch.object(
                self.engine,
                "check_account",
                return_value={
                    "id": "a",
                    "status": "unavailable",
                    "health": {"reason": "connection_unavailable"},
                },
            ),
        ):
            job = self.run_job(self.value())
        self.assertEqual(job["stage"], "needs_check")

    def test_explicit_database_keeps_profiles_beside_that_database(self):
        with patch.dict(os.environ, {}, clear=True):
            manager = ConnectionManager(self.engine)
        self.assertEqual(manager.accounts_dir, self.root / "accounts")

    @unittest.skipUnless(find_spec("playwright"), "browser extra is not installed")
    def test_wrong_x_identity_does_not_save(self):
        # An offline browser double exercises the interactive login loop without network.
        runtime = MagicMock()
        context = runtime.chromium.launch_persistent_context.return_value
        page = context.pages = [MagicMock()]
        page = page[0]
        page.is_closed.return_value = False
        page.get_by_test_id.return_value.count.return_value = 1
        page.get_by_test_id.return_value.first.is_visible.return_value = True
        value = ConnectionInput(platform="x", username="brand")
        job = self.manager.start(value)
        with (
            patch("playwright.sync_api.sync_playwright") as factory,
            patch("auto_promotion.backends.browser.identity", return_value="other"),
        ):
            factory.return_value.__enter__.return_value = runtime
            with self.assertRaises(BackendError) as exc:
                self.manager.x_login(job["id"], value, threading.Event())
        self.assertEqual(exc.exception.safe_code, "BrowserIdentityMismatch")
        context.close.assert_called_once()

    @unittest.skipUnless(find_spec("playwright"), "browser extra is not installed")
    def test_edge_login_and_subsequent_health_use_same_browser_profile(self):
        self.check_installed_browser_login_and_health("msedge")

    @unittest.skipUnless(find_spec("playwright"), "browser extra is not installed")
    def test_chrome_login_and_subsequent_health_use_same_isolated_profile(self):
        self.check_installed_browser_login_and_health("chrome")

    def check_installed_browser_login_and_health(self, channel):
        runtime = MagicMock()
        context = runtime.chromium.launch_persistent_context.return_value
        context.pages = [MagicMock()]
        page = context.pages[0]
        page.is_closed.return_value = False
        page.get_by_test_id.return_value.count.return_value = 1
        page.get_by_test_id.return_value.first.is_visible.return_value = True
        value = ConnectionInput(platform="x", username="brand", browser_channel=channel)
        job = self.manager.start(value)
        with (
            patch("playwright.sync_api.sync_playwright") as factory,
            patch("auto_promotion.backends.browser.identity", return_value="brand"),
            patch("auto_promotion.backends.browser.BrowserBackend") as backend,
        ):
            factory.return_value.__enter__.return_value = runtime
            self.manager.x_login(job["id"], value, threading.Event())
            settings = read_profile("x", "x-brand", accounts_dir=self.manager.accounts_dir)
            self.assertEqual(settings["browser_channel"], channel)
            self.assertEqual(settings["identity"], "brand")
            browser_call("x", "x-brand", "health", {}, MagicMock(), MagicMock())
            launches = runtime.chromium.launch_persistent_context.call_args_list
            self.assertEqual(len(launches), 2)
            expected_path = str(
                profile_path("x", "x-brand", accounts_dir=self.manager.accounts_dir)
                / ("browser-" + channel)
            )
            for index, launch in enumerate(launches):
                self.assertEqual(launch.args[0], expected_path)
                self.assertEqual(launch.kwargs["channel"], channel)
                self.assertEqual(launch.kwargs["headless"], bool(index))
            self.assertEqual(backend.call_args.args[-1], settings)

    def test_existing_chromium_profile_path_is_preserved(self):
        runtime = MagicMock()
        launch_context(runtime, self.root, headless=True)
        launch = runtime.chromium.launch_persistent_context.call_args
        self.assertEqual(launch.args[0], str(self.root / "browser"))
        self.assertNotIn("channel", launch.kwargs)
        with self.assertRaises(BackendError):
            launch_context(runtime, self.root, browser_channel="unexpected", headless=True)


class ConnectionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(db_path=Path(self.tmp.name) / "test.db", token="test-key")

    def call(self, path="/api/connections", method="GET", body=None, headers=None):
        return asyncio.run(
            request(self.app, path, method, body, headers or {"authorization": "Bearer test-key"})
        )

    def test_connection_routes_are_private_and_reject_cross_origin(self):
        self.assertEqual(self.call(headers={"origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call(headers={"x-ap-client": "local-ui"})[0], 401)
        self.assertEqual(self.call()[1], [])

    def test_invalid_connection_never_echoes_submitted_secrets(self):
        for payload in (
            {
                "platform": "instagram",
                "username": "brand",
                "access_token": "private-test-token",
                "api_version": "invalid",
            },
            {"platform": "x", "username": "brand", "password": "private-test-password"},
            {"platform": "x", "username": "brand", "access_token": "private-test-token"},
        ):
            status, body, _ = self.call(method="POST", body=payload)
            self.assertEqual(status, 422)
            self.assertNotIn("private-test", json.dumps(body))

    def test_start_returns_safe_status_and_unknown_job_is_404(self):
        with patch("auto_promotion.connections.threading.Thread"):
            status, job, _ = self.call(method="POST", body={"platform": "x", "username": "@Brand"})
        self.assertEqual(status, 202)
        self.assertEqual(job["username"], "brand")
        self.assertEqual(self.call("/api/connections/" + job["id"])[1]["stage"], "connecting")
        self.assertEqual(self.call("/api/connections/missing")[0], 404)
        self.assertEqual(self.call("/api/connections/" + job["id"] + "/cancel", "POST")[0], 200)

    def test_edge_selection_is_retained_and_unknown_channel_is_rejected(self):
        with patch("auto_promotion.connections.threading.Thread"):
            status, job, _ = self.call(
                method="POST",
                body={"platform": "x", "username": "brand", "browser_channel": "msedge"},
            )
        self.assertEqual(status, 202)
        self.assertEqual(job["browser_channel"], "msedge")
        self.assertEqual(
            self.call(
                method="POST",
                body={"platform": "x", "username": "brand", "browser_channel": "unexpected"},
            )[0],
            422,
        )
