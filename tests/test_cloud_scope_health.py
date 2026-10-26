import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable, task_scope_health


class CloudScopeHealthTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.now = 1800000000
        self.engine.clock = lambda: self.now
        self.task, self.account = self.fixture.ready()
        self.cloud = CloudControl(
            "http://127.0.0.1:8791", node_id="synthetic", node_token="test", operator_token="test"
        )

    def health(self):
        return task_scope_health(self.engine, self.task["id"], cloud=self.cloud)

    def test_scoped_sync_success_is_fresh_bound_and_does_not_clear_global_conflict(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('cloud_control_health',?)",
                (json.dumps({"state": "FrozenJobChanged", "at": self.now - 10}),),
            )
        with patch.object(self.cloud, "request", return_value={}):
            self.cloud.sync(self.engine, task_ids=[self.task["id"]])
        self.assertTrue(self.health()["valid_for_task"])
        self.assertEqual(
            json.loads(
                self.store.one("SELECT value FROM meta WHERE key='cloud_control_health'")["value"]
            )["state"],
            "FrozenJobChanged",
        )
        self.now += 301
        with patch.object(self.cloud, "request") as request:
            self.cloud.sync(self.engine, task_ids=[self.task["id"]])
        request.assert_not_called()
        self.assertFalse(self.health()["valid_for_task"])

    def test_configuration_change_invalidates_scope_observation(self):
        with patch.object(self.cloud, "request", return_value={}):
            self.cloud.sync(self.engine, task_ids=[self.task["id"]])
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET daily_limit=daily_limit+1 WHERE id=?", (self.account["id"],)
            )
        self.assertFalse(self.health()["valid_for_task"])

    def test_late_old_failure_and_success_cannot_replace_newer_scope_observation(self):
        for old_fails in (True, False):
            inner = False

            def race(*args, **kwargs):
                nonlocal inner
                if inner:
                    if not old_fails:
                        raise CloudUnavailable("FrozenJobChanged")
                    return {"changed_records": 1, "unchanged": False}
                inner = True
                try:
                    self.cloud.sync(self.engine, task_ids=[self.task["id"]])
                except CloudUnavailable:
                    pass
                if old_fails:
                    raise CloudUnavailable("CloudTransportUnavailable")
                return {"changed_records": 1, "unchanged": False}

            with patch.object(self.cloud, "_sync", side_effect=race):
                try:
                    self.cloud.sync(self.engine, task_ids=[self.task["id"]])
                except CloudUnavailable:
                    pass
            self.assertEqual(
                self.health()["state"], "connected" if old_fails else "FrozenJobChanged"
            )
            self.now += 31

    def test_scope_retry_after_survives_restart_and_suppresses_requests_until_deadline(self):
        with patch.object(
            self.cloud,
            "request",
            side_effect=CloudUnavailable("CloudTransportUnavailable", retry_after_seconds=60),
        ) as request:
            for _ in range(3):
                with self.assertRaises(CloudUnavailable):
                    self.cloud.sync(self.engine, task_ids=[self.task["id"]])
        self.assertEqual(request.call_count, 1)
        replacement = CloudControl(
            self.cloud.url, node_id=self.cloud.node_id, node_token="test", operator_token="test"
        )
        with patch.object(replacement, "request", return_value={}) as request:
            with self.assertRaises(CloudUnavailable):
                replacement.sync(self.engine, task_ids=[self.task["id"]])
            request.assert_not_called()
            self.now += 61
            replacement.sync(self.engine, task_ids=[self.task["id"]])
        self.assertTrue(self.health()["execution_ready"])

    def test_metadata_sync_propagates_all_accounts_tasks_and_skips_immutable_job_bodies(self):
        second, _ = self.fixture.ready(account=self.account, target_id=987)
        self.engine.change_state(second["id"], "paused")
        with patch.object(self.cloud, "request", return_value={}) as request:
            self.cloud.sync(self.engine, metadata_only=True)
        payloads = [call.args[1] for call in request.call_args_list]
        self.assertFalse(any(payload["jobs"] for payload in payloads))
        self.assertEqual(
            {task["id"] for payload in payloads for task in payload["tasks"]},
            {self.task["id"], second["id"]},
        )
        self.assertTrue(any(payload["accounts"] for payload in payloads))

    def test_frozen_conflict_diagnostics_exclude_raw_values_and_add_local_record_hash(self):
        body = json.dumps(
            {
                "error": "FrozenJobChanged",
                "conflict": {
                    "conflict_index": 0,
                    "changed_fields": ["version"],
                    "old_value": "SENSITIVE_PRIVATE_VALUE",
                },
            }
        ).encode()
        error = HTTPError(self.cloud.url, 409, "conflict", {}, io.BytesIO(body))
        with patch("auto_promotion.cloud_control.urlopen", side_effect=error):
            with self.assertRaises(CloudUnavailable) as caught:
                self.cloud.request("/v1/sync", {}, operator=True)
        self.assertEqual(
            caught.exception.diagnostics["sync_conflict"],
            {"conflict_index": 0, "changed_fields": ["version"]},
        )
        self.assertNotIn("SENSITIVE_PRIVATE_VALUE", json.dumps(caught.exception.diagnostics))

        def conflict(path, payload, **kwargs):
            if payload["jobs"]:
                raise CloudUnavailable(
                    "FrozenJobChanged",
                    diagnostics={
                        "sync_conflict": {"conflict_index": 0, "changed_fields": ["version"]}
                    },
                )
            return {}

        with patch.object(self.cloud, "request", side_effect=conflict):
            with self.assertRaises(CloudUnavailable) as caught:
                self.cloud.sync(self.engine, task_ids=[self.task["id"]])
        self.assertEqual(caught.exception.diagnostics["sync_batch_section"], "jobs")
        self.assertEqual(len(caught.exception.diagnostics["sync_record_hash"]), 64)
        self.assertEqual(self.health()["state"], "FrozenJobChanged")
