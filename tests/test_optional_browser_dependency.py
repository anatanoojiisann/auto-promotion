"""Exercise the optional SDK boundary without installed packages hiding it."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class OptionalBrowserDependencyTests(unittest.TestCase):
    def without_sdk(self, body):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = textwrap.dedent(
                """
                import importlib.abc
                import sys

                class NoBrowserSDK(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == 'playwright' or fullname.startswith('playwright.'):
                            raise ModuleNotFoundError('synthetic missing browser SDK', name=fullname)

                def no_network(event, args):
                    if event in {'socket.connect', 'socket.bind'}:
                        raise AssertionError('network forbidden in optional SDK test')

                sys.meta_path.insert(0, NoBrowserSDK())
                sys.addaudithook(no_network)
                """
            ) + textwrap.dedent(body)
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "WINDIR"}
            }
            env.update(
                HOME=str(root),
                USERPROFILE=str(root),
                AP_DB=str(root / "synthetic.db"),
                AP_ACCOUNTS_DIR=str(root / "accounts"),
                AUTO_PROMOTION_LIVE_WRITES="0",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            )
            result = subprocess.run(
                [sys.executable, "-s", "-c", program],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_api_health_and_simulation_work_without_browser_sdk(self):
        self.without_sdk(
            """
            import os
            from fastapi.testclient import TestClient
            from auto_promotion.api import create_app
            from auto_promotion.adapters import SimulationAdapter
            from auto_promotion.store import Store

            app = create_app(db_path=os.environ['AP_DB'], worker=False, report_worker=False)
            with TestClient(app) as client:
                assert client.get('/health').status_code == 200
            adapter = SimulationAdapter()
            assert adapter.call({'platform': 'x'}, 'health', {})['healthy'] is True
            store = Store(os.environ['AP_DB'])
            assert store.one("SELECT value FROM meta WHERE key='global_stop'")['value'] == '1'
            assert not store.rows('SELECT * FROM attempts')
            assert not any(name.startswith('playwright') for name in sys.modules)
            """
        )

    def test_browser_call_without_sdk_fails_before_launch_or_authorization(self):
        self.without_sdk(
            """
            from unittest.mock import Mock, patch
            from auto_promotion.backends.browser import call

            checkpoint, authorize = Mock(), Mock()
            with (
                patch('auto_promotion.backends.browser.read_profile', return_value={'identity': 'synthetic'}),
                patch('auto_promotion.backends.browser.launch_context') as launch,
            ):
                try:
                    call('x', 'synthetic', 'execute', {'payload': {'action': 'reply'}}, checkpoint, authorize)
                except ModuleNotFoundError as exc:
                    assert exc.name.startswith('playwright')
                else:
                    raise AssertionError('missing browser SDK must fail closed')
                launch.assert_not_called()
            checkpoint.assert_not_called()
            checkpoint.progress.assert_not_called()
            authorize.assert_not_called()
            """
        )

    @unittest.skipUnless(find_spec("playwright"), "browser extra is not installed")
    def test_installed_sdk_timeout_rechecks_identity_without_submitting(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        from auto_promotion.backends.browser import BrowserBackend

        backend = BrowserBackend("x", "synthetic", SimpleNamespace(), Mock(), {"identity": "brand"})
        with (
            patch.object(
                backend,
                "check_identity",
                side_effect=[PlaywrightTimeoutError("synthetic timeout"), None],
            ) as identity,
            patch.object(backend, "check_state") as state,
            patch("auto_promotion.backends.browser.check_status") as status,
        ):
            backend.check_send_identity(timeout=123)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(identity.call_args.kwargs["timeout"], 123)
        state.assert_called_once_with()
        status.assert_called_once_with(backend.page)
        self.assertFalse(backend.submission_started)
        self.assertFalse(backend.page.mock_calls)
