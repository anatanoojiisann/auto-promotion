"""Task status must reflect cloud failures without changing a frozen X batch."""

import copy
import unittest
from datetime import datetime, timezone

from auto_promotion.readiness import task_workflow


class CloudWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.task = {
            "mode": "live",
            "state": "active",
            "phase": "ready",
            "manifest_hash": "frozen",
            "policy": {
                "starts_at": self.iso(self.now - 100),
                "ends_at": self.iso(self.now + 3600),
            },
            "items": [
                {
                    "status": "not_attempted",
                    "due_at": self.now,
                    "payload": {"platform": "x", "action": "reply"},
                    "result": {},
                }
            ],
            "candidates": [],
            "next_at": 0,
            "execution": {
                "pools": {
                    "cloud": {
                        "state": "CloudWriteQuotaExceeded",
                        "retry_at": self.now + 7200,
                        "execution_ready": False,
                    }
                }
            },
        }

    @staticmethod
    def iso(seconds):
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

    def workflow(self, blockers=None):
        return task_workflow(self.task, blockers or [], [], self.now)

    def test_cloud_failure_overrides_ready_and_future_schedule_without_mutation(self):
        for due in (self.now, self.now + 1800):
            with self.subTest(due=due):
                self.task["items"][0]["due_at"] = due
                original = copy.deepcopy(self.task)
                result = self.workflow()
                self.assertEqual(result["status"], "waiting_execution_service")
                self.assertEqual(result["next_at"], self.now + 7200)
                self.assertFalse(result["can_prepare"])
                self.assertEqual(self.task, original)

    def test_legacy_worker_failure_also_reports_execution_service(self):
        self.task["execution"]["pools"]["cloud"] = {"state": "CloudRequestRejected"}
        result = self.workflow()
        self.assertEqual(result["status"], "waiting_execution_service")
        self.assertIsNone(result["next_at"])

    def test_recovery_returns_to_original_schedule(self):
        self.task["execution"]["pools"]["cloud"] = {"state": "connected"}
        self.task["items"][0]["due_at"] = self.now + 1800
        result = self.workflow()
        self.assertEqual(result["status"], "waiting_schedule")
        self.assertEqual(result["next_at"], self.now + 1800)
        self.task["items"][0]["due_at"] = self.now
        self.assertEqual(self.workflow()["status"], "ready")

    def test_local_fallback_is_displayed_without_changing_schedule_or_stop(self):
        self.task["execution"]["pools"]["channel"] = {"active": "local", "until": self.now + 7200}
        original = copy.deepcopy(self.task)
        self.assertIn("本地通道", self.workflow()["label"])
        self.assertEqual(self.workflow(["global_stop"])["status"], "global_stop")
        self.task["items"][0]["due_at"] = self.now + 1800
        self.assertEqual(self.workflow()["status"], "waiting_schedule")
        self.task["items"][0]["due_at"] = self.now
        self.assertEqual(self.task, original)

    def test_expired_or_stopped_tasks_keep_their_status(self):
        self.assertEqual(self.workflow(["global_stop"])["status"], "global_stop")
        self.now += 3600
        self.assertEqual(self.workflow()["status"], "expired")

    def test_submitted_unknown_item_stays_in_verification(self):
        self.task["items"][0]["status"] = "unknown"
        self.assertEqual(self.workflow()["status"], "verifying")

    def test_simulation_does_not_depend_on_cloud(self):
        self.task["mode"] = "simulation"
        self.assertEqual(self.workflow()["status"], "ready")


if __name__ == "__main__":
    unittest.main()
