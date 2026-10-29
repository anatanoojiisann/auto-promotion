"""Readiness messages use task-scoped observations without authorizing execution."""

import copy
import unittest
from datetime import datetime, timezone

from auto_promotion.readiness import task_workflow


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class ScopedReadinessDisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.0
        self.due = self.now + 3600
        self.task = {
            "id": "task-fixture",
            "mode": "live",
            "state": "active",
            "phase": "ready",
            "manifest_hash": "fixture-manifest",
            "next_at": 0,
            "policy": {
                "starts_at": iso(self.now - 1),
                "ends_at": iso(self.now + 7200),
                "max_joins": 0,
            },
            "items": [
                {
                    "id": "item-fixture",
                    "status": "not_attempted",
                    "result": {},
                    "external_reference": None,
                    "due_at": self.due,
                    "payload": {"action": "reply"},
                }
            ],
            "candidates": [],
            "execution": {
                "pools": {
                    "cloud": {
                        "state": "FrozenJobChanged",
                        "at": self.now - 20,
                        "execution_ready": False,
                        "task_scope": {
                            "scope": "task",
                            "task_id": "task-fixture",
                            "state": "connected",
                            "at": self.now - 10,
                            "expires_at": self.now + 290,
                            "execution_ready": True,
                            "valid_for_task": True,
                        },
                    },
                    "channel": {"active": "cloud"},
                }
            },
        }

    def workflow(self, blockers=None):
        return task_workflow(self.task, blockers or [], [], self.now)

    def test_fresh_bound_scope_keeps_future_schedule_and_old_global_observation(self):
        value = self.workflow()
        self.assertEqual(value["status"], "waiting_schedule")
        self.assertEqual(value["next_at"], self.due)
        self.assertEqual(value["schedule_next_at"], self.due)
        observation = value["service_observation"]
        self.assertEqual(observation["global_state"], "FrozenJobChanged")
        self.assertEqual(observation["task_at"], self.now - 10)
        self.assertTrue(observation["task_scope_usable"])

    def test_scope_not_usable_when_wrong_task_stale_expired_or_invalidated(self):
        original = copy.deepcopy(self.task)
        for change in (
            {"task_id": "other"},
            {"scope": "all"},
            {"valid_for_task": False},
            {"at": self.now - 301},
            {"at": self.now + 1},
            {"expires_at": self.now},
            {"execution_ready": False},
        ):
            self.task = copy.deepcopy(original)
            self.task["execution"]["pools"]["cloud"]["task_scope"].update(change)
            with self.subTest(change=change):
                result = self.workflow()
                self.assertEqual(result["status"], "waiting_execution_service")
                self.assertEqual(result["schedule_next_at"], self.due)

    def test_later_global_error_and_global_quota_are_not_hidden_by_scope_success(self):
        for change in (
            {"at": self.now - 1},
            {"at": self.now - 10},
            {"state": "CloudWriteQuotaExceeded"},
            {"state": "CloudControlConfigurationMissing"},
        ):
            with self.subTest(change=change):
                cloud = self.task["execution"]["pools"]["cloud"]
                prior = dict(cloud)
                cloud.update(change)
                self.assertEqual(self.workflow()["status"], "waiting_execution_service")
                cloud.clear()
                cloud.update(prior)

    def test_real_local_blockers_win_and_schedule_is_still_visible(self):
        for reason in (
            "global_stop",
            "live_writes_disabled",
            "account_disabled",
            "account_outbound_blocked",
            "account_cooldown",
        ):
            with self.subTest(reason=reason):
                value = self.workflow([reason])
                self.assertEqual(value["status"], reason)
                self.assertEqual(value["schedule_next_at"], self.due)

    def test_foreign_or_unvalidated_local_scope_does_not_hide_cloud_block(self):
        self.task["execution"]["pools"]["cloud"].pop("task_scope")
        channel = self.task["execution"]["pools"]["channel"]
        channel.update(active="local", until=self.now + 100, valid_for_task=False)
        self.assertEqual(self.workflow()["status"], "waiting_execution_service")
        channel["valid_for_task"] = True
        self.assertEqual(self.workflow()["status"], "waiting_schedule")
        channel["until"] = self.now
        # A validated task-scoped lease may outlive the legacy global entry.
        self.assertEqual(self.workflow()["status"], "waiting_schedule")
        channel["valid_for_task"] = False
        self.assertEqual(self.workflow()["status"], "waiting_execution_service")

    def test_retry_time_is_distinct_from_send_schedule(self):
        cloud = self.task["execution"]["pools"]["cloud"]
        cloud.update(state="CloudWriteQuotaExceeded", retry_at=self.now + 60)
        value = self.workflow()
        self.assertEqual(value["next_at"], self.now + 60)
        self.assertEqual(value["next_at_kind"], "service_retry")
        self.assertEqual(value["schedule_next_at"], self.due)

    def test_stop_is_visible_even_with_conflicting_item_or_starting_service_wait(self):
        blocked = [{"item_id": "item-fixture", "reason": "interaction_already_claimed"}]
        result = task_workflow(self.task, ["global_stop"], blocked, self.now)
        self.assertEqual(result["status"], "global_stop")
        self.task["phase"] = "starting"
        self.task["policy"]["outbound_after_service_recovery_seconds"] = 300
        self.assertEqual(
            task_workflow(self.task, ["global_stop"], [], self.now)["status"], "global_stop"
        )

    def test_fresh_own_sync_failure_overrides_old_global_success_with_own_retry(self):
        cloud = self.task["execution"]["pools"]["cloud"]
        cloud.update(state="connected", execution_ready=True, at=self.now - 2)
        cloud["task_scope"].update(
            state="FrozenJobChanged", execution_ready=False, at=self.now - 1, retry_at=self.now + 45
        )
        for due in (self.due, self.now):
            with self.subTest(due=due):
                self.task["items"][0]["due_at"] = due
                value = self.workflow()
                self.assertEqual(value["status"], "waiting_execution_service")
                self.assertEqual(value["next_at"], self.now + 45)
                self.assertEqual(value["next_at_kind"], "service_retry")
                self.assertTrue(value["service_observation"]["task_scope_failed"])
                self.assertIn("本任务同步", value["message"])

    def test_wrong_stale_or_invalid_task_failure_does_not_block_global_success(self):
        cloud = self.task["execution"]["pools"]["cloud"]
        cloud.update(state="connected", execution_ready=True)
        scope = cloud["task_scope"]
        scope.update(state="FrozenJobChanged", execution_ready=False, retry_at=self.now + 45)
        original = dict(scope)
        for change in (
            {"task_id": "other"},
            {"at": self.now - 301},
            {"expires_at": self.now},
            {"valid_for_task": False},
        ):
            scope.clear()
            scope.update(original | change)
            with self.subTest(change=change):
                self.assertEqual(self.workflow()["status"], "waiting_schedule")

    def test_bound_cloud_failure_is_observed_but_does_not_block_valid_local_scope(self):
        cloud = self.task["execution"]["pools"]["cloud"]
        cloud.update(state="connected", execution_ready=True)
        cloud["task_scope"].update(
            state="FrozenJobChanged", execution_ready=False, retry_at=self.now + 45
        )
        self.task["execution"]["pools"]["channel"].update(
            active="local", until=self.now + 3600, valid_for_task=True
        )
        value = self.workflow()
        self.assertEqual(value["status"], "waiting_schedule")
        self.assertTrue(value["service_observation"]["task_scope_failed"])
        self.assertEqual(value["service_observation"]["task_retry_at"], self.now + 45)
