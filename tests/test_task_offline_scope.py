import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_task_channels as fixtures

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.execution_channels import offline_only, read_state
from auto_promotion.models import AccountInput, ItemInput, TaskInput, canonical
from auto_promotion.task_channels import TaskChannelRouter


class TaskOfflineScopeTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"AP_NODE_ID": "node"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = fixtures.TaskChannelTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Synthetic Telegram",
                backend_label="synthetic",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(self.account["id"])

        def iso(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()

        self.task = self.engine.create_task(
            TaskInput(
                name="Synthetic scoped local task",
                account_ids=[self.account["id"]],
                policy={
                    "execution_channel": "local",
                    "starts_at": iso(self.fixture.now - 100),
                    "ends_at": iso(self.fixture.now + 3600),
                    "min_interval": 0,
                    "max_joins": 0,
                },
            )
        )
        self.engine.set_items(
            self.task["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="send_message",
                    target="@synthetic_group",
                    text="Synthetic test only",
                    scheduled_at=iso(self.fixture.now),
                )
            ],
        )
        self.engine.start_task(self.task["id"], source="synthetic test instruction")
        self.fixture.queue.sync(self.task["id"])
        self.job = self.fixture.queue.claim("simulation.telegram", "synthetic-owner")
        self.assertIsNotNone(self.job)
        self.cloud, self.remote, self.request, self.sync = self.fixture.fake_cloud()
        self.state = {"active": "local", "offline_only": True, "account_ids": ["other-account"]}
        self.save_state()

    def save_state(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES('execution_channel',?)",
                (canonical(self.state),),
            )
            db.execute("INSERT OR REPLACE INTO meta VALUES('cloud_control_required','1')")

    def test_other_accounts_offline_scope_does_not_disable_telegram_coordination(self):
        before = self.engine.task(self.task["id"])["manifest_hash"]
        self.assertTrue(offline_only(self.store))
        self.assertFalse(offline_only(self.store, self.account["id"]))
        context, selected = TaskChannelRouter(self.engine).prepare(self.job)
        self.assertEqual(context["channel"], "local")
        self.assertIs(selected, self.cloud)
        self.sync.assert_called_once_with(self.engine, task_ids=[self.task["id"]])
        self.assertEqual(self.remote[self.account["id"]]["channel"], "local")
        self.assertEqual(read_state(self.store), self.state)
        self.assertEqual(self.engine.task(self.task["id"])["manifest_hash"], before)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_scoped_account_stays_offline_without_ownership_proof(self):
        self.state["account_ids"] = [self.account["id"]]
        self.save_state()
        with self.assertRaisesRegex(CloudUnavailable, "CloudControlConfigurationMissing"):
            TaskChannelRouter(self.engine).prepare(self.job)
        self.fixture.cloud_config.assert_not_called()
        self.request.assert_not_called()

    def test_unscoped_offline_instruction_still_blocks_coordination(self):
        self.state["account_ids"] = []
        self.save_state()
        with self.assertRaisesRegex(CloudUnavailable, "CloudControlConfigurationMissing"):
            TaskChannelRouter(self.engine).prepare(self.job)
        self.fixture.cloud_config.assert_not_called()
        self.request.assert_not_called()

    def test_missing_configuration_is_not_bypassed(self):
        self.fixture.cloud_config.return_value = None
        with self.assertRaisesRegex(CloudUnavailable, "CloudControlConfigurationMissing"):
            TaskChannelRouter(self.engine).prepare(self.job)
        self.request.assert_not_called()
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_failed_new_handoff_can_reconcile_without_changing_generation(self):
        self.fixture.cloud_config.return_value = None
        with self.assertRaisesRegex(CloudUnavailable, "CloudControlConfigurationMissing"):
            TaskChannelRouter(self.engine).prepare(self.job)
        prior = self.store.one(
            "SELECT * FROM account_task_channels WHERE account_id=?", (self.account["id"],)
        )
        self.assertEqual(prior["state"], "handoff")
        self.fixture.cloud_config.return_value = self.cloud
        self.fixture.now += 31
        context, selected = TaskChannelRouter(self.engine).prepare(self.job)
        self.assertIs(selected, self.cloud)
        self.assertEqual(context["generation"], prior["generation"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_invalid_remote_ack_does_not_grant_authority(self):
        self.request.side_effect = lambda *args, **kwargs: {}
        with self.assertRaisesRegex(CloudUnavailable, "TaskChannelHandoffUnverified"):
            TaskChannelRouter(self.engine).prepare(self.job)
        self.assertEqual(
            self.store.one("SELECT state FROM account_task_channels")["state"], "handoff"
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_transport_failure_is_not_replaced_by_offline_authority(self):
        self.request.side_effect = CloudUnavailable("CloudTransportUnavailable")
        with self.assertRaisesRegex(CloudUnavailable, "CloudTransportUnavailable"):
            TaskChannelRouter(self.engine).prepare(self.job)
        self.assertEqual(read_state(self.store), self.state)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
