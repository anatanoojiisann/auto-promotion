import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auto_promotion import device_control as dc
from auto_promotion.device_ui import make_server


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class DeviceClientTests(unittest.TestCase):
    def test_dpapi_persists_no_plaintext_and_prepare_reuses_request_after_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "device.enc"
            args = SimpleNamespace(
                command="init",
                config=str(path),
                account="synthetic",
                target="-1002123962275",
                text=None,
            )
            dc.execute(args)
            state = dc.load(path)
            self.assertNotIn(state["token"].encode(), path.read_bytes())
            self.assertNotIn(b"synthetic", path.read_bytes())
            args.command, args.text = "prepare", "synthetic text"
            seen = []

            def uncertain(config, op, body):
                seen.append(body["request_id"])
                raise RuntimeError("ControlOutcomeUncertainCheckStatusDoNotRecreate")

            with patch.object(dc, "request", uncertain):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        dc.execute(args)
            self.assertEqual(seen[0], seen[1])
            args.text = "changed"
            with self.assertRaisesRegex(RuntimeError, "FrozenDeviceDraftChanged"):
                dc.execute(args)

    def test_ui_rejects_missing_csrf_and_cross_origin_without_operation(self):
        calls = []
        server = make_server("unused", 0, operation=lambda args: calls.append(args.command))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            html = urlopen(base).read().decode()
            self.assertIn("准备", html)
            req = Request(
                base + "/api/device/start",
                data=b"{}",
                headers={"Origin": "https://foreign.invalid"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class DeviceTransportTests(unittest.TestCase):
    def test_server_errors_are_allowlisted_and_never_echo_raw_credentials(self):
        import io
        from unittest.mock import MagicMock

        config = {
            "url": "https://auto-promotion-control.auto-promotion-control.workers.dev",
            "token": "synthetic",
            "node_id": "device_synthetic",
            "store_id": "store_synthetic",
        }
        for body, expected in [
            (
                b'{"error":"DeviceRequiresExplicitChannelHandoff"}',
                "DeviceRequiresExplicitChannelHandoff",
            ),
            (b'{"error":"secret-synthetic"}', "ControlRequestRejectedHTTP409"),
        ]:
            opener = MagicMock()
            opener.open.side_effect = HTTPError(
                "https://synthetic.invalid", 409, "private", {}, io.BytesIO(body)
            )
            with patch.object(dc.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(RuntimeError, "^" + expected + "$"):
                    dc.request(config, "status", {})
