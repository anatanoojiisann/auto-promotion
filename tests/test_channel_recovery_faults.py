import unittest
from unittest.mock import patch

import test_task_channels as channel_fixtures

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.execution_channels import configure_independent, use_cloud
from auto_promotion.pool_runtime import run_pool
from auto_promotion.task_channels import TaskChannelRouter, guard_context


class ChannelRecoveryFaultTests(unittest.TestCase):
    def setUp(self):
        self.base = channel_fixtures.TaskChannelTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.engine, self.store = self.base.engine, self.base.store

    def test_handoff_ack_must_match_full_authority_tuple(self):
        account = None
        for index, (field, value) in enumerate(
            (
                ("node_id", "foreign"),
                ("account_id", "foreign"),
                ("store_id", "foreign"),
                ("task_id", "foreign"),
                ("until_at", 1),
            )
        ):
            with self.subTest(field=field):
                task, account = self.base.fixture.ready(
                    account=account, channel="cloud", target_id=1000 + index
                )
                cloud, _, request, _ = self.base.fake_cloud()
                original = request.side_effect

                def corrupt(path, payload=None, **kwargs):
                    response = original(path, payload, **kwargs)
                    if path == "/v1/account-channel":
                        response["channel"][field] = value
                    return response

                cloud.request.side_effect = corrupt
                job = self.base.claim(task)
                try:
                    with self.assertRaisesRegex(CloudUnavailable, "TaskChannelHandoffUnverified"):
                        TaskChannelRouter(self.engine).prepare(job)
                finally:
                    self.base.queue.settle(job)
                    self.engine.change_state(task["id"], "cancelled")
                    self.base.now += 31

    def test_same_generation_recovery_does_not_repeat_transition_audit(self):
        task, _ = self.base.fixture.ready(channel="cloud")
        _, _, _, _ = self.base.fake_cloud(lose_response=True)
        job = self.base.claim(task)
        with self.assertRaises(CloudUnavailable):
            TaskChannelRouter(self.engine).prepare(job)
        self.base.queue.settle(job)
        self.base.now += 31
        job = self.base.queue.claim("simulation.x", "new-owner")
        TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='execution.account_handoff_started'"
            )["n"],
            1,
        )

    def test_late_same_generation_failure_cannot_delay_recovered_owner(self):
        task, account = self.base.fixture.ready(channel="cloud")
        cloud, _, request, _ = self.base.fake_cloud()
        job = self.base.claim(task)
        original = request.side_effect

        def delayed_failure(path, payload=None, **kwargs):
            response = original(path, payload, **kwargs)
            if path == "/v1/account-channel":
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE execution_jobs SET owner='new-owner' WHERE id=?", (job["id"],)
                    )
                    db.execute(
                        "UPDATE account_task_channels SET state='ready',error=NULL,retry_at=0 WHERE account_id=?",
                        (account["id"],),
                    )
                raise CloudUnavailable("CloudTransportUnavailable")
            return response

        cloud.request.side_effect = delayed_failure
        with self.assertRaises(CloudUnavailable):
            TaskChannelRouter(self.engine).prepare(job)
        row = self.store.one("SELECT * FROM account_task_channels")
        self.assertEqual(row["state"], "ready")
        self.assertIsNone(row["error"])
        self.assertEqual(row["retry_at"], 0)

    def test_real_pool_late_failure_cannot_modify_new_generation_owner_or_backoff(self):
        task, account = self.base.fixture.ready(channel="cloud")
        self.base.fake_cloud()
        self.base.queue.sync()

        def stale_prepare(job):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE execution_jobs SET owner='new-owner' WHERE id=?", (job["id"],))
                db.execute(
                    "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                    (
                        account["id"],
                        "cloud",
                        "2" * 32,
                        self.base.now + 3600,
                        task["id"],
                        self.base.now,
                    ),
                )
            raise CloudUnavailable("OldWorkerLostResponse")

        with patch(
            "auto_promotion.pool_runtime.TaskChannelRouter.prepare", side_effect=stale_prepare
        ):
            run_pool(self.engine, "simulation.x", "old-node", max_jobs=1)
        row = self.store.one("SELECT * FROM account_task_channels")
        self.assertEqual(
            (row["generation"], row["state"], row["error"], row["retry_at"]),
            ("2" * 32, "ready", None, 0),
        )
        self.assertEqual(self.store.one("SELECT owner FROM execution_jobs")["owner"], "new-owner")
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_real_pool_late_success_cannot_replace_new_generation_error_or_claim(self):
        task, _ = self.base.fixture.ready(channel="cloud")
        cloud, _, request, _ = self.base.fake_cloud()
        self.base.queue.sync()
        original = request.side_effect

        def stale_success(path, payload=None, **kwargs):
            result = original(path, payload, **kwargs)
            if path == "/v1/account-channel":
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE execution_jobs SET owner='new-owner'")
                    db.execute(
                        "UPDATE account_task_channels SET generation=?,state='ready',error='NewOwnerError',retry_at=?",
                        ("2" * 32, self.base.now + 99),
                    )
            return result

        cloud.request.side_effect = stale_success
        run_pool(self.engine, "simulation.x", "old-node", max_jobs=1)
        row = self.store.one("SELECT * FROM account_task_channels")
        self.assertEqual(
            (row["generation"], row["state"], row["error"], row["retry_at"]),
            ("2" * 32, "ready", "NewOwnerError", self.base.now + 99),
        )
        self.assertEqual(self.store.one("SELECT owner FROM execution_jobs")["owner"], "new-owner")
        self.assertEqual(self.store.one("SELECT count(*) n FROM task_channel_claims")["n"], 0)

    def test_original_legacy_grant_revocation_invalidates_final_guard(self):
        task, account = self.base.fixture.ready(channel="local")
        configure_independent(self.engine, [account["id"]], self.base.now + 4000)
        job = self.base.claim(task)
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(
            self.store.one("SELECT fence_source FROM account_task_channels")["fence_source"],
            "legacy",
        )
        guard_context(self.engine, context)
        use_cloud(self.engine)
        with self.assertRaisesRegex(CloudUnavailable, "TaskChannelClaimExpired"):
            guard_context(self.engine, context)
        self.base.fake_cloud()
        next_context, _ = TaskChannelRouter(self.engine).prepare(job)
        self.assertNotEqual(next_context["generation"], context["generation"])
        self.assertEqual(
            self.store.one("SELECT fence_source FROM account_task_channels")["fence_source"],
            "cloud_acknowledged",
        )

    def test_retry_after_is_not_shortened_and_blocks_reentry_without_requests(self):
        task, _ = self.base.fixture.ready(channel="cloud")
        _, _, request, _ = self.base.fake_cloud()
        request.side_effect = CloudUnavailable(
            "CloudTransportUnavailable", retry_after_seconds=864000
        )
        job = self.base.claim(task)
        router = TaskChannelRouter(self.engine)
        with self.assertRaises(CloudUnavailable):
            router.prepare(job)
        self.assertEqual(
            self.store.one("SELECT retry_at FROM account_task_channels")["retry_at"],
            self.base.now + 864000,
        )
        for _ in range(3):
            with self.assertRaises(CloudUnavailable):
                router.prepare(job)
        self.assertEqual(request.call_count, 1)

    def test_same_generation_shorter_verified_fence_is_monotonically_renewed(self):
        task, account = self.base.fixture.ready(channel="cloud")
        _, remote, request, _ = self.base.fake_cloud()
        job = self.base.claim(task)
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE account_task_channels SET state='handoff'")
        remote[account["id"]]["until_at"] -= 100
        renewed, _ = TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(renewed["generation"], context["generation"])
        writes = [call for call in request.call_args_list if call.args[0] == "/v1/account-channel"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1].args[1]["expected_generation"], context["generation"])
        self.assertEqual(
            self.store.one(
                "SELECT count(*) n FROM audit WHERE kind='execution.account_handoff_started'"
            )["n"],
            1,
        )
