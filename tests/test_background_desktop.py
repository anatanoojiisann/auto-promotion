import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_promotion import background_desktop, native_bridge
from auto_promotion.backends import tabbit
from auto_promotion.backends.common import BackendError, save_profile


class BackgroundDesktopTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "platform": "x",
            "account": "brand",
            "operation": "engagement",
            "payload": {"read_only": True},
        }

    def test_only_headed_read_only_browser_metrics_use_private_desktop(self):
        with (
            patch.object(background_desktop.sys, "platform", "win32"),
            patch.dict(os.environ, {background_desktop._CHILD_ENV: "0"}),
            patch.object(background_desktop, "read_profile") as read_profile,
        ):
            read_profile.return_value = {"browser_channel": "msedge", "browser_headless": False}
            self.assertTrue(background_desktop.needs_background_desktop(self.request))
            for settings in (
                {"browser_channel": "msedge", "browser_headless": True},
                {"browser_channel": "tabbit", "browser_headless": False},
                {"connection_type": "x_api", "browser_headless": False},
            ):
                read_profile.return_value = settings
                self.assertFalse(background_desktop.needs_background_desktop(self.request))
            read_profile.return_value = {"browser_channel": "msedge", "browser_headless": False}
            for operation, read_only in (("execute", True), ("engagement", False)):
                request = {
                    **self.request,
                    "operation": operation,
                    "payload": {"read_only": read_only},
                }
                self.assertFalse(background_desktop.needs_background_desktop(request))
            with patch.dict(os.environ, {background_desktop._CHILD_ENV: "1"}):
                self.assertFalse(background_desktop.needs_background_desktop(self.request))

    def test_desktop_setup_failure_preserves_existing_collection_path(self):
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.request))),
            patch("sys.stdout", output),
            patch.object(background_desktop, "needs_background_desktop", return_value=True),
            patch.object(
                background_desktop,
                "dispatch_on_background_desktop",
                side_effect=background_desktop.BackgroundDesktopUnavailable,
            ),
            patch.object(native_bridge, "dispatch", return_value={"verified": True}) as dispatch,
        ):
            native_bridge.main()
        dispatch.assert_called_once_with(self.request)
        self.assertEqual(json.loads(output.getvalue()), {"verified": True})

    def test_failed_started_child_is_not_silently_retried(self):
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.request))),
            patch("sys.stdout", output),
            patch.object(background_desktop, "needs_background_desktop", return_value=True),
            patch.object(
                background_desktop,
                "dispatch_on_background_desktop",
                side_effect=BackendError("BackgroundDesktopTimeout"),
            ),
            patch.object(native_bridge, "dispatch") as dispatch,
        ):
            native_bridge.main()
        dispatch.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["bridge_error"], "BackgroundDesktopTimeout")

    @unittest.skipUnless(sys.platform == "win32", "Windows private desktop")
    def test_private_desktop_bridge_round_trip(self):
        result = background_desktop.dispatch_on_background_desktop(
            {"platform": "x", "account": "unused", "operation": "not_supported", "payload": {}}
        )
        self.assertEqual(result["bridge_error"], "UnsupportedPlatformOperation")

    @unittest.skipUnless(sys.platform == "win32", "Windows private desktop")
    def test_headed_metrics_use_the_normal_bridge_on_private_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"AP_ACCOUNTS_DIR": folder}, clear=False):
                os.environ.pop(background_desktop._CHILD_ENV, None)
                save_profile(
                    "x",
                    "brand",
                    {"identity": "brand", "browser_channel": "msedge", "browser_headless": False},
                )
                request = {
                    **self.request,
                    "payload": {"read_only": True, "external_reference": "invalid"},
                }
                output = io.StringIO()
                with (
                    patch("sys.stdin", io.StringIO(json.dumps(request))),
                    patch("sys.stdout", output),
                ):
                    native_bridge.main()
                result = json.loads(output.getvalue())
                self.assertFalse(result["verified"])
                self.assertEqual(result["reason"], "InvalidMetricsReference")

    def test_tabbit_cli_keeps_its_captured_output_without_a_console_window(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "tabbit-cli.exe"
            launcher.touch()
            completed = SimpleNamespace(returncode=0, stdout='{"ok": true}')
            with (
                patch.object(tabbit, "launcher_path", return_value=launcher),
                patch.object(tabbit.subprocess, "run", return_value=completed) as run,
            ):
                self.assertEqual(tabbit.cli(["status"], "instance"), {"ok": True})
            self.assertEqual(
                run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            self.assertTrue(run.call_args.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
