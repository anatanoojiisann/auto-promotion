import unittest
from types import SimpleNamespace
from unittest.mock import patch

from auto_promotion.backends.browser import BackendError, BrowserBackend, XStateError


class HomeRedirectGuardTests(unittest.TestCase):
    def backend(self):
        page = SimpleNamespace(url="https://x.com/")
        return BrowserBackend("x", "test", SimpleNamespace(), page, {"identity": "test"})

    def test_root_to_home_is_rechecked_with_the_full_guard(self):
        backend = self.backend()
        calls = []

        def observe(context, page):
            calls.append(page.url)
            if len(calls) == 1:
                page.url = "https://x.com/home"
                raise BackendError("XStateObservationUnavailable")

        with patch("auto_promotion.backends.browser.check_x_context_state", side_effect=observe):
            backend.check_state()
        self.assertEqual(calls, ["https://x.com/", "https://x.com/home"])

    def test_challenge_is_never_retried_or_ignored(self):
        backend = self.backend()
        with patch(
            "auto_promotion.backends.browser.check_x_context_state",
            side_effect=XStateError("XChallengeRequired", "challenge", "x_challenge"),
        ) as guard:
            with self.assertRaises(XStateError):
                backend.check_state()
        self.assertEqual(guard.call_count, 1)

    def test_nonhome_navigation_does_not_retry(self):
        backend = self.backend()

        def observe(context, page):
            page.url = "https://x.com/account/access"
            raise BackendError("XStateObservationUnavailable")

        with patch(
            "auto_promotion.backends.browser.check_x_context_state", side_effect=observe
        ) as guard:
            with self.assertRaises(BackendError):
                backend.check_state()
        self.assertEqual(guard.call_count, 1)

    def test_second_incomplete_observation_stays_blocked(self):
        backend = self.backend()

        def observe(context, page):
            page.url = "https://x.com/home"
            raise BackendError("XStateObservationUnavailable")

        with patch(
            "auto_promotion.backends.browser.check_x_context_state", side_effect=observe
        ) as guard:
            with self.assertRaises(BackendError):
                backend.check_state()
        self.assertEqual(guard.call_count, 2)


if __name__ == "__main__":
    unittest.main()
