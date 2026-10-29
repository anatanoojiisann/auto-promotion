"""Regression cases from the three-day audit. Only synthetic adapters and stores."""

import json
import sqlite3
import unittest

import test_task_channels as fixtures

from auto_promotion.channel_decisions import resolved
from auto_promotion.execution_channels import configure_independent, offline_only
from auto_promotion.execution_diagnostics import exception_details
from auto_promotion.models import AccountInput, Policy, canonical
from auto_promotion.queue_store import JobQueue
from auto_promotion.readiness import task_workflow
from auto_promotion.statistics_retries import pending_until, settle_read
from auto_promotion.task_channels import TaskChannelRouter


class BlockageRepairs(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskChannelTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.engine, self.store = self.f.engine, self.f.store

    def test_unknown_finishes_execution_without_requeue_or_success(self):
        t, _ = self.f.fixture.ready(channel="local")
        self.f.queue.sync()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown',result=? WHERE task_id=?",
                (canonical({"reason": "receipt_missing", "verified": False}), t["id"]),
            )
            self.engine._mark_completed(db, t["id"])
        result = self.engine.task(t["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"], {"unknown": 1})
        self.assertEqual(
            result["readiness"]["workflow"]["status"], "completed_pending_verification"
        )
        self.assertIsNone(self.f.queue.claim("simulation.x", "next"))
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))

    def test_expired_starting_discovered_and_empty_tasks_finish_idempotently(self):
        account = None
        for phase, empty in [("starting", False), ("discovered", False), ("discovered", True)]:
            t, account = self.f.fixture.ready(start=False, account=account)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE tasks SET state='active',phase=? WHERE id=?", (phase, t["id"]))
                if empty:
                    db.execute("DELETE FROM items WHERE task_id=?", (t["id"],))
            self.f.now += 4000
            self.f.queue.sync(t["id"])
            self.f.queue.sync(t["id"])
            current = self.engine.task(t["id"])
            self.assertEqual(current["state"], "completed")
            self.assertTrue(all(i["status"] == "missed" for i in current["items"]))
            self.assertEqual(
                len(
                    self.store.rows(
                        "SELECT * FROM audit WHERE kind='task.execution_finished' AND task_id=?",
                        (t["id"],),
                    )
                ),
                1,
            )

    def test_live_lease_is_not_closed_or_reset(self):
        t, _ = self.f.fixture.ready(channel="local")
        job = self.f.claim(t)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE task_id=?", (t["id"],))
            self.engine._mark_completed(db, t["id"])
        self.assertEqual(self.engine.task(t["id"])["state"], "active")
        self.assertEqual(
            self.store.one("SELECT state FROM execution_jobs WHERE id=?", (job["id"],))["state"],
            "leased",
        )

    def test_chronic_read_failure_backs_off_across_rounds_and_restarts(self):
        with self.store.connect(write=True) as db:
            first = settle_read(db, "old", {"reason": "no_durable_receipt"}, False, self.f.now)
        with self.store.connect(write=True) as db:
            self.assertEqual(pending_until(db, "old"), first)
            second = settle_read(db, "old", {"reason": "no_durable_receipt"}, False, first)
            self.assertGreater(second - first, first - self.f.now)
            reset = settle_read(
                db,
                "budget",
                {
                    "reason": "TelegramReadAccountBudgetExhausted",
                    "read_rpc_budget_resets_at": self.f.now + 86400,
                },
                False,
                self.f.now,
            )
            self.assertEqual(reset, self.f.now + 86400)
            settle_read(db, "old", {}, True, second)
            self.assertEqual(pending_until(db, "old"), 0)

    def test_explicit_local_intent_and_rich_route_audit_are_preserved(self):
        t, _ = self.f.fixture.ready(channel="local")
        before = self.engine.task(t["id"])["policy_hash"]
        context, _ = TaskChannelRouter(self.engine).prepare(self.f.claim(t))
        row = self.store.one(
            "SELECT details FROM audit WHERE kind='execution.job_channel' AND task_id=?", (t["id"],)
        )
        data = json.loads(row["details"])
        self.assertEqual(data["requested_channel"], "local")
        self.assertEqual(data["channel"], context["channel"])
        self.assertEqual(data["policy_hash"], before)
        self.assertIn("generation", data)
        self.assertEqual(self.engine.task(t["id"])["policy_hash"], before)

    def test_expired_independent_local_never_resolves_online(self):
        t, a = self.f.fixture.ready(channel="local")
        configure_independent(self.engine, [a["id"]], self.f.now + 1)
        self.f.now += 2
        self.assertEqual(
            resolved(self.engine, {"execution_channel": "inherit"}, t["id"], a["id"]), "local"
        )
        self.assertTrue(self.engine.task(t["id"])["channel"]["local_authority_expired"])

    def test_scoped_offline_does_not_affect_other_account(self):
        t, a = self.f.fixture.ready(channel="local")
        configure_independent(self.engine, [a["id"]], self.f.now + 3600)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
            state = json.loads(row[0])
            state["offline_only"] = True
            db.execute("UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(state),))
        self.assertTrue(offline_only(self.store, a["id"]))
        self.assertFalse(offline_only(self.store, "unrelated"))
        cloud, _, _, _ = self.f.fake_cloud()
        b = self.engine.add_account(
            AccountInput(
                platform="x",
                label="other",
                backend_label="other",
                mode="simulation",
                min_interval=0,
            )
        )
        u, _ = self.f.fixture.ready(account=b, channel="cloud", target_id=1001)
        self.f.queue.sync(u["id"])
        job = self.f.queue.claim(
            "simulation.x", "outside", reference_id=self.engine.task(u["id"])["items"][0]["id"]
        )
        context, _ = TaskChannelRouter(self.engine).prepare(job)
        self.assertEqual(context["channel"], "cloud")
        self.assertIs(self.f.cloud_config.return_value, cloud)
        from auto_promotion.cloud_control import CloudJournal

        lease = {"id": job["id"], "account_id": b["id"], "lease_token": "synthetic"}
        outcome = {
            "state": "failed_known",
            "result": {"confirmed_not_submitted": True},
            "attempt_id": None,
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,outcome,updated_at) VALUES(?,?,?,?)",
                (job["id"], canonical(lease), canonical(outcome), self.f.now),
            )
        CloudJournal(self.engine, cloud).flush(job_id=job["id"])
        self.assertFalse(self.store.rows("SELECT * FROM cloud_receipt_outbox"))
        self.assertEqual(len(self.store.rows("SELECT * FROM cloud_receipt_history")), 1)

    def test_user_selected_local_is_not_reported_as_quota_fallback(self):
        t = {
            "id": "fixture",
            "state": "active",
            "mode": "live",
            "phase": "ready",
            "next_at": 0,
            "manifest_hash": "hash",
            "policy": {
                "starts_at": "2026-10-26T00:00:00+08:00",
                "ends_at": "2040-01-01T00:00:00+08:00",
            },
            "items": [
                {
                    "status": "not_attempted",
                    "due_at": self.f.now - 10,
                    "payload": {"action": "reply"},
                    "result": {},
                    "external_reference": None,
                }
            ],
            "candidates": [],
            "execution": {
                "pools": {
                    "channel": {"valid_for_task": True, "reason": "explicit_user_local"},
                    "cloud": {"state": "CloudReadQuotaExceeded"},
                }
            },
        }
        result = task_workflow(t, [], [], self.f.now)
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("额度耗尽", result["message"])
        self.assertIn("用户选择", result["message"])

    def test_explicit_instruction_source_requires_explicit_channel(self):
        with self.assertRaises(ValueError):
            Policy(
                starts_at="2026-10-26T00:00:00+08:00",
                ends_at="2026-10-27T00:00:00+08:00",
                execution_channel_source="chat:user-message",
            )

    def test_sqlite_diagnostics_retain_codes_without_private_message(self):
        exc = sqlite3.OperationalError("private path and secret")
        exc.sqlite_errorcode = 5
        exc.sqlite_errorname = "SQLITE_BUSY"
        exc.db_phase = "write_lock"
        exc.lock_wait_ms = 125
        result = exception_details(exc)
        self.assertEqual(result["sqlite_errorcode"], 5)
        self.assertEqual(result["db_phase"], "write_lock")
        self.assertNotIn("private", json.dumps(result))

    def test_more_than_64_blocked_jobs_do_not_starve_another_account(self):
        t, a = self.f.fixture.ready(channel="local")
        self.f.queue.sync(t["id"])
        item = self.engine.task(t["id"])["items"][0]
        job = self.store.one("SELECT * FROM execution_jobs WHERE reference_id=?", (item["id"],))
        route = json.loads(job["route"])
        route["resource"] = "busy-profile"
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET route=? WHERE task_id=?", (canonical(route), t["id"])
            )
            for n in range(65):
                iid = "blocked-" + str(n)
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        iid,
                        t["id"],
                        a["id"],
                        n + 2,
                        canonical(item["payload"]),
                        item["payload_hash"],
                        self.f.now - 10,
                    ),
                )
                db.execute(
                    "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "send:" + iid,
                        "send",
                        iid,
                        t["id"],
                        a["id"],
                        "simulation.x",
                        canonical(route),
                        item["payload_hash"],
                        self.f.now - 10,
                        self.f.now + 100,
                        self.f.now,
                        self.f.now,
                    ),
                )
            db.execute(
                "INSERT INTO execution_resources VALUES(?,?,?)",
                (route["resource"], "blocked", self.f.now + 100),
            )
        b = self.engine.add_account(
            AccountInput(
                platform="x",
                label="other",
                backend_label="other",
                mode="simulation",
                min_interval=0,
            )
        )
        u, _ = self.f.fixture.ready(account=b, channel="local", target_id=1003)
        self.f.queue.sync(u["id"])
        selected = JobQueue(self.engine).claim("simulation.x", "next")
        self.assertEqual(selected["task_id"], u["id"])


class DependencyRepairs(unittest.TestCase):
    def setUp(self):
        import test_x_original_posts as original

        self.f = original.OriginalPostTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_finished_dependency_preserves_unknown_receipts(self):
        source = self.f.source()
        self.f.finish(source, status="unknown", verified=False, published=False)
        child = self.f.item(
            self.f.task(sources=[source["id"]], outbound_dependency_mode="finished")
        )
        child = self.f.engine.start_task(child["id"])
        self.assertEqual(child["phase"], "ready")
        self.assertTrue(
            all(i["status"] == "unknown" for i in self.f.engine.task(source["id"])["items"])
        )

    def test_dependency_deadline_ends_wait_without_submission(self):
        import test_x_original_posts as original

        source = self.f.source()
        child = self.f.item(
            self.f.task(
                sources=[source["id"]], outbound_dependency_deadline=original.iso(self.f.now + 100)
            )
        )
        self.f.engine.start_task(child["id"])
        self.f.now += 101
        JobQueue(self.f.engine).sync(child["id"])
        current = self.f.engine.task(child["id"])
        self.assertEqual(current["state"], "completed")
        self.assertEqual(
            current["items"][0]["result"]["reason"], "interaction_dependency_deadline_elapsed"
        )
        self.assertFalse(self.f.store.rows("SELECT * FROM attempts"))
