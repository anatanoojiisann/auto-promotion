import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.core import Engine
from auto_promotion.models import PLATFORMS, AccountInput, ItemInput, TaskInput
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue
from auto_promotion.routing import route_for
from auto_promotion.store import Store


class UnknownAdapter(SimulationAdapter):
    calls = 0

    def call(self, account, operation, payload):
        if operation == "execute":
            self.calls += 1
            return {"status": "unknown", "reason": "LostResponse", "submission_started": True}
        return super().call(account, operation, payload)


class PersistentPoolTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "var" / "pool-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        assert Path(self.temp.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "queue.db")
        self.engine = Engine(
            self.store, AdapterRegistry(Path(self.temp.name)), clock=lambda: 1800000000
        )
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")

    def ready(self, *, account=None, start=True, target_id=None):
        from datetime import datetime, timezone

        def iso(t):
            return datetime.fromtimestamp(t, timezone.utc).isoformat()

        now = self.engine.clock()

        account = account or self.engine.add_account(
            AccountInput(
                platform="x",
                label="X test",
                backend_label="x-test",
                mode="simulation",
                min_interval=0,
                daily_limit=50,
                hourly_limit=20,
            )
        )
        self.engine.check_account(account["id"])
        task = self.engine.create_task(
            TaskInput(
                name="test",
                account_ids=[account["id"]],
                policy={
                    "starts_at": iso(now - 100),
                    "ends_at": iso(now + 3600),
                    "min_interval": 0,
                    "daily_limit": 20,
                    "max_joins": 0,
                    "target_allowlist": [],
                },
            )
        )
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=account["id"],
                    action="reply",
                    target="https://x.com/example/status/"
                    + str(target_id or len(self.store.rows("SELECT id FROM tasks"))),
                    text="specific reply",
                    scheduled_at=iso(now),
                )
            ],
        )
        if start:
            self.engine.start_task(task["id"], source="test_explicit_start")
        return task, account

    def test_all_platform_and_connection_routes(self):
        for platform in PLATFORMS:
            a = {"platform": platform, "mode": "live", "id": "a", "backend_label": "local"}
            expected = {
                "telegram": "mtproto",
                "discord": "bot",
                "instagram": "graph",
                "tiktok": "organic",
                "reddit": "manual",
                "x": "playwright",
            }[platform]
            self.assertEqual(route_for(a, profile={}).pool, platform + "." + expected)
            a["mode"] = "simulation"
            self.assertEqual(route_for(a).pool, "simulation." + platform)
        a = {"platform": "x", "mode": "live", "id": "a", "backend_label": "local"}
        for channel, pool in [
            ("msedge", "x.edge"),
            ("tabbit", "x.tabbit"),
            ("browser_use", "x.browser_use"),
            ("chromium", "x.playwright"),
        ]:
            self.assertEqual(route_for(a, profile={"browser_channel": channel}).pool, pool)
        self.assertEqual(route_for(a, {"execution_mode": "manual"}, profile={}).pool, "x.manual")

    def test_draft_is_never_enqueued_or_started(self):
        task, _ = self.ready(start=False)
        self.assertEqual(JobQueue(self.engine).sync(), 0)
        self.assertEqual(self.engine.task(task["id"])["state"], "draft")

    def test_legacy_run_entry_point_queues_in_pooled_mode(self):
        task, _ = self.ready()
        from auto_promotion.pool_runtime import PoolScheduler

        PoolScheduler(self.engine).enable()
        self.assertTrue(self.engine.run_once(task["id"])["queued"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM execution_jobs")["n"], 1)

    def test_stale_client_cannot_consume_attempt_without_pool_owner(self):
        import sqlite3

        from auto_promotion.pool_runtime import PoolScheduler

        self.ready()
        PoolScheduler(self.engine).enable()
        JobQueue(self.engine).sync()
        JobQueue(self.engine).claim("simulation.x", "actual-worker")
        item = self.store.one("SELECT * FROM items")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "PersistentJobLeaseRequired"):
            self.engine._claim(item["id"])
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_sync_is_durable_and_idempotent(self):
        task, _ = self.ready()
        queue = JobQueue(self.engine)
        self.assertEqual(queue.sync(), 1)
        self.assertEqual(queue.sync(), 0)
        reopened = Engine(
            Store(self.store.path), AdapterRegistry(Path(self.temp.name)), clock=self.engine.clock
        )
        self.assertEqual(len(JobQueue(reopened).pools()["jobs"]), 1)
        self.assertIsNotNone(self.engine.task(task["id"])["manifest_hash"])

    def test_cancelled_task_closes_only_unattempted_pending_join(self):
        from datetime import datetime, timezone

        from auto_promotion.models import CandidateInput

        now = self.engine.clock()
        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="test",
                backend_label="test-join",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(account["id"])
        task = self.engine.create_task(
            TaskInput(
                name="cancel join",
                account_ids=[account["id"]],
                policy={
                    "starts_at": datetime.fromtimestamp(now - 10, timezone.utc).isoformat(),
                    "ends_at": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
                    "min_interval": 0,
                    "max_joins": 1,
                },
            )
        )
        self.engine.candidate(
            task["id"],
            CandidateInput(
                account_id=account["id"], target="@test_ai", language="en", confidence=0.9
            ),
        )
        self.engine.start_task(task["id"], sending=False)
        queue = JobQueue(self.engine)
        queue.sync()
        self.engine.change_state(task["id"], "cancelled")
        queue.recover()
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "cancelled")
        self.assertEqual(self.store.one("SELECT state FROM candidates")["state"], "not_attempted")
        self.assertEqual(self.store.one("SELECT count(*) n FROM join_attempts")["n"], 0)

    def test_cancel_does_not_reset_unknown_attempt(self):
        task, _ = self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown',attempt_id='uncertain'")
        self.engine.change_state(task["id"], "cancelled")
        queue.recover()
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertEqual(self.store.one("SELECT attempt_id FROM items")["attempt_id"], "uncertain")

    def test_due_interaction_is_not_starved_by_older_join_backlog(self):
        from datetime import datetime, timezone

        from auto_promotion.models import CandidateInput

        now = self.engine.clock()

        def iso(at):
            return datetime.fromtimestamp(at, timezone.utc).isoformat()

        account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="test",
                backend_label="test-priority",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(account["id"])
        policy = {"starts_at": iso(now - 100), "ends_at": iso(now + 3600), "min_interval": 0}
        join = self.engine.create_task(
            TaskInput(
                name="join backlog", account_ids=[account["id"]], policy={**policy, "max_joins": 1}
            )
        )
        self.engine.candidate(
            join["id"],
            CandidateInput(
                account_id=account["id"], target="@test_ai_join", language="en", confidence=0.9
            ),
        )
        self.engine.start_task(join["id"], sending=False)
        send = self.engine.create_task(
            TaskInput(name="due interaction", account_ids=[account["id"]], policy=policy)
        )
        self.engine.set_items(
            send["id"],
            [
                ItemInput(
                    account_id=account["id"],
                    action="send_message",
                    target="@test_ai_send",
                    text="specific question",
                    scheduled_at=iso(now),
                )
            ],
        )
        self.engine.start_task(send["id"])
        queue = JobQueue(self.engine)
        queue.sync()
        self.assertEqual(queue.claim("simulation.telegram", "test-worker")["kind"], "send")

    def test_two_consumers_claim_only_once(self):
        self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(lambda owner: queue.claim("simulation.x", owner), ["one", "two"])
            )
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_account_is_serial_across_tasks(self):
        _, a = self.ready()
        self.ready(account=a)
        queue = JobQueue(self.engine)
        queue.sync()
        self.assertIsNotNone(queue.claim("simulation.x", "one"))
        self.assertIsNone(queue.claim("simulation.x", "two"))

    def test_expired_lease_with_attempt_does_not_requeue(self):
        self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        job = queue.claim("simulation.x", "dead")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET lease_until=0")
            db.execute(
                "UPDATE items SET status='executing',attempt_id='recorded-attempt' WHERE id=?",
                (job["reference_id"],),
            )
        queue.recover()
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertIsNone(queue.claim("simulation.x", "replacement"))

    def test_expired_preparation_without_attempt_can_be_reclaimed(self):
        self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        queue.claim("simulation.x", "dead")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET lease_until=0")
        self.assertIsNotNone(queue.claim("simulation.x", "replacement"))

    def test_global_stop_blocks_claims(self):
        self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        self.assertIsNone(queue.claim("simulation.x", "one"))

    def test_unknown_send_is_never_replayed_after_worker_restart(self):
        task, _ = self.ready()
        adapter = UnknownAdapter()
        self.engine.adapters.overrides["x"] = adapter
        queue = JobQueue(self.engine)
        queue.sync()
        run_pool(self.engine, "simulation.x", "test-node", max_jobs=1)
        self.assertEqual(self.engine.task(task["id"])["summary"], {"unknown": 1})
        queue.sync()
        self.assertIsNone(queue.claim("simulation.x", "replacement"))
        self.assertEqual(adapter.calls, 1)

    def test_expired_pending_item_is_missed_without_attempt(self):
        self.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET expires_at=0")
        queue.recover()
        item = self.store.one("SELECT status,attempt_id FROM items")
        self.assertEqual(item, {"status": "missed", "attempt_id": None})

    def test_shared_browser_resource_is_exclusive_across_accounts(self):
        a = self.engine.add_account(
            AccountInput(
                platform="x",
                label="second",
                backend_label="second",
                mode="simulation",
                min_interval=0,
            )
        )
        with self.engine.lanes.acquire(a["id"], resource="desktop:shared"):
            from auto_promotion.dispatcher import AccountBusy

            other = self.engine.add_account(
                AccountInput(
                    platform="x",
                    label="third",
                    backend_label="third",
                    mode="simulation",
                    min_interval=0,
                )
            )
            with self.assertRaises(AccountBusy):
                with self.engine.lanes.acquire(other["id"], resource="desktop:shared"):
                    pass

    def test_six_platform_pools_execute_in_independent_processes(self):
        from datetime import datetime, timezone

        self.engine.clock = time.time
        now = time.time()

        def iso(at):
            return datetime.fromtimestamp(at, timezone.utc).isoformat()

        targets = {
            "telegram": {"action": "send_message", "target": "@simulated_group"},
            "x": {"action": "reply", "target": "https://x.com/example/status/123"},
            "reddit": {
                "action": "reply",
                "target": "https://www.reddit.com/r/example/comments/abc/test/",
            },
            "instagram": {
                "action": "manual_comment",
                "target": "https://www.instagram.com/p/test/",
                "execution_mode": "manual",
            },
            "discord": {"action": "send_message", "target": "123456789012345678"},
            "tiktok": {
                "action": "reply",
                "target": "owned-account",
                "target_owned": True,
                "tiktok_video_id": "123",
                "tiktok_comment_id": "456",
            },
        }
        for platform in PLATFORMS:
            a = self.engine.add_account(
                AccountInput(
                    platform=platform,
                    label=platform,
                    backend_label="test-" + platform,
                    mode="simulation",
                    min_interval=0,
                )
            )
            self.engine.check_account(a["id"])
            task = self.engine.create_task(
                TaskInput(
                    name=platform,
                    account_ids=[a["id"]],
                    policy={
                        "starts_at": iso(now - 60),
                        "ends_at": iso(now + 3600),
                        "min_interval": 0,
                        "max_joins": 0,
                    },
                )
            )
            self.engine.set_items(
                task["id"],
                [
                    ItemInput(
                        account_id=a["id"],
                        text="simulated content",
                        scheduled_at=iso(now),
                        **targets[platform],
                    )
                ],
            )
            self.engine.start_task(task["id"], source="test_explicit_start")
        from auto_promotion.pool_runtime import PoolScheduler

        PoolScheduler(self.engine).enable()
        JobQueue(self.engine).sync()
        env = {k: v for k, v in os.environ.items() if not k.startswith("AP_CLOUD_")}
        env["AUTO_PROMOTION_LIVE_WRITES"] = "0"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "auto_promotion.cli",
                    "--db",
                    self.store.path,
                    "pool-worker",
                    "--pool",
                    "simulation." + p,
                    "--node-id",
                    "process-test",
                    "--idle-exit",
                    "1",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for p in PLATFORMS
        ]
        try:
            for child in children:
                out, err = child.communicate(timeout=25)
                self.assertEqual(child.returncode, 0, err.decode(errors="replace"))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        states = self.store.rows("SELECT status,COUNT(*) n FROM items GROUP BY status")
        self.assertEqual(
            {r["status"]: r["n"] for r in states}, {"confirmed": 5, "manual_attention": 1}
        )
        self.assertEqual(
            len({r["pid"] for r in self.store.rows("SELECT pid FROM pool_workers")}), 6
        )


if __name__ == "__main__":
    unittest.main()
