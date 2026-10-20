import unittest
from unittest.mock import patch

from auto_promotion.backends.browser_use import call
from auto_promotion.backends.common import BackendError, automatic_actions


class BrowserUseConnectionTests(unittest.TestCase):
    def settings(self):
        return {
            "identity": "example",
            "browser_channel": "browser_use",
            "browser_family": "chrome",
            "browser_instance_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "browser_tab_id": "123",
            "browser_use_observation": {
                "observed_at": 1000,
                "handles": ["example"],
                "profile_href": "/example",
            },
        }

    @patch("auto_promotion.backends.browser_use.time.time", return_value=1001)
    def test_recent_observation_and_identity_must_both_match(self, _clock):
        settings = self.settings()
        self.assertTrue(call("x", settings, "health")["identity_verified"])
        settings["browser_use_observation"]["profile_href"] = "/another"
        self.assertFalse(call("x", settings, "health")["healthy"])

    @patch("auto_promotion.backends.browser_use.time.time", return_value=1001)
    def test_edge_observation_verifies_identity_without_enabling_actions(self, _clock):
        settings = self.settings()
        settings["browser_family"] = "edge"
        self.assertTrue(call("x", settings, "health")["identity_verified"])
        self.assertFalse(call("x", settings, "preflight")["allowed"])
        with self.assertRaises(BackendError):
            call("x", settings, "execute")

    def test_unsupported_browser_family_is_rejected(self):
        settings = self.settings()
        settings["browser_family"] = "unknown"
        with self.assertRaises(BackendError):
            call("x", settings, "health")

    @patch("auto_promotion.backends.browser_use.time.time", return_value=1301)
    def test_expired_observation_requires_browser_recheck(self, _clock):
        result = call("x", self.settings(), "health")
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["reason"], "BrowserUseRecheckRequired")

    @patch("auto_promotion.backends.browser_use.time.time", return_value=999)
    def test_future_observation_is_not_accepted(self, _clock):
        self.assertFalse(call("x", self.settings(), "health")["healthy"])

    def test_observation_never_authorizes_unattended_actions(self):
        settings = self.settings()
        settings["automatic_actions"] = ["reply"]
        self.assertFalse(call("x", settings, "preflight")["allowed"])
        with self.assertRaises(BackendError):
            call("x", settings, "execute")
        with patch("auto_promotion.backends.common.read_profile", return_value=settings):
            self.assertEqual(
                automatic_actions({"platform": "x", "mode": "live", "backend_label": "local"}),
                [],
            )

    def test_native_chrome_requires_its_own_reply_opt_in(self):
        account = {"platform": "x", "mode": "live", "backend_label": "chrome-native"}
        settings = {"identity": "example", "browser_channel": "chrome"}
        with patch("auto_promotion.backends.common.read_profile", return_value=settings):
            self.assertEqual(automatic_actions(account), [])
            settings["automatic_actions"] = ["reply", "like", "repost"]
            self.assertEqual(automatic_actions(account), ["reply"])
