import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_promotion.backends import browser
from auto_promotion.backends.common import BackendError, read_profile, save_profile


class BrowserSessionReconnectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_same_connection_preserves_commissioned_actions_and_browser_mode(self):
        save_profile(
            "x",
            "brand",
            {
                "identity": "brand",
                "browser_channel": "msedge",
                "automatic_actions": ["reply"],
                "browser_headless": False,
            },
        )
        browser.save_browser_login("x", "brand", "brand", "msedge")
        settings = read_profile("x", "brand")
        self.assertEqual(settings["automatic_actions"], ["reply"])
        self.assertIs(settings["browser_headless"], False)

    def test_new_connection_does_not_enable_automatic_actions(self):
        browser.save_browser_login("x", "brand", "brand", "msedge")
        self.assertNotIn("automatic_actions", read_profile("x", "brand"))

    def test_channel_change_does_not_inherit_another_connections_opt_ins(self):
        save_profile(
            "x",
            "brand",
            {
                "identity": "brand",
                "browser_channel": "browser_use",
                "automatic_actions": ["reply"],
                "browser_headless": False,
                "browser_use_observation": {"private": "observation"},
            },
        )
        browser.save_browser_login("x", "brand", "brand", "msedge")
        self.assertEqual(
            read_profile("x", "brand"),
            {"label": "brand", "identity": "brand", "browser_channel": "msedge"},
        )

    def test_different_identity_cannot_replace_the_bound_connection(self):
        save_profile(
            "x",
            "brand",
            {"identity": "brand", "browser_channel": "msedge", "automatic_actions": ["reply"]},
        )
        before = read_profile("x", "brand")
        with self.assertRaises(BackendError) as caught:
            browser.save_browser_login("x", "brand", "other", "msedge")
        self.assertEqual(caught.exception.safe_code, "ChangedIdentityRequiresNewAccountLabel")
        self.assertEqual(read_profile("x", "brand"), before)

    @unittest.skipUnless(find_spec("playwright"), "browser extra is not installed")
    def test_browser_mode_is_account_local_and_defaults_to_headless(self):
        for configured, expected in [(None, True), (False, False), (True, True), ("false", True)]:
            with self.subTest(configured=configured):
                settings = {"identity": "brand", "browser_channel": "msedge"}
                if configured is not None:
                    settings["browser_headless"] = configured
                runtime = MagicMock()
                context = runtime.chromium.launch_persistent_context.return_value
                context.pages = [MagicMock()]
                with (
                    patch.object(browser, "read_profile", return_value=settings),
                    patch("playwright.sync_api.sync_playwright") as factory,
                    patch.object(browser, "BrowserBackend"),
                ):
                    factory.return_value.__enter__.return_value = runtime
                    browser.call("x", "brand", "health", {}, MagicMock(), MagicMock())
                options = runtime.chromium.launch_persistent_context.call_args.kwargs
                self.assertIs(options["headless"], expected)
                self.assertEqual(options["channel"], "msedge")
                context.close.assert_called_once()

    def test_recognizable_public_landing_requires_login(self):
        page = MagicMock(url="https://x.com/")
        page.get_by_test_id.return_value.wait_for.side_effect = RuntimeError("timeout")
        page.get_by_role.return_value.is_visible.return_value = True
        with self.assertRaises(BackendError) as caught:
            browser.identity("x", MagicMock(), page)
        self.assertEqual(caught.exception.safe_code, "BrowserLoginRequired")

    def test_unknown_timeout_is_not_mislabeled_as_signed_out(self):
        page = MagicMock(url="https://x.com/home")
        failure = RuntimeError("timeout")
        page.get_by_test_id.return_value.wait_for.side_effect = failure
        page.get_by_role.return_value.is_visible.return_value = False
        with self.assertRaises(RuntimeError) as caught:
            browser.identity("x", MagicMock(), page)
        self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
