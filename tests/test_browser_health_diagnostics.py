import unittest

from auto_promotion.execution_diagnostics import browser_read_failure_reason


class BrowserHealthDiagnosticsTests(unittest.TestCase):
    def test_busy_profile_is_distinguished_from_login_failure(self):
        error = RuntimeError("Failed to create a ProcessSingleton for private-profile-path")
        self.assertEqual(browser_read_failure_reason(error, "Error"), "BrowserProfileBusy")

    def test_proxy_failure_never_exposes_credential_bearing_url(self):
        error = RuntimeError(
            "net::ERR_PROXY_CONNECTION_FAILED https://user:secret@private.example/"
        )
        self.assertEqual(browser_read_failure_reason(error, "Error"), "BrowserProxyUnavailable")

    def test_unrecognized_failure_keeps_safe_existing_code(self):
        self.assertEqual(
            browser_read_failure_reason(RuntimeError("private details"), "Error"), "Error"
        )
