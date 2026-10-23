import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.core import Conflict, Engine


class OwnedPostAdmissionTests(unittest.TestCase):
    def probe(self):
        return SimpleNamespace(
            adapters=SimpleNamespace(
                context=SimpleNamespace(job={"channel": "cloud", "cloud": {}}), call=Mock()
            ),
            store=Mock(one=Mock(return_value={"value": "1"})),
            _log_call=Mock(),
        )

    def invoke(self, probe, cloud, *, action="post"):
        with patch("auto_promotion.cloud_control.CloudControl.configured", return_value=cloud):
            return Engine._call_unlocked(
                probe,
                {"mode": "live", "platform": "x"},
                "execute",
                {"attempt_id": "one", "payload": {"action": action, "target_owned": True}},
                "task",
            )

    def test_denied_guard_proves_no_adapter_submission(self):
        probe = self.probe()
        cloud = Mock(guard=Mock(side_effect=CloudUnavailable("PacingWait")))
        result = self.invoke(probe, cloud)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["reason"], "PacingWait")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertIs(result["submission_started"], False)
        probe.adapters.call.assert_not_called()
        cloud.guard.assert_called_once()

    def test_provider_error_keeps_uncertainty(self):
        probe = self.probe()
        probe.adapters.call.side_effect = RuntimeError("provider failed after dispatch")
        with self.assertRaises(Conflict):
            self.invoke(probe, Mock())
        probe.adapters.call.assert_called_once()

    def test_reply_error_keeps_existing_behavior(self):
        probe = self.probe()
        cloud = Mock(guard=Mock(side_effect=CloudUnavailable("PacingWait")))
        with self.assertRaises(Conflict):
            self.invoke(probe, cloud, action="reply")
        probe.adapters.call.assert_not_called()

    def test_arbitrary_error_text_is_not_exposed(self):
        probe = self.probe()
        cloud = Mock(guard=Mock(side_effect=CloudUnavailable("token=private-value")))
        result = self.invoke(probe, cloud)
        self.assertEqual(result["reason"], "CloudControlUnavailable")
        self.assertNotIn("private-value", json.dumps(result))

    def test_allowed_guard_calls_provider_once(self):
        probe = self.probe()
        probe.adapters.call.return_value = {"status": "confirmed", "verified": True}
        result = self.invoke(probe, Mock())
        self.assertEqual(result["status"], "confirmed")
        self.assertNotIn("confirmed_not_submitted", result)
        probe.adapters.call.assert_called_once()
