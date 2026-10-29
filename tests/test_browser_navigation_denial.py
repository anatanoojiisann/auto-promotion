"""A denied document must not become a generic navigation/login failure."""

import unittest
from unittest.mock import MagicMock

from auto_promotion.backends.browser import BrowserBackend, navigate_home
from auto_promotion.backends.common import BackendError


class BrowserNavigationDenialTests(unittest.TestCase):
    def page(self, *, status, navigation=True, main_frame=True, abort=True, host="x.com"):
        page = MagicMock()
        page.url = "https://x.com/home"
        page.is_closed.return_value = False
        page.locator.return_value.count.return_value = 0
        page.evaluate.return_value = None
        response = MagicMock(status=status)
        # The backend retains only navigation responses from the platform host.
        response.url = "https://" + host + "/"
        response.request.is_navigation_request.return_value = navigation
        response.frame = page.main_frame if main_frame else object()
        listeners = {}
        page.on.side_effect = lambda event, listener: listeners.update({event: listener})
        failure = RuntimeError("Page.goto: Download is starting")

        def goto(*args, **kwargs):
            listeners["response"](response)
            if abort:
                raise failure
            return response

        page.goto.side_effect = goto
        return page, failure

    def test_empty_403_document_retains_denial_and_stops_before_identity(self):
        page, _ = self.page(status=403)
        backend = BrowserBackend("x", "brand", MagicMock(pages=[page]), page, {"identity": "brand"})
        backend.check_identity = MagicMock()
        with self.assertRaises(BackendError) as caught:
            backend.run("health", {}, MagicMock(), MagicMock())
        self.assertEqual(caught.exception.safe_code, "BrowserNavigationAccessDenied")
        self.assertEqual(caught.exception.execution_step, "home_navigation")
        backend.check_identity.assert_not_called()
        page.remove_listener.assert_called_once_with("response", page.on.call_args.args[1])

    def test_empty_429_document_preserves_rate_limit_handling(self):
        page, _ = self.page(status=429)
        backend = BrowserBackend("x", "brand", MagicMock(pages=[page]), page, {"identity": "brand"})
        backend.check_identity = MagicMock()
        result = backend.run("health", {}, MagicMock(), MagicMock())
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["reason"], "BrowserRateLimited")
        backend.check_identity.assert_not_called()

    def test_unrelated_response_cannot_misclassify_navigation(self):
        for navigation, main_frame, host in [
            (False, True, "x.com"),
            (True, False, "x.com"),
            (True, True, "unrelated.test"),
        ]:
            with self.subTest(navigation=navigation, main_frame=main_frame, host=host):
                page, _ = self.page(
                    status=403, navigation=navigation, main_frame=main_frame, host=host
                )
                with self.assertRaises(BackendError) as caught:
                    navigate_home(page, "x", timeout=1000)
                self.assertEqual(caught.exception.safe_code, "BrowserNavigationDownload")
                page.remove_listener.assert_called_once()

    def test_completed_denied_navigation_is_also_rejected(self):
        page, _ = self.page(status=403, abort=False)
        with self.assertRaises(BackendError) as caught:
            navigate_home(page, "x", timeout=1000)
        self.assertEqual(caught.exception.safe_code, "BrowserNavigationAccessDenied")

    def test_success_still_requires_exact_account_identity(self):
        page, _ = self.page(status=200, abort=False)
        backend = BrowserBackend("x", "brand", MagicMock(pages=[page]), page, {"identity": "brand"})
        backend.check_identity = MagicMock(side_effect=BackendError("BrowserIdentityMismatch"))
        with self.assertRaises(BackendError) as caught:
            backend.run("health", {}, MagicMock(), MagicMock())
        self.assertEqual(caught.exception.safe_code, "BrowserIdentityMismatch")
        self.assertEqual(caught.exception.execution_step, "identity_check")


if __name__ == "__main__":
    unittest.main()
