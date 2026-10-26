"""Deterministic waiting/start regressions; temporary DB and synthetic adapters only."""

import json
import os
import unittest
from unittest.mock import patch

import test_engine as fixtures

from auto_promotion.core import Conflict, Engine
from auto_promotion.queue_store import JobQueue


class ExecutionWaitTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.engine, self.store = self.f.engine, self.f.store
        self.operations = []
        self.original = self.f.provider.call

    def provider(self, account, operation, payload):
        self.operations.append(operation)
        if operation == "health":
            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "account_identity": "42",
                "permissions": ["reply"],
            }
        if operation == "preflight":
            return {"allowed": True}
        return self.original(account, operation, payload)

    def live_shape(self):
        self.engine.live_enabled = True
        self.enterContext(
            patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.f.tmp.name + "/accounts"})
        )
        self.enterContext(patch.object(self.f.registry, "commissioned", return_value=True))
        self.enterContext(patch("auto_promotion.core.automatic_action_allowed", return_value=True))
        self.enterContext(patch.object(self.f.provider, "call", side_effect=self.provider))
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET mode='live',min_interval=120 WHERE id=?",
                (self.f.accounts["x"]["id"],),
            )
        self.engine.check_account(self.f.accounts["x"]["id"])

    def task(self, *, target="https://x.com/example/status/12345"):
        task = self.f.task("x", min_interval=120)
        self.engine.set_items(task["id"], [self.f.item("x", target=target)])
        return self.engine.start_task(task["id"])

    def test_known_intent_conflict_performs_zero_external_work_and_keeps_owner(self):
        self.live_shape()
        owner, waiting = self.task(), self.task()
        before = self.store.rows("SELECT * FROM interaction_intents")
        self.operations.clear()
        for _ in range(2):
            self.engine.run_once(waiting["id"])
        self.assertEqual(self.operations, [])
        self.assertEqual(self.store.rows("SELECT * FROM interaction_intents"), before)
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        item = self.engine.task(waiting["id"])["items"][0]
        self.assertEqual(item["status"], "not_attempted")
        self.assertEqual(item["result"]["skip_reason"], "interaction_already_claimed")
        self.assertEqual(self.engine.task(owner["id"])["summary"], {"not_attempted": 1})

    def test_queue_conflict_wait_is_durable_bounded_and_rechecks_changed_owner(self):
        self.live_shape()
        owner, waiting = self.task(), self.task()
        queue = JobQueue(self.engine)
        queue.sync(waiting["id"])
        self.operations.clear()
        pool = self.store.one("SELECT pool FROM execution_jobs")["pool"]
        self.assertIsNone(queue.claim(pool, "worker"))
        job = self.store.one("SELECT * FROM execution_jobs")
        self.assertEqual(job["state"], "pending")
        self.assertGreaterEqual(job["available_at"], self.f.now + 60)
        self.assertEqual(json.loads(job["result"])["wait_reason"], "interaction_already_claimed")
        other = Engine(self.store, self.f.registry, clock=lambda: self.f.now)
        self.assertIsNone(JobQueue(other).claim(pool, "restarted"))
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE interaction_intents SET item_id=? WHERE item_id=?",
                (waiting["items"][0]["id"], owner["items"][0]["id"]),
            )
        resumed = JobQueue(other).claim(pool, "new-owner")
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["reference_id"], waiting["items"][0]["id"])
        self.assertEqual(self.operations, [])
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])

    def test_active_start_is_read_only_even_if_provider_health_would_timeout(self):
        task = self.task()
        with patch.object(
            self.f.provider, "call", side_effect=TimeoutError("synthetic")
        ) as provider:
            result = self.engine.start_and_run(task["id"])
        provider.assert_not_called()
        self.assertTrue(result["execution_result"]["already_started"])
        self.assertEqual(result["execution_result"]["processed"], [])
        self.assertEqual(result["manifest_hash"], task["manifest_hash"])
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])

    def test_active_start_still_rejects_changed_configuration(self):
        task = self.task()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET min_interval=min_interval+1 WHERE id=?",
                (self.f.accounts["x"]["id"],),
            )
        with patch.object(self.f.provider, "call") as provider, self.assertRaises(Conflict):
            self.engine.start_and_run(task["id"])
        provider.assert_not_called()

    def test_late_pause_wins_over_start_that_is_still_validating(self):
        from threading import Event, Thread

        task = self.f.task("x")
        self.engine.set_items(task["id"], [self.f.item("x")])
        entered, release, failures = Event(), Event(), []
        original = self.engine.validate_item

        def validating(*args, **kwargs):
            value = original(*args, **kwargs)
            entered.set()
            release.wait(5)
            return value

        def start():
            try:
                self.engine.start_task(task["id"])
            except Conflict as exc:
                failures.append(exc)

        with patch.object(self.engine, "validate_item", side_effect=validating):
            worker = Thread(target=start)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.engine.change_state(task["id"], "paused")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.engine.task(task["id"])["state"], "paused")
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(self.engine.start_task(task["id"])["state"], "active")

    def test_early_gate_does_not_replace_final_atomic_claim(self):
        self.live_shape()
        first, second = self.task(), self.task()
        self.operations.clear()

        def race(account, operation, payload):
            result = self.provider(account, operation, payload)
            if operation == "preflight":
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE interaction_intents SET item_id=? WHERE item_id=?",
                        (second["items"][0]["id"], first["items"][0]["id"]),
                    )
            return result

        with patch.object(self.f.provider, "call", side_effect=race):
            self.engine.run_once(first["id"])
        self.assertEqual(self.operations, ["health", "preflight"])
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])
        self.assertEqual(
            self.store.one("SELECT item_id FROM interaction_intents")["item_id"],
            second["items"][0]["id"],
        )

    def test_active_start_rechecks_local_automatic_authorization(self):
        self.live_shape()
        task = self.task()
        self.operations.clear()
        with (
            patch("auto_promotion.core.automatic_action_allowed", return_value=False),
            self.assertRaises(Conflict),
        ):
            self.engine.start_and_run(task["id"])
        self.assertEqual(self.operations, [])

    def test_stale_settle_cannot_clear_new_workers_intent_wait(self):
        self.live_shape()
        task = self.task()
        queue = JobQueue(self.engine)
        queue.sync(task["id"])
        pool = self.store.one("SELECT pool FROM execution_jobs")["pool"]
        old = queue.claim(pool, "old")
        self.assertIsNotNone(old)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET owner='new',result=? WHERE id=?",
                ('{"new_evidence":true}', old["id"]),
            )
        queue.settle(old)
        row = self.store.one("SELECT * FROM execution_jobs")
        self.assertEqual(row["owner"], "new")
        self.assertEqual(json.loads(row["result"]), {"new_evidence": True})

    def test_late_cleanup_cannot_release_new_intent_owner(self):
        from auto_promotion.interactions import Interactions

        first, second = self.task(), self.task()
        old_item, new_item = first["items"][0]["id"], second["items"][0]["id"]
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO hf_candidates(id,policy_id,project_id,platform,source_event_id,source,source_hash,source_event_at,first_seen_at,updated_at,item_id,task_id) VALUES('candidate','policy','default','x','12345','{}','hash',?,?,?, ?,?)",
                (self.f.now, self.f.now, self.f.now, old_item, first["id"]),
            )
            db.execute(
                "UPDATE interaction_intents SET candidate_id='candidate',item_id=?", (new_item,)
            )
        service = object.__new__(Interactions)
        service.clock = lambda: self.f.now
        row = {"id": "candidate", "item_id": old_item, "task_id": first["id"]}
        with self.assertRaises(Conflict), self.store.connect(write=True) as db:
            service._retire_unsubmitted(db, row)
        self.assertEqual(
            self.store.one("SELECT item_id FROM interaction_intents")["item_id"], new_item
        )
        self.assertEqual(self.engine.task(first["id"])["state"], "active")

    def test_stale_start_cannot_freeze_a_new_draft_payload(self):
        task = self.f.task("x")
        self.engine.set_items(task["id"], [self.f.item("x")])
        original = self.engine.validate_item
        edited = []

        def validate(*args, **kwargs):
            value = original(*args, **kwargs)
            if not edited:
                edited.append(True)
                self.engine.set_items(
                    task["id"],
                    [self.f.item("x", text="new authorized draft requires its own start")],
                )
            return value

        with (
            patch.object(self.engine, "validate_item", side_effect=validate),
            self.assertRaises(Conflict),
        ):
            self.engine.start_task(task["id"])
        self.assertIsNone(self.engine.task(task["id"])["manifest_hash"])

    def test_two_concurrent_first_starts_dispatch_only_once(self):
        from threading import Event, Thread

        task = self.f.task("x")
        self.engine.set_items(task["id"], [self.f.item("x")])
        entered, release, results, failures = Event(), Event(), [], []

        def provider(account, operation, payload):
            self.operations.append(operation)
            if operation == "health":
                entered.set()
                release.wait(5)
            return self.original(account, operation, payload)

        def start():
            try:
                results.append(self.engine.start_and_run(task["id"]))
            except Exception as exc:
                failures.append(type(exc).__name__)

        with patch.object(self.f.provider, "call", side_effect=provider):
            worker = Thread(target=start)
            worker.start()
            self.assertTrue(entered.wait(5))
            second = Engine(self.store, self.f.registry, clock=lambda: self.f.now).start_and_run(
                task["id"]
            )
            self.assertTrue(second["execution_result"]["already_started"])
            release.set()
            worker.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(self.operations.count("health"), 1)
        self.assertEqual(self.operations.count("execute"), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)

    def test_pause_during_first_health_prevents_final_claim(self):
        from threading import Event, Thread

        task = self.f.task("x")
        self.engine.set_items(task["id"], [self.f.item("x")])
        entered, release, results = Event(), Event(), []

        def provider(account, operation, payload):
            self.operations.append(operation)
            if operation == "health":
                entered.set()
                release.wait(5)
            return self.original(account, operation, payload)

        with patch.object(self.f.provider, "call", side_effect=provider):
            worker = Thread(target=lambda: results.append(self.engine.start_and_run(task["id"])))
            worker.start()
            self.assertTrue(entered.wait(5))
            self.engine.change_state(task["id"], "paused")
            release.set()
            worker.join(5)
        self.assertEqual(self.engine.task(task["id"])["state"], "paused")
        self.assertNotIn("execute", self.operations)
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])

    def test_wait_release_never_reclaims_attempted_unknown_or_expired_items(self):
        self.live_shape()
        for final_state in ["unknown", "expired", "cancelled"]:
            with self.subTest(final_state=final_state):
                owner, waiting = (
                    self.task(
                        target="https://x.com/example/status/"
                        + str(1000 + len(self.store.rows("SELECT id FROM tasks")))
                    ),
                    None,
                )
                target = owner["items"][0]["payload"]["target"]
                waiting = self.task(target=target)
                queue = JobQueue(self.engine)
                queue.sync(waiting["id"])
                job_row = self.store.one(
                    "SELECT * FROM execution_jobs WHERE task_id=?", (waiting["id"],)
                )
                pool = job_row["pool"]
                with patch.dict(os.environ, {"AP_EXECUTOR_TASK_IDS": waiting["id"]}):
                    self.assertIsNone(queue.claim(pool, "waiter"))
                    with self.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE interaction_intents SET item_id=? WHERE item_id=?",
                            (waiting["items"][0]["id"], owner["items"][0]["id"]),
                        )
                        if final_state == "expired":
                            db.execute(
                                "UPDATE execution_jobs SET expires_at=? WHERE id=?",
                                (self.f.now - 1, job_row["id"]),
                            )
                        else:
                            db.execute(
                                "UPDATE items SET status=? WHERE id=?",
                                (final_state, waiting["items"][0]["id"]),
                            )
                    self.assertIsNone(queue.claim(pool, "new-worker"))
                    self.assertIn(
                        self.store.one(
                            "SELECT state FROM execution_jobs WHERE id=?", (job_row["id"],)
                        )["state"],
                        ["unknown", "missed", "cancelled"],
                    )
        self.assertEqual(self.store.rows("SELECT * FROM attempts"), [])


class XReadCooldownTests(unittest.TestCase):
    setUp = ExecutionWaitTests.setUp
    provider = ExecutionWaitTests.provider
    live_shape = ExecutionWaitTests.live_shape

    def test_retry_after_blocks_every_entry_until_expiry_and_is_account_scoped(self):
        self.live_shape()
        account = self.engine.account(self.f.accounts["x"]["id"])
        calls = []

        def limited(sender, operation, payload):
            calls.append(operation)
            if len(calls) == 1:
                return {
                    "rate_limited": True,
                    "retry_after_seconds": 120,
                    "coverage_complete": False,
                    "reason": "XAPIRateLimited",
                    "read_primary_request_count": 1,
                }
            return {"allowed": True, "coverage_complete": False, "read_primary_request_count": 1}

        with patch.object(self.f.provider, "call", side_effect=limited):
            self.engine.call(account, "discover", {})
            for op in ["health", "preflight", "discover", "verify", "execute"]:
                blocked = self.engine.call(account, op, {})
                self.assertEqual(blocked.get("reason"), "XReadCooldown")
                self.assertEqual(blocked.get("account_retry_at"), self.f.now + 120)
                self.assertEqual(blocked["read_primary_request_count"], 0)
            self.assertEqual(calls, ["discover"])
            other = self.f.accounts["telegram"]
            self.engine.call(other, "health", {})
            self.assertEqual(len(calls), 2)
            self.f.now += 120
            self.engine.call(account, "preflight", {})
            self.assertEqual(calls[-1], "preflight")
        rows = self.store.rows("SELECT * FROM hf_read_cooldowns")
        self.assertEqual([row["scope_id"] for row in rows], ["x:account:" + account["id"]])
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_media_resource_failures_do_not_create_account_cooldown(self):
        self.live_shape()
        account = self.engine.account(self.f.accounts["x"]["id"])
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "coverage_complete": False,
                "read_resource_failures": {"media_429": 1},
                "read_primary_request_count": 1,
            },
        ) as provider:
            self.engine.call(account, "discover", {})
            self.engine.call(account, "discover", {})
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(self.engine.account(account["id"])["cooldown_until"], 0)
        self.assertEqual(self.store.rows("SELECT * FROM hf_read_cooldowns"), [])

    def test_legacy_account_read_deadline_is_respected_without_network(self):
        self.live_shape()
        account = self.engine.account(self.f.accounts["x"]["id"])
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES(?,?)",
                ("hf_x_read_after:" + account["id"], str(self.f.now + 300)),
            )
        self.operations.clear()
        result = self.engine.call(account, "health")
        self.assertEqual(result.get("reason"), "XReadCooldown")
        self.assertEqual(self.operations, [])

    def test_browser_read_metadata_survives_native_engine_and_export(self):
        from auto_promotion import native_bridge
        from auto_promotion.task_tools import TaskTools

        self.live_shape()
        task = self.f.task("x", min_interval=120)
        account = self.engine.account(self.f.accounts["x"]["id"])
        result = {
            "coverage_complete": False,
            "candidates": [],
            "read_state": "partial",
            "read_observed_at": self.f.now,
            "read_observation_source": "playwright_current_dom",
            "read_page_fingerprint": "1" * 64,
            "read_primary_request_count": 1,
            "read_primary_response_count": 1,
            "read_primary_requests_blocked": 0,
            "read_resource_failures": {"media_429": 1},
            "read_request_count_source": "observed_primary_search_requests",
            "read_cleanup_state": "complete",
            "live_probe": True,
        }

        def adapter(sender, operation, payload):
            return native_bridge.dispatch(
                {
                    "platform": "x",
                    "account": sender["backend_label"],
                    "account_id": sender["id"],
                    "database": self.store.path,
                    "operation": operation,
                    "payload": payload,
                }
            )

        with (
            patch.object(self.f.registry, "call", side_effect=adapter),
            patch("auto_promotion.backends.browser.call", return_value=result),
        ):
            observed = self.engine.call(account, "discover", {}, task["id"])
        self.assertEqual(observed["read_primary_request_count"], 1)
        evidence = TaskTools(self.engine).evidence(task["id"])["execution_read_observations"][0][
            "result"
        ]
        self.assertEqual(evidence["read_page_fingerprint"], "1" * 64)
        self.assertEqual(evidence["read_resource_failures"], {"media_429": 1})
        self.assertEqual(evidence["read_observation_source"], "playwright_current_dom")
        self.assertTrue(evidence["live_probe"])

    def test_long_retry_after_and_reset_are_never_shortened(self):
        self.live_shape()
        account = self.engine.account(self.f.accounts["x"]["id"])
        long_wait = 30 * 86400
        with patch.object(
            self.f.provider,
            "call",
            return_value={
                "rate_limited": True,
                "retry_after_seconds": long_wait,
                "rate_limit_reset_at": self.f.now + long_wait + 10,
            },
        ):
            result = self.engine.call(account, "discover")
        self.assertEqual(result["account_retry_at"], self.f.now + long_wait + 10)
        self.assertEqual(
            self.engine.account(account["id"])["cooldown_until"], self.f.now + long_wait + 10
        )

    def test_final_native_authorization_rechecks_legacy_read_deadline_after_claim(self):
        from auto_promotion.backends.common import BackendError, verify_claim

        self.live_shape()
        task = self.f.task("x", min_interval=120)
        self.engine.set_items(task["id"], [self.f.item("x")])
        task = self.engine.start_task(task["id"])
        claim = self.engine._claim(task["items"][0]["id"], {"allowed": True})
        self.assertIsNotNone(claim)
        account = self.engine.account(self.f.accounts["x"]["id"])
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES(?,?)",
                ("hf_x_read_after:" + account["id"], str(self.f.now + 120)),
            )
        request = {
            "database": self.store.path,
            "platform": "x",
            "account_id": account["id"],
            "account": account["backend_label"],
            "operation": "execute",
            "payload": claim,
        }
        with (
            patch.dict(
                os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_X_OWNER": "auto-promotion"}
            ),
            patch("auto_promotion.backends.common.time.time", return_value=self.f.now),
            self.assertRaisesRegex(BackendError, "AccountReadCooldown"),
        ):
            verify_claim(request)
        self.assertEqual(len(self.store.rows("SELECT * FROM attempts")), 1)
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "executing")
