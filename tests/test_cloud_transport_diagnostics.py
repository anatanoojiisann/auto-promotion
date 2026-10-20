import io
import json
import socket
import ssl
import unittest
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from auto_promotion.cloud_control import CloudControl, CloudUnavailable, _safe_transport_cause


class CloudTransportDiagnosticsTests(unittest.TestCase):
    def client(self):
        return CloudControl("http://127.0.0.1:8791", operator_token="SYNTHETIC-token-CANARY")

    def test_safe_transport_categories_preserve_the_public_error_and_request_id(self):
        for error, cause in [
            (TimeoutError("session-CANARY"), "timeout"),
            (URLError(TimeoutError("api_hash-CANARY")), "timeout"),
            (socket.gaierror("phone-CANARY"), "dns"),
            (ssl.SSLError("key-CANARY"), "tls"),
            (ConnectionResetError("cookie-CANARY"), "connection_reset"),
            (ConnectionRefusedError("code-CANARY"), "connection_refused"),
            (RemoteDisconnected("body-CANARY"), "remote_disconnected"),
            (IncompleteRead(b"raw-session-CANARY"), "incomplete_read"),
        ]:
            with self.subTest(cause=cause):
                client = self.client()
                with patch("auto_promotion.cloud_control.urlopen", side_effect=error) as opening:
                    with self.assertRaises(CloudUnavailable) as raised:
                        client.request(
                            "/v1/executor/telegram/login-poll",
                            {"secret": "text-CANARY"},
                            operator=True,
                        )
                self.assertEqual(str(raised.exception), "CloudTransportUnavailable")
                diagnostic = raised.exception.diagnostics
                self.assertEqual(diagnostic["cause"], cause)
                self.assertEqual(diagnostic["timeout_ms"], 12000)
                request = opening.call_args.args[0]
                self.assertEqual(request.get_header("X-ap-request-id"), diagnostic["request_id"])
                self.assertEqual(opening.call_args.kwargs["timeout"], 12)
                self.assertNotIn("CANARY", json.dumps(diagnostic))
                self.assertLess(len(json.dumps(diagnostic)), 768)

    def test_json_decode_is_distinct_without_retaining_response_content(self):
        with patch(
            "auto_promotion.cloud_control.urlopen",
            return_value=io.BytesIO(b"session-CANARY not-json"),
        ):
            with self.assertRaises(CloudUnavailable) as raised:
                self.client().request("/v1/executor/telegram/login-begin", {}, operator=True)
        self.assertEqual(str(raised.exception), "CloudTransportUnavailable")
        self.assertEqual(raised.exception.diagnostics["cause"], "json_decode")
        self.assertNotIn("CANARY", json.dumps(raised.exception.diagnostics))

    def test_http_error_retry_and_code_are_compatible_and_parse_failure_is_visible(self):
        for body, code, retry, parse in [
            (
                b'{"error":"CloudWriteQuotaExceeded","retry_after_seconds":3600}',
                "CloudWriteQuotaExceeded",
                3600,
                None,
            ),
            (b"api_hash-CANARY not-json", "CloudRequestRejected", None, "json_decode"),
        ]:
            error = HTTPError(
                "http://secret.invalid/token-CANARY", 503, "secret-CANARY", {}, io.BytesIO(body)
            )
            with patch("auto_promotion.cloud_control.urlopen", side_effect=error):
                with self.assertRaises(CloudUnavailable) as raised:
                    self.client().request("/v1/executor/telegram/login-poll", {}, operator=True)
            self.assertEqual(str(raised.exception), code)
            self.assertEqual(raised.exception.retry_after_seconds, retry)
            self.assertEqual(raised.exception.diagnostics["cause"], "http_rejected")
            self.assertEqual(raised.exception.diagnostics.get("response_parse_cause"), parse)
            self.assertTrue(error.closed)
            self.assertNotIn("CANARY", json.dumps(raised.exception.diagnostics))

    def test_successful_response_logs_only_valid_correlation_fields(self):
        result = {
            "flow_id": "00000000-0000-4000-8000-000000000001",
            "request_id": "token-CANARY",
            "generation": True,
            "qr_uri": "tg://login?token=token-CANARY",
            "session": "session-CANARY",
            "phone": "+15555550123",
        }
        client = self.client()
        with self.assertLogs("auto_promotion.cloud_control", level="INFO") as logs:
            with patch(
                "auto_promotion.cloud_control.urlopen",
                return_value=io.BytesIO(json.dumps(result).encode()),
            ):
                self.assertEqual(
                    client.request("/v1/executor/telegram/login-begin", {}, operator=True), result
                )
        self.assertEqual(client.last_request_diagnostics["flow_id"], result["flow_id"])
        self.assertNotIn("generation", client.last_request_diagnostics)
        self.assertNotIn("response_request_id", client.last_request_diagnostics)
        self.assertNotIn("CANARY", " ".join(logs.output))
        self.assertNotIn("15555550123", " ".join(logs.output))

    def test_failing_logger_and_formatter_cannot_change_success_or_original_failure(self):
        for failing in ["logging.getLogger", "_build_request_diagnostic"]:
            with self.subTest(failing=failing):
                target = (
                    "auto_promotion.cloud_control.logging.getLogger"
                    if failing == "logging.getLogger"
                    else "auto_promotion.cloud_control.CloudControl._build_request_diagnostic"
                )
                with patch(target, side_effect=RuntimeError("key-CANARY")):
                    with patch(
                        "auto_promotion.cloud_control.urlopen",
                        return_value=io.BytesIO(b'{"status":"pending"}'),
                    ):
                        self.assertEqual(
                            self.client().request(
                                "/v1/executor/telegram/login-poll", {}, operator=True
                            ),
                            {"status": "pending"},
                        )
                    with patch(
                        "auto_promotion.cloud_control.urlopen", side_effect=TimeoutError("secret")
                    ):
                        with self.assertRaises(CloudUnavailable) as raised:
                            self.client().request(
                                "/v1/executor/telegram/login-poll", {}, operator=True
                            )
                    self.assertEqual(str(raised.exception), "CloudTransportUnavailable")

    def test_cyclic_or_text_causes_and_unknown_exception_names_are_not_exported(self):
        error = URLError("token-CANARY")
        error.reason = error
        self.assertEqual(_safe_transport_cause(error), "unknown")
        self.assertEqual(_safe_transport_cause(URLError("session-CANARY")), "unknown")
        self.assertEqual(_safe_transport_cause(RuntimeError("api_hash-CANARY")), "unknown")

    def test_log_budget_reserves_failures_and_keeps_only_latest_safe_metadata(self):
        client = self.client()
        with patch("auto_promotion.cloud_control.logging.getLogger") as logger:
            with patch(
                "auto_promotion.cloud_control.urlopen",
                side_effect=lambda *a, **kw: io.BytesIO(b'{"status":"pending"}'),
            ):
                for _ in range(100):
                    client.request("/v1/executor/telegram/login-poll", {}, operator=True)
            with patch("auto_promotion.cloud_control.urlopen", side_effect=TimeoutError("CANARY")):
                for _ in range(40):
                    with self.assertRaises(CloudUnavailable):
                        client.request(
                            "/v1/executor/telegram/login-poll",
                            {"flow_id": "00000000-0000-4000-8000-000000000001"},
                            operator=True,
                        )
        self.assertEqual(logger.return_value.info.call_count, 64)
        self.assertEqual(logger.return_value.warning.call_count, 32)
        self.assertEqual(client.last_request_diagnostics["cause"], "timeout")
        self.assertEqual(
            client.last_request_diagnostics["flow_id"], "00000000-0000-4000-8000-000000000001"
        )


if __name__ == "__main__":
    unittest.main()
